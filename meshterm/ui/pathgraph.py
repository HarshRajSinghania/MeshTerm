"""THE route-graph widget: hop sequences drawn as a tidy left-to-right layered graph.

Extracted from the Message paths dialog so every screen that draws walked (or planned)
routes draws them the same way. A *layer* is one path — its relay hops, an edge colour,
and a draw priority — and the widget lays every distinct path out between a left and a
right endpoint marker.

Rather than fanning the paths as free lanes and averaging shared relays (which pulls
lines into acute tangles wherever many paths meet the two endpoints), the widget draws
the paths as one **layered graph**, the way a subway map or a dependency chart reads:

* **ranks** (columns) are hop distance from the left endpoint — a node sits in the column
  of its *furthest* appearance across the paths, so every edge runs strictly left-to-right
  and a relay shared by several routes is drawn **once**, in one place;
* **long edges** — a path that skips a column (a direct shot past where another route
  stops to relay) — are routed through invisible waypoints at each column it crosses, so
  the line bends around the intervening nodes instead of slicing through them;
* **within a column** the nodes are ordered to minimise crossings (a few barycentre
  sweeps), then spread down the full available height with their vertical positions
  pulled toward the average of their neighbours — straightening each route into a lane
  while keeping the branches far enough apart to leave readable (~60°) angles;
* **shared cells** go to the highest-priority layer (drawn last), so e.g. the Message
  paths dialog's selected path reads white over the unused grey;
* **labels** sit straight above or below their marker — pushed to the side away from the
  graph's middle, a spot clear of the drawn lines preferred — and endpoints slide their
  label inward from the canvas edge so a long name still lands by its marker. A caller
  that wants a node unlabelled returns ``None`` for it.

Everything renders onto a :class:`~meshterm.ui.mapcanvas.MapCanvas`; the caller supplies
the per-node glyph/label/colour callbacks, so the widget stays free of contact-list and
theme concerns.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Callable, Optional, Sequence

from .mapcanvas import RGB, MapCanvas, parse_hex

#: Sentinel node ids for the graph's endpoints (NUL never collides with hex hops).
#: Callers key their glyph/label callbacks on these for the two ends of every path.
SRC_NODE = "\x00src"
DST_NODE = "\x00dst"

#: Prefix for the invisible waypoint ids a long edge is routed through — one per column
#: it crosses. Never handed to the caller's callbacks (waypoints draw line only).
_VIRTUAL_PREFIX = "\x00v"

#: Vertical dot separation aimed for between adjacent nodes sharing a column — the base
#: "lane" height one unit of the layout maps to. Wide enough to seat a marker and its
#: label row; a tall graph compresses it to fit, a sparse one stretches it (bounded by
#: :data:`_MAX_STRETCH`) to spend the height it has on airier, less acute branch angles.
_LANE_STEP_DOTS = 14

#: How far the lane step may stretch beyond :data:`_LANE_STEP_DOTS` when a graph has few
#: rows to fill — enough to open the branch angles up without blowing a two-node fork out
#: to the full height.
_MAX_STRETCH = 1.8

#: Dots reserved beyond the outermost node at each end of the graph — a label row for
#: that node's marker, plus a little air. The box is sized and centred on the graph's
#: real vertical extent (which is rarely symmetric), so it spends its rows on the paths
#: rather than on a mirrored half that stays empty.
_GRAPH_END_DOTS = 8

#: The dot row within a character cell a horizontal edge line is aimed at — the
#: upper-middle of the cell's 2×4 pixel grid (rows 0..3 top-down), where a one-dot
#: line reads as running through the glyph rather than hugging its bottom edge. Every
#: node's y is snapped onto this row (see ``_mid_row``) so same-column runs stay level.
_CELL_MID_DOT = 1

#: Dot-space margin the endpoint markers keep from the canvas edges.
_GRAPH_PAD_DOTS = 6

#: Minimum vertical separation, in layout units, between two nodes in the same column —
#: one lane. The ordering/placement passes never seat two markers closer than this.
_MIN_LANE_GAP = 1.0

#: Sweeps of the crossing-minimisation and vertical-straightening relaxations. The graphs
#: are tiny (a handful of nodes over a handful of columns), so a few passes converge.
_ORDER_SWEEPS = 6
_PLACE_ITERS = 10

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
            path runs endpoint to endpoint straight across).
        color: The edge colour the path draws in.
        priority: Draw priority — where paths share a cell, the highest priority
            keeps it (its edges are also drawn last).
    """

    hops: tuple[str, ...]
    color: RGB
    priority: int


