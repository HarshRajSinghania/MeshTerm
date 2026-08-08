"""Compose a rendered map frame: basemap geometry, labels, and mesh nodes.

Given a :class:`~meshterm.core.geo.Viewport` and the decoded vector tiles covering it, this
projects every street, river, water body and place label onto a :class:`MapCanvas`, then
overlays the mesh nodes with their names on top. It is pure and synchronous — tile *fetching*
happens elsewhere (:mod:`meshterm.services.basemap`) — so both the interactive screen and
the one-shot CLI render call the same code.

Colours target a dark terminal (the app theme): warm roads, grey minor streets, blue water
and rivers, faint green parks. Because a cell shows one colour, draw priorities keep the
important feature visible where things overlap (rivers over water, major roads over minor).
The features whose hue *is* the information — water, its watercourses, parks, highways —
name a ``map.*`` theme style instead of a hex so the 16-slot console chooses its own shade
(see :data:`~meshterm.ui.theme.MESH_THEME_16`); everything else is grey either way.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..core.geo import Viewport
from ..core.mvt import GEOM_LINE, GEOM_POLYGON, Layer
from .mapcanvas import MapCanvas
from .marks import NODE_MARK, REPEATER_MARK, RGB, SELF_MARK, UNKNOWN_MARK, parse_hex
from .theme import mark_rgb

# -- node markers -------------------------------------------------------------


@dataclass(slots=True)
class MapMarker:
    """One mesh node to overlay on the map.

    Attributes:
        label: Node name shown beside the marker.
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        is_repeater: Whether the node is a repeater (prioritised marker).
        is_self: Whether this is our own node (highlighted).
        detail: Extra text for the CLI legend (e.g. ``"18 pkts · +6.0 dB"``).
        key: The node's key hex (as full as the caller holds), seeding the label's
            key-derived hue; ``None`` leaves the label the muted no-key grey.
    """

    label: str
    lat: float
    lon: float
    is_repeater: bool = False
    is_self: bool = False
    detail: str = ""
    key: Optional[str] = None

    def _rank(self) -> int:
        """Draw order: self on top of repeaters on top of leaf nodes."""
        return 2 if self.is_self else (1 if self.is_repeater else 0)


# Marker palette, shared across the whole app — canonical tuples in ui.marks; the old
# private names stay bound here for this module and its existing importers.
_SELF = SELF_MARK
_REPEATER = REPEATER_MARK
_NODE = NODE_MARK
_UNKNOWN = UNKNOWN_MARK


# -- basemap styling ----------------------------------------------------------

# Every colour here is whatever :func:`~meshterm.ui.theme.mark_rgb` takes: a literal
# ``#rrggbb``, or a theme style name where the hue carries meaning and the platform must
# pick its own shade (the ``map.*`` entries — see the theme, which explains why water,
# parks and highways cannot survive a naive downsample to 16 slots).

# Road class -> (colour, priority). Higher priority wins a shared cell.
_ROAD_STYLE: dict[str, tuple[str, int]] = {
    "motorway": ("map.highway", 27),
    "trunk": ("map.highway", 26),
    "primary": ("#e8b24a", 25),
    "secondary": ("#d7c257", 24),
    "tertiary": ("#a7adb8", 23),
    "minor": ("#828894", 21),
    "unclassified": ("#828894", 21),
    "residential": ("#828894", 21),
    "living_street": ("#767c88", 20),
    "service": ("#5f646e", 19),
    "pedestrian": ("#5a5f69", 15),
    "path": ("#4b5560", 12),
    "track": ("#4b5560", 12),
    "footway": ("#454e58", 11),
    "cycleway": ("#455864", 12),
}
_ROAD_DEFAULT = ("#5f646e", 18)
_RAIL = ("#8a8f98", 22)

# Waterway class -> (colour, priority).
_WATERWAY_STYLE: dict[str, tuple[str, int]] = {
    "river": ("map.river", 30),
    "canal": ("map.river", 30),
    "stream": ("map.stream", 29),
    "ditch": ("map.ditch", 28),
    "drain": ("map.ditch", 28),
}
_WATER_FILL = ("map.water", 6)
_GREEN_FILL = ("map.park", 4)
_GREEN_CLASSES = {"wood", "forest", "grass", "park", "meadow", "scrub", "wetland", "cemetery"}

# Label styling per place class: (colour, bold, rank, min_display_zoom). Lower rank places
# claim screen space first; min zoom hides the fine detail until you zoom in.
_PLACE_STYLE: dict[str, tuple[str, bool, int, int]] = {
    "city": ("#f8fafc", True, 0, 0),
    "town": ("#e6ebf2", True, 1, 9),
    "village": ("#cdd6e2", False, 2, 12),
    "suburb": ("#9fb0c4", False, 3, 12),
    "quarter": ("#94a5ba", False, 4, 13),
    "neighbourhood": ("#8496ab", False, 5, 13),
    "hamlet": ("#8496ab", False, 6, 14),
}
_WATER_LABEL = ("#7dd3fc", False, 2)
_STREET_LABEL = ("#9aa0aa", False, 7)

#: Polygon layers drawn as fills, in painting order (later wins the shared cell).
_FILL_LAYERS = ("water", "landcover", "landuse", "park")

#: Every basemap layer :func:`_draw_tile` looks at — and so the only geometry the map
#: has any use for. A planet tile also ships ``building``, ``housenumber``, ``poi`` and
#: ``mountain_peak``, which together are a large share of its features and none of its
#: pixels: decoding them cost ~40% of every tile until this set was handed to
#: :func:`~meshterm.core.mvt.decode_tile` (measured on the Lyra — a 153 KB tile went
#: 1236 ms → 281 ms). Passed to the tile source at construction
#: (:attr:`meshterm.context.AppContext.basemap_source`); the on-disk cache still holds
#: whole tiles, so widening this set costs a re-decode, never a re-download.
DRAWN_LAYERS: frozenset[str] = frozenset(
    _FILL_LAYERS
    + ("waterway", "transportation", "boundary", "transportation_name", "place", "water_name")
)


@dataclass(slots=True)
class _Label:
    """A pending label placement candidate.

    Attributes:
        alts: Further anchors to try, in order, when the preferred one is already taken.
            A line feature offers these along the stretch of itself that is on screen, so
            a street whose middle is under a node marker still gets named further along.
    """

    rank: int
    x: float
    y: float
    text: str
    color: RGB
    bold: bool
    min_zoom: int = 0
    alts: tuple[tuple[float, float], ...] = ()


@dataclass(slots=True)
class _Frame:
    """Working state while composing one frame."""

    canvas: MapCanvas
    viewport: Viewport
    labels: list[_Label] = field(default_factory=list)


def render_map(
    viewport: Viewport,
    tiles: dict[tuple[int, int, int], Optional[list[Layer]]],
    markers: list[MapMarker],
    *,
    max_labels: int = 80,
    find: str = "",
) -> list[str]:
    """Render a full map frame to truecolour ANSI lines.

    Args:
        viewport: The view to render (its dot size sets the canvas size).
        tiles: Decoded layers keyed by ``(z, x, y)``; ``None`` values are pending/absent.
        markers: Mesh nodes to overlay.
        max_labels: Cap on basemap labels placed, to keep the map readable.
        find: A live node-name filter: markers whose label contains it
            (case-insensitively) draw with bright white labels while the rest dim to
            unlabelled context. Empty draws every node normally.

    Returns:
        One ANSI string per row, ready for the TUI frame or the console.
    """
    canvas = MapCanvas(viewport.dot_w // 2, viewport.dot_h // 4)
    frame = _Frame(canvas=canvas, viewport=viewport)

    for (z, x, y), layers in tiles.items():
        if layers:
            _draw_tile(frame, layers, z, x, y)

    # Nodes reserve their cells first so basemap labels route around them.
    _draw_nodes(canvas, viewport, markers, find=find)

    # Then place basemap labels by importance, honouring the zoom gate and collisions.
    # OpenStreetMap splits a long street into several named features, so one name can
    # arrive many times over; on a terminal's worth of columns the second copy is only
    # ever taking space from a street that has none, so a name is drawn once per frame.
    placed = 0
    named: set[str] = set()
    for label in sorted(frame.labels, key=lambda l: l.rank):
        if placed >= max_labels:
            break
        if viewport.zoom < label.min_zoom or label.text in named:
            continue
        for ax, ay in ((label.x, label.y), *label.alts):
            if canvas.place_label(ax, ay, label.text, label.color, bold=label.bold):
                placed += 1
                named.add(label.text)
                break

    return canvas.to_ansi_lines()


def _draw_tile(frame: _Frame, layers: list[Layer], z: int, x: int, y: int) -> None:
    """Draw one decoded tile's geometry and collect its label candidates."""
    vp = frame.canvas
    by_name = {layer.name: layer for layer in layers}

    def project(ring: list[tuple[int, int]], extent: int) -> list[tuple[float, float]]:
        return [
            frame.viewport.feature_to_dot(x, y, z, extent, lx, ly) for lx, ly in ring
        ]

    # Fills first (water, green space) so lines and labels sit on top.
    for name in _FILL_LAYERS:
        layer = by_name.get(name)
        if layer is None:
            continue
        for feat in layer.features:
            if feat.geom_type != GEOM_POLYGON:
                continue
            if name == "water":
                color, prio = _WATER_FILL
            elif str(feat.get("class") or feat.get("subclass")) in _GREEN_CLASSES:
                color, prio = _GREEN_FILL
            else:
                continue
            vp.fill_polygon(
                [project(r, layer.extent) for r in feat.rings], mark_rgb(color), prio
            )

    # Waterways (rivers/streams) as lines.
    waterway = by_name.get("waterway")
    if waterway is not None:
        for feat in waterway.features:
            style = _WATERWAY_STYLE.get(str(feat.get("class")))
            if style is None or feat.geom_type != GEOM_LINE:
                continue
            color, prio = style
            for ring in feat.rings:
                if len(ring) >= 2:
                    vp.draw_line(project(ring, waterway.extent), mark_rgb(color), prio)
            if feat.name:
                _add_line_label(frame, feat.rings, waterway.extent, z, x, y, feat.name, _WATER_LABEL)

    # Roads / rail.
    transportation = by_name.get("transportation")
    if transportation is not None:
        for feat in transportation.features:
            if feat.geom_type != GEOM_LINE:
                continue
            cls = str(feat.get("class") or "")
            if cls in ("rail", "transit"):
                color, prio = _RAIL
            else:
                color, prio = _ROAD_STYLE.get(cls, _ROAD_DEFAULT)
            rgb = mark_rgb(color)
            for ring in feat.rings:
                if len(ring) >= 2:
                    vp.draw_line(project(ring, transportation.extent), rgb, prio)

    # Boundaries (admin) as a faint hint.
    boundary = by_name.get("boundary")
    if boundary is not None:
        for feat in boundary.features:
            if feat.geom_type != GEOM_LINE:
                continue
            try:
                if int(feat.get("admin_level", 99)) > 6:
                    continue
            except (TypeError, ValueError):
                continue
            for ring in feat.rings:
                if len(ring) >= 2:
                    vp.draw_line(project(ring, boundary.extent), mark_rgb("#6d5f88"), 14)

    # Street-name labels (only kick in at high zoom via the label's min_zoom gate).
    tname = by_name.get("transportation_name")
    if tname is not None:
        color, bold, rank = _STREET_LABEL
        for feat in tname.features:
            if feat.name and feat.rings:
                _add_line_label(
                    frame, feat.rings, tname.extent, z, x, y, feat.name,
                    (color, bold, rank), min_zoom=15,
                )

    # Place labels.
    place = by_name.get("place")
    if place is not None:
        for feat in place.features:
            if not feat.name or not feat.rings or not feat.rings[0]:
                continue
            style = _PLACE_STYLE.get(str(feat.get("class")))
            if style is None:
                continue
            color, bold, rank, min_zoom = style
            lx, ly = feat.rings[0][0]
            dx, dy = frame.viewport.feature_to_dot(x, y, z, place.extent, lx, ly)
            frame.labels.append(
                _Label(rank, dx, dy, feat.name, mark_rgb(color), bold, min_zoom)
            )

    # Water-body names.
    water_name = by_name.get("water_name")
    if water_name is not None:
        color, bold, rank = _WATER_LABEL
        for feat in water_name.features:
            if feat.name and feat.rings and feat.rings[0]:
                lx, ly = feat.rings[0][0]
                dx, dy = frame.viewport.feature_to_dot(x, y, z, water_name.extent, lx, ly)
                frame.labels.append(_Label(rank, dx, dy, feat.name, mark_rgb(color), bold, 8))


