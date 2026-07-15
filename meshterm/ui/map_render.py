"""Compose a rendered map frame: basemap geometry, labels, and mesh nodes.

Given a :class:`~meshterm.core.geo.Viewport` and the decoded vector tiles covering it, this
projects every street, river, water body and place label onto a :class:`MapCanvas`, then
overlays the mesh nodes with their names on top. It is pure and synchronous — tile *fetching*
happens elsewhere (:mod:`meshterm.services.basemap`) — so both the interactive screen and
the one-shot CLI render call the same code.

Colours target a dark terminal (the app theme): warm roads, grey minor streets, blue water
and rivers, faint green parks. Because a cell shows one colour, draw priorities keep the
important feature visible where things overlap (rivers over water, major roads over minor).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from ..core.geo import Viewport
from ..core.mvt import GEOM_LINE, GEOM_POLYGON, Layer
from .mapcanvas import RGB, MapCanvas, parse_hex

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
    """

    label: str
    lat: float
    lon: float
    is_repeater: bool = False
    is_self: bool = False
    detail: str = ""

    def _rank(self) -> int:
        """Draw order: self on top of repeaters on top of leaf nodes."""
        return 2 if self.is_self else (1 if self.is_repeater else 0)


# Marker palette, shared across the whole app. The basemap is blue (water), green (parks) and
# warm amber (roads), so markers use the hues a map never contains — pink/violet — to stand
# out against it. Ordinary nodes are the loudest (bright pink); repeaters recede a step (a
# calmer violet); our own node stays the fixed yellow star; a node heard of but never
# identified is a muted grey ring.
_SELF = ("★", "#facc15")
_REPEATER = ("▲", "#a78bfa")
_NODE = ("●", "#f472b6")
_UNKNOWN = ("○", "#94a3b8")


# -- basemap styling ----------------------------------------------------------

# Road class -> (colour, priority). Higher priority wins a shared cell.
_ROAD_STYLE: dict[str, tuple[str, int]] = {
    "motorway": ("#f2a13d", 27),
    "trunk": ("#f2a13d", 26),
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
    "river": ("#49b0ec", 30),
    "canal": ("#49b0ec", 30),
    "stream": ("#3f8fbf", 29),
    "ditch": ("#3a7ba6", 28),
    "drain": ("#3a7ba6", 28),
}
_WATER_FILL = ("#153b56", 6)
_GREEN_FILL = ("#173a29", 4)
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


@dataclass(slots=True)
class _Label:
    """A pending label placement candidate."""

    rank: int
    x: float
    y: float
    text: str
    color: RGB
    bold: bool
    min_zoom: int = 0


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
    placed = 0
    for label in sorted(frame.labels, key=lambda l: l.rank):
        if placed >= max_labels:
            break
        if viewport.zoom < label.min_zoom:
            continue
        if canvas.place_label(label.x, label.y, label.text, label.color, bold=label.bold):
            placed += 1

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
    for name in ("water", "landcover", "landuse", "park"):
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
                [project(r, layer.extent) for r in feat.rings], parse_hex(color), prio
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
                    vp.draw_line(project(ring, waterway.extent), parse_hex(color), prio)
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
            rgb = parse_hex(color)
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
                    vp.draw_line(project(ring, boundary.extent), parse_hex("#6d5f88"), 14)

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
                _Label(rank, dx, dy, feat.name, parse_hex(color), bold, min_zoom)
            )

    # Water-body names.
    water_name = by_name.get("water_name")
    if water_name is not None:
        color, bold, rank = _WATER_LABEL
        for feat in water_name.features:
            if feat.name and feat.rings and feat.rings[0]:
                lx, ly = feat.rings[0][0]
                dx, dy = frame.viewport.feature_to_dot(x, y, z, water_name.extent, lx, ly)
                frame.labels.append(_Label(rank, dx, dy, feat.name, parse_hex(color), bold, 8))


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
    """Queue a label at the midpoint of a line feature's longest part."""
    longest = max(rings, key=len)
    lx, ly = longest[len(longest) // 2]
    dx, dy = frame.viewport.feature_to_dot(x, y, z, extent, lx, ly)
    color, bold, rank = style
    frame.labels.append(_Label(rank, dx, dy, text, parse_hex(color), bold, min_zoom))


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

    # A filtered-out node is context: bare dim glyph, no label.
    labelled = (p for p in placed if p[3])
    for marker, ix, iy, _matched in sorted(labelled, key=lambda p: -p[0]._rank()):
        _, color = _marker_style(marker)
        label_color = _FIND_MATCH_LABEL if needle else parse_hex(color)
        canvas.marker_label(ix, iy, marker.label, label_color)
