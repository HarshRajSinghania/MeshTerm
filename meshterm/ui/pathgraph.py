"""THE route-graph widget: hop sequences drawn as fanned braille lanes between us-es.

Extracted from the Message paths dialog so every screen that draws walked (or planned)
routes draws them the same way. A *layer* is one path — its relay hops, an edge colour,
and a draw priority — and the widget lays every distinct path out between a left and a
right endpoint marker:

* **columns** are the distinct node depths, spread evenly across the width, so forks
  read clearly however lopsided the paths' lengths are;
* **lanes** fan out from the vertical centre in layer order (first layer on the centre
  line, the next below, the next above, …); a node shared between paths averages its
  lanes, so a common relay pulls the paths together where they actually met;
* **shared cells** go to the highest-priority layer (drawn last), so e.g. the Message
  paths dialog's selected path reads white over the unused grey, and the Pathfinder's
  current attempt reads white over the session's green over the all-time yellow;
* **labels** sit straight above or below their marker — the side away from the centre
  line first, a spot clear of the drawn lines preferred — and endpoints try the same
  vertical placements before falling back beside the marker. A caller that wants a
  node unlabelled (the Pathfinder names only our own node) returns ``None`` for it.

Everything renders onto a :class:`~meshterm.ui.mapcanvas.MapCanvas`; the caller supplies
the per-node glyph/label/colour callbacks, so the widget stays free of contact-list and
theme concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Callable, Optional, Sequence

from .mapcanvas import RGB, MapCanvas, parse_hex

#: Sentinel node ids for the graph's endpoints (NUL never collides with hex hops).
#: Callers key their glyph/label callbacks on these for the two ends of every path.
SRC_NODE = "\x00src"
DST_NODE = "\x00dst"

#: Vertical dot separation between path lanes.
_LANE_STEP_DOTS = 12

#: Dots reserved beyond the outermost lane at each end of the fan — a label row for
#: that lane's marker, plus a little air. The box is sized and centred on the fan's
#: real extent (which is rarely symmetric), so it spends its rows on the paths rather
#: than on a mirrored half that stays empty.
_GRAPH_END_DOTS = 8

#: The dot row within a character cell a horizontal edge line is aimed at — the
#: upper-middle of the cell's 2×4 pixel grid (rows 0..3 top-down), where a one-dot
#: line reads as running through the glyph rather than hugging its bottom edge. Every
#: node's y is snapped onto this row (see ``pos``) so same-lane runs stay level there.
_CELL_MID_DOT = 1

#: Dot-space margin the endpoint markers keep from the canvas edges.
_GRAPH_PAD_DOTS = 6

#: The widest a node label may render on the graph before it is ellipsized.
_GRAPH_LABEL_W = 12

#: A node's graph marker: the glyph and its ``#rrggbb`` colour (the shared node-glyph
#: tuples from :mod:`~meshterm.ui.map_render` / :mod:`~meshterm.ui.widgets` fit as-is).
GlyphOf = Callable[[str], tuple[str, str]]

#: A node's graph label, or ``None``/``""`` to leave the marker bare.
LabelOf = Callable[[str], Optional[str]]

#: The colour a node's label is drawn in (usually the node's name hue).
LabelRgbOf = Callable[[str], RGB]


@dataclass(frozen=True)
class PathLayer:
    """One path drawn on the route graph.

    Attributes:
        hops: The relay node ids between the endpoints, in walk order (empty = the
            path runs endpoint to endpoint on the centre line).
        color: The edge colour the path draws in.
        priority: Draw priority — where paths share a cell, the highest priority
            keeps it (its edges are also drawn last).
    """

    hops: tuple[str, ...]
    color: RGB
    priority: int


def _mid_row(y_dot: int) -> int:
    """Snap a dot row onto the upper-middle dot of its character cell.

    A braille cell is four dot rows tall; a horizontal line drawn on the top or
    bottom row hugs the glyph's edge and reads as sitting too high or too low.
    Snapping every node's y to :data:`_CELL_MID_DOT` keeps markers — and the level
    runs between same-lane nodes — centred in the cell's pixel space.
    """
    return round((y_dot - _CELL_MID_DOT) / 4) * 4 + _CELL_MID_DOT


def render_path_graph(
    layers: Sequence[PathLayer],
    width: int,
    *,
    glyph_of: GlyphOf,
    label_of: LabelOf,
    label_rgb_of: LabelRgbOf,
    min_rows: int = 5,
    max_rows: int = 15,
) -> list[str]:
    """Draw the layered route graph and return its ANSI lines.

    Args:
        layers: The paths to draw, in *lane* order (the first rides the centre line,
            the next fans below, the next above, …). Layers with identical hop
            sequences collapse to one drawn path owned by the highest priority among
            them — re-walking a known route highlights it rather than doubling it.
        width: Canvas width in character cells.
        glyph_of: Marker glyph + colour per node id (endpoints keyed by
            :data:`SRC_NODE` / :data:`DST_NODE`).
        label_of: Label text per node id (``None``/``""`` = bare marker). Labels
            longer than the widget's budget are ellipsized.
        label_rgb_of: Label colour per node id.
        min_rows: The fewest canvas rows to draw, however flat the fan.
        max_rows: The most canvas rows to spend; a taller fan compresses its lane
            spacing to fit.

    Returns:
        One ANSI string per canvas row (empty when there are no layers to draw).
    """
    if not layers:
        return []

    # Collapse identical paths onto their highest-priority layer, preserving first
    # appearance for lane order; then fan lanes over the relayed paths (a direct
    # path rides the centre line anyway): 0, -1, 1, -2, 2…
    drawn: list[PathLayer] = []
    by_hops: dict[tuple[str, ...], int] = {}
    for layer in layers:
        at = by_hops.get(layer.hops)
        if at is None:
            by_hops[layer.hops] = len(drawn)
            drawn.append(layer)
        elif layer.priority > drawn[at].priority:
            drawn[at] = PathLayer(layer.hops, layer.color, layer.priority)
    relayed = [layer.hops for layer in drawn if layer.hops]
    lane_of = {hops: (-1) ** k * ((k + 1) // 2) for k, hops in enumerate(relayed)}

    # Positions: a node's depth is its mean relative slot along the paths through
    # it; the distinct depths then map to evenly spaced columns (endpoints pinned
    # to the margins). y is the mean lane, centre-pinned for the endpoints — a
    # shared relay averages toward the middle.
    rel: dict[str, list[float]] = {}
    lanes: dict[str, list[int]] = {}
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]
    for layer, seq in zip(drawn, seqs):
        hops = len(seq) - 1
        for i, node in enumerate(seq):
            rel.setdefault(node, []).append(i / hops)
            lanes.setdefault(node, []).append(lane_of.get(layer.hops, 0))
    depth = {node: sum(r) / len(r) for node, r in rel.items()}
    columns = sorted(set(depth.values()))
    slot = {d: i / max(1, len(columns) - 1) for i, d in enumerate(columns)}
    lane_y = {
        node: sum(l) / len(l)
        for node, l in lanes.items()
        if node not in (SRC_NODE, DST_NODE)
    }

    # Height follows where the relays actually land after lane-averaging — and how
    # far the fan reaches *each* way, which is rarely symmetric (lanes fan 0, −1, +1,
    # −2, …). Sizing to the real up/down reach and centring the endpoints' lane-0
    # line on it keeps the box off the empty half a mirrored block would leave.
    step = _LANE_STEP_DOTS
    ups = -min([0.0, *lane_y.values()])   # lanes rising above the lane-0 line
    downs = max([0.0, *lane_y.values()])  # …and dropping below it

    def sized(step: int) -> tuple[int, int]:
        return (round(ups * step) + _GRAPH_END_DOTS,
                round(downs * step) + _GRAPH_END_DOTS)

    top, bot = sized(step)
    rows = max(min_rows, ceil((top + bot) / 4))
    if rows > max_rows:
        rows = max_rows
        reach = ups + downs
        if reach:
            step = max(4, int((rows * 4 - 2 * _GRAPH_END_DOTS) / reach))
        top, bot = sized(step)
    canvas = MapCanvas(width, rows)
    dot_w = width * 2
    cy = _mid_row(top)
    span = dot_w - 2 * _GRAPH_PAD_DOTS

    def pos(node: str) -> tuple[int, int]:
        x = _GRAPH_PAD_DOTS + round(slot[depth[node]] * span)
        if node in (SRC_NODE, DST_NODE):
            return x, cy
        return x, _mid_row(cy + round(lane_y[node] * step))

    # Edges: ascending priority, so where paths share cells the top layer is drawn
    # last and keeps them.
    for layer, seq in sorted(zip(drawn, seqs), key=lambda ls: ls[0].priority):
        for i in range(len(seq) - 1):
            canvas.draw_line([pos(seq[i]), pos(seq[i + 1])], layer.color, layer.priority)

    # Markers for every node; labels for the nodes the caller names. Endpoints then
    # the topmost layer's hops go first so they win collisions. Every label sits
    # straight above or below its marker — the side away from the centre line first,
    # a spot clear of the drawn lines preferred — with a beside-the-marker placement
    # as the endpoints' last resort (their edge columns can starve a centred run).
    for node in rel:
        glyph, color_hex = glyph_of(node)
        canvas.marker(*pos(node), glyph, parse_hex(color_hex))
    labelled = [DST_NODE, SRC_NODE]
    for layer in sorted(drawn, key=lambda l: -l.priority):
        for hop in layer.hops:
            if hop not in labelled:
                labelled.append(hop)
    for node in labelled:
        label = label_of(node)
        if not label:
            continue
        if len(label) > _GRAPH_LABEL_W:
            label = label[: _GRAPH_LABEL_W - 1] + "…"
        rgb = label_rgb_of(node)
        x, y = pos(node)
        # An endpoint hugs a canvas edge, where a run centred on its marker falls
        # off; its label anchor slides inward just far enough to fit, so the name
        # still lands above/below the star instead of being dropped.
        anchor_x = x
        if node in (SRC_NODE, DST_NODE):
            half = len(label) // 2
            cx = min(max(x >> 1, half), max(half, width - (len(label) - half)))
            anchor_x = cx * 2
        rows_out = (y - 4, y + 4) if y <= cy else (y + 4, y - 4)
        if any(
            canvas.place_label(anchor_x, sy, label, rgb, bold=True, avoid_dots=True)
            for sy in rows_out
        ):
            continue
        if any(
            canvas.place_label(anchor_x, sy, label, rgb, bold=True) for sy in rows_out
        ):
            continue
        if node in (SRC_NODE, DST_NODE):
            if not canvas.marker_label(x, y, label, rgb, avoid_dots=True):
                canvas.marker_label(x, y, label, rgb)
    return canvas.to_ansi_lines()