def _clip_to_canvas(
    x0: float, y0: float, x1: float, y1: float, w: float, h: float
) -> Optional[tuple[float, float, float, float]]:
    """Trim a segment to the ``0..w`` by ``0..h`` canvas (Liang-Barsky).

    Returns:
        The part of the segment inside the canvas, or ``None`` if none of it is. A street
        that crosses the view with both of its endpoints beyond the edges still yields the
        stretch you can see, which is the whole point of clipping rather than testing the
        endpoints.
    """
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
        if p == 0:
            if q < 0:
                return None  # parallel to this edge and wholly outside it
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)


def _add_line_label(
    frame: _Frame,
    rings: list[list[tuple[int, int]]],
    extent: int,
    z: int,
    x: int,
    y: int,
    text: str,
    style: tuple[str, bool, int],
    *,
    min_zoom: int = 0,
) -> None:
    """Queue a label on the longest stretch of a line feature that is actually on screen.

    Anchoring to the feature's own midpoint — the middle of the street as the *tile* drew
    it — pins the label to a fixed geographic point rather than to your view, and the
    tighter you zoom the less likely that point is to still be on screen. It made street
    names get rarer the further in you went: of 265 named streets in a central Montréal
    tile, 37 anchors landed on a 53x26 canvas at zoom 14 and only 6 at zoom 16.

    So the line is clipped to the canvas first and the label goes on the longest piece
    that survives, with the next-longest pieces kept as alternates for when that spot is
    already spoken for. Nothing is queued for a feature that is wholly off screen.
    """
    vp = frame.viewport
    w, h = float(vp.dot_w), float(vp.dot_h)
    pieces: list[tuple[float, float, float]] = []  # (length, mid x, mid y)
    for ring in rings:
        if len(ring) < 2:
            continue
        prev = vp.feature_to_dot(x, y, z, extent, *ring[0])
        for point in ring[1:]:
            cur = vp.feature_to_dot(x, y, z, extent, *point)
            visible = _clip_to_canvas(prev[0], prev[1], cur[0], cur[1], w, h)
            prev = cur
            if visible is None:
                continue
            ax, ay, bx, by = visible
            pieces.append((math.hypot(bx - ax, by - ay), (ax + bx) / 2, (ay + by) / 2))
    if not pieces:
        return
    pieces.sort(key=lambda p: -p[0])
    color, bold, rank = style
    frame.labels.append(
        _Label(
            rank, pieces[0][1], pieces[0][2], text, mark_rgb(color), bold, min_zoom,
            alts=tuple((px, py) for _, px, py in pieces[1:4]),
        )
    )