@dataclass
class _LayoutNode:
    """A vertex the layout positions — a real node, an endpoint, or an edge waypoint.

    Attributes:
        node: The id handed to the caller's callbacks (``SRC_NODE``/``DST_NODE`` for the
            endpoints, a hop hash for a relay), or a ``_VIRTUAL_PREFIX`` id for a waypoint.
        rank: The column (hop distance from the left endpoint) the node sits in.
        real: Whether the node carries a marker and label (waypoints are line-only).
        order: Its position within the column, top to bottom (set by the ordering pass).
        y: Its vertical coordinate — layout units during placement, canvas dots after.
    """

    node: str
    rank: int
    real: bool
    order: int = 0
    y: float = 0.0


def _mid_row(y_dot: float) -> int:
    """Snap a dot row onto the upper-middle dot of its character cell.

    A braille cell is four dot rows tall; a horizontal line drawn on the top or
    bottom row hugs the glyph's edge and reads as sitting too high or too low.
    Snapping every node's y to :data:`_CELL_MID_DOT` keeps markers — and the level
    runs between same-column nodes — centred in the cell's pixel space.
    """
    return round((y_dot - _CELL_MID_DOT) / 4) * 4 + _CELL_MID_DOT


def _collapse(layers: Sequence[PathLayer]) -> list[PathLayer]:
    """Fold layers with identical hop sequences onto their highest-priority instance.

    Re-walking a known route highlights it rather than doubling it: the first appearance
    fixes draw order, the strongest priority (and its colour) wins the shared path.
    """
    drawn: list[PathLayer] = []
    by_hops: dict[tuple[str, ...], int] = {}
    for layer in layers:
        at = by_hops.get(layer.hops)
        if at is None:
            by_hops[layer.hops] = len(drawn)
            drawn.append(layer)
        elif layer.priority > drawn[at].priority:
            drawn[at] = PathLayer(layer.hops, layer.color, layer.priority)
    return drawn


_HEX_DIGITS = frozenset("0123456789abcdef")


def _coalesce_prefixes(layers: Sequence[PathLayer]) -> list[PathLayer]:
    """Fold an under-specified hop into the longer id it can only be, across the layers.

    A relay reaches the graph at different hash widths across the paths — a 1-byte trace
    hop (``be``) beside the same node's wider id (``be1d1c1dbc4b``). Drawn as-is they double
    the node: two markers a column apart, often carrying the same resolved name. The
    observed-topology graph coalesces what it can, but only where a hash is unambiguous
    against the *whole* contact list; a hash that opens two contacts (``be`` also begins
    ``bedd2b``) survives to here, even though among *these* paths it can only be the one wide
    node present. So the widget closes the gap locally: using only the ids the layers hold,
    a short id is rewritten to the longer id it strictly prefixes when exactly one such id is
    present (following a chain to its longest end); a short id that prefixes two distinct
    present nodes is genuinely ambiguous and left as itself. The per-node callbacks then see
    one id per node, and any paths made identical by the rewrite collapse together downstream.
    """
    ids = {hop for layer in layers for hop in layer.hops}
    remap: dict[str, str] = {}
    remaining = set(ids)
    while True:
        pair = _prefix_merge(remaining)
        if pair is None:
            break
        short, long = pair
        remap[short] = long
        remaining.discard(short)
    if not remap:
        return list(layers)

    def resolved(hop: str) -> str:
        for _ in range(len(remap) + 1):  # follow a rewrite chain, guarded against cycles
            if hop not in remap:
                break
            hop = remap[hop]
        return hop

    return [
        PathLayer(tuple(resolved(hop) for hop in layer.hops), layer.color, layer.priority)
        for layer in layers
    ]


def _prefix_merge(nodes: set[str]) -> Optional[tuple[str, str]]:
    """The next ``(short, long)`` id pair to fold among ``nodes``, or ``None`` when none.

    A short hex id (under a full 6-byte width) folds when the longer present ids that
    extend it all lie on one prefix chain — the longest starts with every other — so it can
    only name that one node. Shortest ids are offered first, collapsing a chain from its end.
    """
    for short in sorted(nodes, key=len):
        if len(short) >= 12 or not _is_hex(short):
            continue
        exts = [
            other
            for other in nodes
            if other != short and len(other) > len(short)
            and _is_hex(other) and other.startswith(short)
        ]
        if not exts:
            continue
        longest = max(exts, key=len)
        if all(longest.startswith(ext) for ext in exts):
            return short, longest
    return None


