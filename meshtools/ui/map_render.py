"""Compose a rendered map frame: basemap geometry, labels, and mesh nodes.

Given a :class:`~meshtools.core.geo.Viewport` and the decoded vector tiles covering it, this
projects every street, river, water body and place label onto a :class:`MapCanvas`, then
overlays the mesh nodes with their names on top. It is pure and synchronous — tile *fetching*
happens elsewhere (:mod:`meshtools.services.basemap`) — so both the interactive screen and
the one-shot CLI render call the same code.

Colours target a dark terminal (the app theme): warm roads, grey minor streets, blue water
and rivers, faint green parks. Because a cell shows one colour, draw priorities keep the
important feature visible where things overlap (rivers over water, major roads over minor).
"""

from __future__ import annotations

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


_SELF = ("★", "#ffffff")
_REPEATER = ("▲", "#ff5d73")
_NODE = ("●", "#22d3ee")


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
) -> list[str]:
    """Render a full map frame to truecolour ANSI lines.

    Args:
        viewport: The view to render (its dot size sets the canvas size).
        tiles: Decoded layers keyed by ``(z, x, y)``; ``None`` values are pending/absent.
        markers: Mesh nodes to overlay.
        max_labels: Cap on basemap labels placed, to keep the map readable.

    Returns:
        One ANSI string per row, ready for the TUI frame or the console.
    """
    canvas = MapCanvas(viewport.dot_w // 2, viewport.dot_h // 4)
    frame = _Frame(canvas=canvas, viewport=viewport)

    for (z, x, y), layers in tiles.items():
        if layers:
            _draw_tile(frame, layers, z, x, y)

    # Nodes reserve their cells first so basemap labels route around them.
    _draw_nodes(canvas, viewport, markers)

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


def _draw_nodes(canvas: MapCanvas, viewport: Viewport, markers: list[MapMarker]) -> None:
    """Overlay mesh nodes (lowest priority first, so self/repeaters land on top)."""
    for marker in sorted(markers, key=lambda m: m._rank()):
        x, y = viewport.lonlat_to_dot(marker.lat, marker.lon)
        ix, iy = int(round(x)), int(round(y))
        if not (0 <= ix < viewport.dot_w and 0 <= iy < viewport.dot_h):
            continue
        if marker.is_self:
            glyph, color = _SELF
        elif marker.is_repeater:
            glyph, color = _REPEATER
        else:
            glyph, color = _NODE
        canvas.marker(ix, iy, glyph, parse_hex(color), label=marker.label)