def _marker_style(marker: MapMarker) -> tuple[str, str]:
    """The glyph and colour for a marker: self, repeater, or leaf node."""
    if marker.is_self:
        return _SELF
    if marker.is_repeater:
        return _REPEATER
    return _NODE


#: How many piled nodes it takes for a marker's glyph to reach full brightness.
_PILE_FULL = 8


def _pile_color(color: RGB, count: int, *, cap: int = _PILE_FULL) -> RGB:
    """Brighten a marker's colour by how many nodes share its cell.

    One cell can only show a single glyph, so where many nodes fall on the same spot the
    map would otherwise hide the crowd behind one marker. Instead we keep the top node's
    glyph and wash its colour toward white as the pile grows, so a bright marker reads as
    a busy cluster. The ramp is logarithmic (2 nodes → a clear lift, saturating around
    ``cap``) so it stays informative without a lone extra node looking crowded.
    """
    if count <= 1:
        return color
    t = min(1.0, math.log2(count) / math.log2(cap))
    r, g, b = color
    return (
        round(r + (255 - r) * t),
        round(g + (255 - g) * t),
        round(b + (255 - b) * t),
    )


#: How far a node outside an active find filter dims (glyph colour multiplier).
_FIND_DIM = 0.35

#: Label colour for find-filter matches: full white, the brightest thing on the map.
_FIND_MATCH_LABEL: RGB = (255, 255, 255)