def _is_hex(value: str) -> bool:
    """Whether every character of ``value`` is a hex digit (endpoint sentinels are not)."""
    return bool(value) and all(char in _HEX_DIGITS for char in value)


def _pava(values: list[float]) -> list[float]:
    """Pool-adjacent-violators: the nearest non-decreasing sequence to ``values`` (L2).

    The engine behind :func:`_pack` — it fits a monotone curve to the desired positions,
    merging any run that would step backwards into its weighted mean.
    """
    means: list[float] = []
    counts: list[int] = []
    for value in values:
        means.append(value)
        counts.append(1)
        while len(means) > 1 and means[-2] > means[-1]:
            m2, c2 = means.pop(), counts.pop()
            m1, c1 = means.pop(), counts.pop()
            means.append((m1 * c1 + m2 * c2) / (c1 + c2))
            counts.append(c1 + c2)
    out: list[float] = []
    for mean, count in zip(means, counts):
        out.extend([mean] * count)
    return out


def _pack(desired: list[float], gap: float) -> list[float]:
    """Place ordered nodes as close to their desired ys as a minimum gap allows.

    Given each node's preferred y (the average of its neighbours) in column order, return
    ys that stay in that order with at least ``gap`` between neighbours and minimise the
    total squared shift — the optimal one-dimensional separation, via isotonic regression:
    subtract the running gap, fit a monotone curve (:func:`_pava`), add the gap back.
    """
    if not desired:
        return []
    shifted = [value - i * gap for i, value in enumerate(desired)]
    fitted = _pava(shifted)
    return [value + i * gap for i, value in enumerate(fitted)]


def render_path_graph(
    layers: Sequence[PathLayer],
    width: int,
    *,
    glyph_of: GlyphOf,
    label_of: LabelOf,
    label_rgb_of: LabelRgbOf,
    min_rows: int = 5,
    max_rows: int = 15,
    lane_step: int = _LANE_STEP_DOTS,
) -> list[str]:
    """Draw the layered route graph and return its ANSI lines.

    Args:
        layers: The paths to draw, in draw order (later layers, and higher priorities,
            win shared cells). Layers with identical hop sequences collapse to one drawn
            path owned by the highest priority among them.
        width: Canvas width in character cells.
        glyph_of: Marker glyph + colour per node id (endpoints keyed by
            :data:`SRC_NODE` / :data:`DST_NODE`).
        label_of: Label text per node id (``None``/``""`` = bare marker). A label is
            kept whole unless it is wider than the canvas, when it is ellipsized to fit.
        label_rgb_of: Label colour per node id.
        min_rows: The fewest canvas rows to draw, however flat the graph.
        max_rows: The most canvas rows to spend; a taller graph compresses its lane
            spacing to fit.
        lane_step: Vertical dot separation aimed for between column-mates. A smaller step
            packs the graph into a shallower band; a larger one opens the branch angles
            up. Bounded by the row budget either way. Defaults to :data:`_LANE_STEP_DOTS`.

    Returns:
        One ANSI string per canvas row (empty when there are no layers to draw).
    """
    if not layers:
        return []

    drawn = _collapse(_coalesce_prefixes(layers))
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]

    # First-appearance order for every node and edge, so the layout is identical on every
    # repaint: a set of hash-seeded string ids would iterate in a run-varying order and let
    # the ordering pass settle differently each time, making the graph jump between frames.
    ordered_nodes: list[str] = []
    ordered_edges: list[tuple[str, str]] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str]] = set()
    for seq in seqs:
        for node in seq:
            if node not in seen_nodes:
                seen_nodes.add(node)
                ordered_nodes.append(node)
        for u, v in zip(seq, seq[1:]):
            if (u, v) not in seen_edges:
                seen_edges.add((u, v))
                ordered_edges.append((u, v))

    # -- Ranks (columns): the longest path (in edges) from the source to each node. Ranking
    # by longest path — not merely a node's furthest position in some one sequence — is what
    # guarantees every edge steps strictly forward (``rank(v) ≥ rank(u) + 1``): a relay that
    # one route reaches late is seated in the deeper column, so no edge ever doubles back or
    # runs *within* a column (which would draw as a stray vertical bar). The right endpoint,
    # the sink of every path, takes the deepest rank of all. Relaxed to a fixed point, capped
    # at the node count so a pathological order-flipped pair can't spin it forever.
    rank: dict[str, int] = {node: 0 for node in ordered_nodes}
    for _ in range(len(ordered_nodes)):
        changed = False
        for u, v in ordered_edges:
            if rank[v] < rank[u] + 1:
                rank[v] = rank[u] + 1
                changed = True
        if not changed:
            break
    max_rank = max(rank.values())

    # -- Edges routed. An edge that spans more than one column is broken over invisible
    # waypoints, one per crossed column, so the drawn line bends around the nodes between its
    # ends instead of cutting across them. ``route`` maps each directed edge to the full
    # waypoint chain the drawing pass threads its line through.
    route: dict[tuple[str, str], list[str]] = {}
    nodes: dict[str, _LayoutNode] = {}

    def ensure(node: str, node_rank: int, real: bool) -> None:
        if node not in nodes:
            nodes[node] = _LayoutNode(node=node, rank=node_rank, real=real)

    for node in ordered_nodes:
        ensure(node, rank[node], real=True)

    virtual = 0
    for u, v in ordered_edges:
        chain = [u]
        for r in range(rank[u] + 1, rank[v]):
            vid = f"{_VIRTUAL_PREFIX}{virtual}"
            virtual += 1
            ensure(vid, r, real=False)
            chain.append(vid)
        chain.append(v)
        route[(u, v)] = chain

    columns: dict[int, list[str]] = defaultdict(list)
    for node in nodes.values():
        columns[node.rank].append(node.node)

    # -- Adjacency over the routed (waypoint-inclusive) graph, for ordering + straightening.
    adjacent: dict[str, list[str]] = defaultdict(list)
    for chain in route.values():
        for a, b in zip(chain, chain[1:]):
            adjacent[a].append(b)
            adjacent[b].append(a)

    _order_columns(columns, adjacent, nodes)
    extent = _place_columns(columns, adjacent, nodes)

    # -- Vertical sizing: fill the row budget with the graph's real extent, stretching a
    # sparse graph (bounded) for airier angles and compressing a busy one to fit.
    if extent <= 0:
        step = 0.0
        rows = min_rows
    else:
        ideal = extent * lane_step + 2 * _GRAPH_END_DOTS
        rows = max(min_rows, min(max_rows, ceil(ideal / 4)))
        avail = rows * 4 - 2 * _GRAPH_END_DOTS
        step = min(avail / extent, lane_step * _MAX_STRETCH)

    canvas = MapCanvas(width, rows)
    dot_w = width * 2
    span = dot_w - 2 * _GRAPH_PAD_DOTS
    min_y = min(node.y for node in nodes.values())
    band = extent * step
    top = (rows * 4 - band) / 2  # centre the real extent in the chosen rows

    def x_of(node_rank: int) -> int:
        return _GRAPH_PAD_DOTS + round(node_rank / max_rank * span) if max_rank else dot_w // 2

    pos: dict[str, tuple[int, int]] = {}
    for node in nodes.values():
        pos[node.node] = (x_of(node.rank), _mid_row(top + (node.y - min_y) * step))
    mid_y = rows * 2  # canvas vertical middle, in dots — the side labels push away from

    # -- Edges: ascending priority, so where paths share cells the top layer draws last
    # and keeps them. Each layer threads its line through the waypoint chain of every edge.
    for layer, seq in sorted(zip(drawn, seqs), key=lambda ls: ls[0].priority):
        for u, v in zip(seq, seq[1:]):
            points = [pos[node] for node in route[(u, v)]]
            canvas.draw_line(points, layer.color, layer.priority)

    # -- Markers for every real node; waypoints stay line-only.
    for node in nodes.values():
        if not node.real:
            continue
        glyph, color_hex = glyph_of(node.node)
        canvas.marker(*pos[node.node], glyph, parse_hex(color_hex))

    _place_labels(canvas, nodes, pos, width, mid_y, label_of, label_rgb_of)
    return canvas.to_ansi_lines()