def _dimmed(color: RGB, factor: float) -> RGB:
    """``color`` scaled toward black by ``factor``, clamped to byte range."""
    return tuple(max(0, min(255, round(c * factor))) for c in color)  # type: ignore[return-value]


def _draw_nodes(
    canvas: MapCanvas, viewport: Viewport, markers: list[MapMarker], *, find: str = ""
) -> None:
    """Overlay mesh nodes: every glyph, then labels by importance until they collide.

    Glyphs are drawn lowest-priority first so self/repeaters land on top of leaf nodes.
    Where several nodes share a cell the surviving glyph is brightened by the pile size
    (:func:`_pile_color`) so crowded spots glow rather than silently hiding the crowd.
    Labels are then placed highest-priority first — self, then repeaters, then leaf
    nodes — each only if it fits without overlapping. So on a crowded map the important
    labels win the available space and the rest show as a bare marker (no overlap).

    With a ``find`` filter active only the matching nodes keep labels — drawn in bright
    white so they pop — while everything else dims to near-background context and match
    labels never lose the collision contest to non-matches.
    """
    needle = find.casefold()
    placed: list[tuple[MapMarker, int, int, bool]] = []
    for marker in markers:
        x, y = viewport.lonlat_to_dot(marker.lat, marker.lon)
        ix, iy = int(round(x)), int(round(y))
        if not (0 <= ix < viewport.dot_w and 0 <= iy < viewport.dot_h):
            continue
        matched = not needle or needle in marker.label.casefold()
        placed.append((marker, ix, iy, matched))

    # A cell is (dot_x >> 1, dot_y >> 2); count how many nodes land on each.
    pile = Counter((ix >> 1, iy >> 2) for _, ix, iy, _m in placed)

    # Matches draw after (over) dimmed non-matches whatever their rank, so the node
    # being searched for is never buried under a brighter neighbour.
    for marker, ix, iy, matched in sorted(placed, key=lambda p: (p[3], p[0]._rank())):
        glyph, color = _marker_style(marker)
        rgb = parse_hex(color)
        if not matched:
            rgb = _dimmed(rgb, _FIND_DIM)
        else:
            rgb = _pile_color(rgb, pile[(ix >> 1, iy >> 2)])
        canvas.marker(ix, iy, glyph, rgb)

    # A filtered-out node is context: bare dim glyph, no label. Labels take the
    # node's key-derived name hue — the app-wide colour rule — with our own label the
    # pure ``you`` white (the ★ glyph keeps its yellow); an active find filter still
    # forces every match's label full white so the sought node pops.
    from .widgets import name_rgb  # widgets imports this module; late-bind to dodge the cycle

    labelled = (p for p in placed if p[3])
    for marker, ix, iy, _matched in sorted(labelled, key=lambda p: -p[0]._rank()):
        if needle:
            label_color = _FIND_MATCH_LABEL
        elif marker.is_self:
            label_color = (255, 255, 255)  # the `you` white
        else:
            label_color = name_rgb(marker.label, marker.key)
        canvas.marker_label(ix, iy, marker.label, label_color)