def _order_columns(
    columns: dict[int, list[str]],
    adjacent: dict[str, list[str]],
    nodes: dict[str, _LayoutNode],
) -> None:
    """Order the nodes within each column to reduce edge crossings (barycentre sweeps).

    Starting from first-appearance order, each node is repeatedly re-seated at the mean
    position of its neighbours in the column just settled — down the ranks, then up —
    which is the standard cheap crossing-minimiser. A node with no neighbour in the
    reference column keeps its place. The results are written back as each node's
    ``order`` (its index within the column).
    """
    order: dict[str, int] = {}
    for column in columns.values():
        for i, node in enumerate(column):
            order[node] = i

    max_rank = max(columns)

    def barycentre(node: str, ref_rank: int) -> float:
        refs = [order[n] for n in adjacent[node] if nodes[n].rank == ref_rank]
        return sum(refs) / len(refs) if refs else float(order[node])

    for _ in range(_ORDER_SWEEPS):
        for r in range(1, max_rank + 1):
            columns[r].sort(key=lambda n: barycentre(n, r - 1))  # noqa: B023 - r is bound per loop
            for i, node in enumerate(columns[r]):
                order[node] = i
        for r in range(max_rank - 1, -1, -1):
            columns[r].sort(key=lambda n: barycentre(n, r + 1))  # noqa: B023 - r is bound per loop
            for i, node in enumerate(columns[r]):
                order[node] = i

    for column in columns.values():
        for i, node in enumerate(column):
            nodes[node].order = i


def _place_columns(
    columns: dict[int, list[str]],
    adjacent: dict[str, list[str]],
    nodes: dict[str, _LayoutNode],
) -> float:
    """Assign each node a vertical coordinate (layout units) and return the total extent.

    Seeded from the column orders, every node is pulled toward the average height of its
    neighbours and its column re-separated to the minimum lane gap (:func:`_pack`) — a few
    passes each way. This straightens each route into a near-level lane while keeping
    branches apart; an endpoint, being the lone node in its column, drifts to the centroid
    of its own branches so the whole fan reads as leaving (or arriving at) one point.
    Returns ``max_y - min_y`` across all nodes, the height the caller scales to the canvas.
    """
    for column in columns.values():
        for node in column:
            nodes[node].y = float(nodes[node].order)

    max_rank = max(columns)
    ranks_desc = list(range(max_rank + 1))

    def relax(order_of_ranks: list[int]) -> None:
        for r in order_of_ranks:
            column = columns[r]
            desired = []
            for node in column:
                neighbours = adjacent[node]
                if neighbours:
                    desired.append(sum(nodes[n].y for n in neighbours) / len(neighbours))
                else:
                    desired.append(nodes[node].y)
            for node, y in zip(column, _pack(desired, _MIN_LANE_GAP)):
                nodes[node].y = y

    for _ in range(_PLACE_ITERS):
        relax(ranks_desc)
        relax(ranks_desc[::-1])

    ys = [node.y for node in nodes.values()]
    return max(ys) - min(ys)


def _place_labels(
    canvas: MapCanvas,
    nodes: dict[str, _LayoutNode],
    pos: dict[str, tuple[int, int]],
    width: int,
    mid_y: int,
    label_of: LabelOf,
    label_rgb_of: LabelRgbOf,
) -> None:
    """Write each real node's label above or below its marker, clear of the lines if it can.

    Endpoints first, then relays column by column, so the named ends win any contest for a
    cell. Each label is tried on the side away from the graph's middle first (spreading the
    text outward off the busy centre), preferring a row the drawn lines don't already
    occupy; an endpoint, hard against a canvas edge, slides its anchor inward far enough for
    the whole name to land, and falls back to sitting beside its marker. A name is only
    ever shortened when it is wider than the entire canvas.
    """
    endpoints = [n for n in (DST_NODE, SRC_NODE) if n in nodes]
    relays = [
        layout.node
        for layout in sorted(nodes.values(), key=lambda n: (n.rank, n.order))
        if layout.real and layout.node not in (SRC_NODE, DST_NODE)
    ]
    for node in [*endpoints, *relays]:
        label = label_of(node)
        if not label:
            continue
        if len(label) > width:
            label = label[: max(1, width - 1)] + "…"
        rgb = label_rgb_of(node)
        x, y = pos[node]
        anchor_x = x
        if node in (SRC_NODE, DST_NODE):
            half = len(label) // 2
            cx = min(max(x >> 1, half), max(half, width - (len(label) - half)))
            anchor_x = cx * 2
        rows_out = (y - 4, y + 4) if y <= mid_y else (y + 4, y - 4)
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
