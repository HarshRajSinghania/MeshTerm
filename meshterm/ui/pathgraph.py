"""THE route-graph widget: hop sequences drawn as a left-to-right flow of paths.

Extracted from the Message paths dialog so every screen that draws walked (or planned)
routes draws them the same way. A *layer* is one path — its relay hops, an edge colour,
and a draw priority — and the widget lays every distinct path out between a shared left
and right endpoint marker.

Every path shares its two endpoints (an origin on the left, us on the right) and is walked
left to right, so the picture is a *flow*: routes that **diverge** out of the origin, run
their own course, and **converge** back into us, sharing a relay wherever their walks agree.
Earlier drawings tried a vertical *bus* the lanes tapped at right angles (a metro map you
wander around, the bare 90° turns hiding that A flows to B), then oblique branches straight
off each marker (which left every off-lane relay a pointed *peak* or *valley*). The widget
now draws the fan as a **multilane highway**: a node always sits on a level platform in its
lane, and a route changes lane only *between* nodes, easing across on a single gentle shift
the way a car drifts one lane over and then stays there. Concretely:

* **lanes** — each distinct path owns one horizontal lane; the highest-priority (drawn last,
  e.g. the selected or best-evidence) path takes the centre lane, running dead straight
  through the two endpoints as the flow's spine, and the alternatives lie above and below it.
  The lanes are ordered to seat routes that share relays near each other, so a shared hop
  costs the shortest possible detour;
* **columns** (x) place each node by *balanced* rank — its distance from the origin over its
  distance-plus-remaining-distance to us — so a path's relays spread evenly between the two
  ends however long the other paths are, and a shared relay lands in one place;
* **platforms** — every node is entered and left along a level run in its own lane, so a
  relay that sits off its neighbours' lane reads as a flat-topped *trapezium*, never a
  pointed peak or valley. Between two nodes on different lanes the line stays level out of the
  first, makes one oblique shift, and runs level into the second — the only corners are the
  soft level→oblique bends of the shift, never a bare right angle in open canvas;
* **shared relays** draw as a single marker (a route re-using a hop is not a new node): the
  marker sits on its highest-priority path's lane, and a lower path that also rides it leans
  off its lane to meet the platform and back — which reads as the alternative *branching
  through the shared node*, exactly the story the evidence tells;
* **diverge / converge** — the fan at each end is flow, not a switchboard: routes leave the
  origin overlapping on the centre line and peel off one by one to their lanes (the
  divergence), and mirror that back into us on the right (the convergence). Every edge draws
  exactly once, however many routes share it; the one edge drawn as a bare vertical is a pair
  of nodes walked in *both* directions — a genuine two-way hop, the sole place a straight
  up-and-down line tells the truth;
* **labels** sit straight above or below their marker — pushed to the side away from the
  graph's middle, a spot clear of the drawn lines preferred — and endpoints slide their
  label inward from the canvas edge so a long name still lands by its marker. A caller that
  wants a node unlabelled returns ``None`` for it.

Everything renders onto a :class:`~meshterm.ui.mapcanvas.MapCanvas`; the caller supplies
the per-node glyph/label/colour callbacks, so the widget stays free of contact-list and
theme concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from math import ceil
from typing import Callable, Optional, Sequence

from .mapcanvas import RGB, MapCanvas, parse_hex

#: Sentinel node ids for the graph's endpoints (NUL never collides with hex hops).
#: Callers key their glyph/label callbacks on these for the two ends of every path.
SRC_NODE = "\x00src"
DST_NODE = "\x00dst"

#: Vertical dot separation aimed for between adjacent lanes — the base height one lane maps
#: to. Wide enough to seat a marker and its label row; a graph with many lanes compresses it
#: to fit the row budget, and a graph with few lanes never stretches *past* it (spreading the
#: lanes further apart would only reintroduce the empty space the layout exists to avoid), so
#: a sparse graph draws compact rather than splayed.
_LANE_STEP_DOTS = 14

#: Dots reserved beyond the outermost lane at each end of the graph — a label row for that
#: lane's markers, plus a little air.
_GRAPH_END_DOTS = 8

#: The dot row within a character cell a horizontal edge line is aimed at — the upper-middle
#: of the cell's 2×4 pixel grid (rows 0..3 top-down), where a one-dot line reads as running
#: through the glyph rather than hugging its bottom edge. Every lane's y is snapped onto this
#: row (see :func:`_mid_row`) so a level run stays level and same-column markers align.
_CELL_MID_DOT = 2

#: Dot-space margin the endpoint markers keep from the canvas edges.
_GRAPH_PAD_DOTS = 6

#: The share of a lane change's horizontal column gap spent on the eased curve itself — the
#: rest is split into the two level platforms that flank it, where the nodes sit. At ``1.0`` the
#: whole gap curves (the platforms vanish and successive shifts run into one another); a smaller
#: share lengthens the flat platforms and steepens the (now shorter) curve between them. Sizing
#: the curve as a fraction of the gap — rather than by a fixed slope keyed off the vertical
#: offset — keeps the platform-to-curve proportion constant however wide or narrow the columns
#: fall.
_CURVE_SPAN = 0.75

#: The shortest horizontal run a lane change is given even for a one-lane hop, so a tight
#: column gap still bends across a few dots rather than snapping over in one abrupt step.
_MIN_SHIFT_DOTS = 4

#: How far (as a fraction of the shift's horizontal span) the bezier control points sit in
#: from each end — both placed level with their own end, so the curve leaves and enters the
#: platforms horizontally. Near ½ the S is at its roundest; lower tightens it toward a
#: straight diagonal with only its corners eased.
_BEND_K = 0.5

#: Dots left of our marker the flow arrow sits — one cell, so it embeds in the trunk as ``▶★``
#: and marks the node → us direction without crowding the endpoint.
_ARROW_GAP_DOTS = 2

#: Most distinct paths the lane order is optimised over by exhaustive search. Beyond it the
#: search space (``(n-1)!`` orders of the non-central lanes) is too large, so a barycentre
#: heuristic seats the lanes instead. The widget's real inputs sit far under this.
_MAX_EXACT_LANES = 8

#: Sweeps of the barycentre lane-ordering heuristic used past :data:`_MAX_EXACT_LANES`.
_ORDER_SWEEPS = 8

#: The glyph used for the flow arrow embedded in the trunk just before us, 
#: so the whole flow reads better as a directed run node → us (not a map you wander).
#_ARROW_GLYPH = "▶"  
_ARROW_GLYPH = ""  


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
            keeps it (its edges are also drawn last), and the highest-priority path
            takes the straight centre lane.
    """

    hops: tuple[str, ...]
    color: RGB
    priority: int


def _mid_row(y_dot: float) -> int:
    """Snap a dot row onto the upper-middle dot of its character cell.

    A braille cell is four dot rows tall; a horizontal line drawn on the top or bottom row
    hugs the glyph's edge and reads as sitting too high or too low. Snapping every lane's y
    to :data:`_CELL_MID_DOT` keeps markers — and the level runs along a lane — centred in the
    cell's pixel space.
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
    """Draw the diverge/converge route-flow graph and return its ANSI lines.

    Args:
        layers: The paths to draw, in draw order (later layers, and higher priorities,
            win shared cells; the highest priority takes the straight centre lane). Layers
            with identical hop sequences collapse to one drawn path owned by the highest
            priority among them.
        width: Canvas width in character cells.
        glyph_of: Marker glyph + colour per node id (endpoints keyed by
            :data:`SRC_NODE` / :data:`DST_NODE`).
        label_of: Label text per node id (``None``/``""`` = bare marker). A label is
            kept whole unless it is wider than the canvas, when it is ellipsized to fit.
        label_rgb_of: Label colour per node id.
        min_rows: The fewest canvas rows to draw, however few the lanes.
        max_rows: The most canvas rows to spend; a graph with more lanes than fit
            compresses its lane spacing rather than growing past this.
        lane_step: Vertical dot separation aimed for between lanes. A larger step opens the
            lanes apart; the row budget compresses it when there are many lanes, and never
            stretches past it when there are few. Defaults to :data:`_LANE_STEP_DOTS`.

    Returns:
        One ANSI string per canvas row (empty when there are no layers to draw).
    """
    if not layers:
        return []

    drawn = _collapse(_coalesce_prefixes(layers))
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]

    # First-appearance order for every node, so the layout is identical on every repaint: a
    # set of hash-seeded string ids would iterate in a run-varying order and let the passes
    # settle differently each time, making the graph jump between frames.
    ordered_nodes: list[str] = []
    seen: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for seq in seqs:
        for node in seq:
            if node not in seen:
                seen.add(node)
                ordered_nodes.append(node)
        edges.update(zip(seq, seq[1:]))

    xfrac = _balanced_x(ordered_nodes, edges)

    # A node draws on its highest-priority owning path — the first (by priority, then
    # appearance) to carry it — so a shared relay sits once, on the strongest route through it,
    # and weaker routes jog to meet it. A path that introduces no node of its own (every hop
    # already owned by a stronger route) is *subsumed*: it earns no lane and simply threads the
    # markers others placed, so it costs no empty band.
    best = max(range(len(drawn)), key=lambda j: drawn[j].priority)
    owner: dict[str, int] = {}
    for i in sorted(range(len(drawn)), key=lambda j: -drawn[j].priority):
        for node in seqs[i]:
            owner.setdefault(node, i)
    lane_of_path = _assign_lanes(drawn, seqs, owner, best)

    # The endpoints sit on the best path's lane, where the strongest route runs dead straight
    # across as the graph's spine; the alternatives fan above and below it.
    max_lane = max(lane_of_path.values())
    centre_lane = float(lane_of_path[best])
    node_lane: dict[str, float] = {
        node: (centre_lane if node in (SRC_NODE, DST_NODE) else float(lane_of_path[owner[node]]))
        for node in ordered_nodes
    }

    # -- Vertical sizing: size the rows to the lanes, compressing the spacing (never
    # stretching it past ``lane_step``) so a busy graph fits and a sparse one stays compact.
    if max_lane <= 0:
        step = 0.0
        rows = min_rows
    else:
        ideal = max_lane * lane_step + 2 * _GRAPH_END_DOTS
        rows = max(min_rows, min(max_rows, ceil(ideal / 4)))
        avail = rows * 4 - 2 * _GRAPH_END_DOTS
        step = min(float(lane_step), avail / max_lane)

    canvas = MapCanvas(width, rows)
    dot_w = width * 2
    span = dot_w - 2 * _GRAPH_PAD_DOTS
    band = max_lane * step
    top = (rows * 4 - band) / 2  # centre the lane band in the chosen rows

    def x_of(node: str) -> int:
        return _GRAPH_PAD_DOTS + round(xfrac[node] * span)

    pos: dict[str, tuple[int, int]] = {
        node: (x_of(node), _mid_row(top + node_lane[node] * step)) for node in ordered_nodes
    }
    # -- Edges. Collect every edge once, keyed by its unordered node pair: an edge two routes
    # share — or a pair walked in *both* directions — must draw a single time, else it silts up
    # as a doubled line a dot off itself (two routes' Bresenham runs never land on the exact
    # same dots). Each pair keeps the colour and priority of the strongest route through it; a
    # two-way pair is flagged so it draws as the one honest vertical rather than a lane change.
    bidir = {frozenset((u, v)) for (u, v) in edges if (v, u) in edges}
    edge_style: dict[frozenset[str], tuple[int, RGB]] = {}
    for layer, seq in sorted(zip(drawn, seqs), key=lambda ls: ls[0].priority):
        for u, v in zip(seq, seq[1:]):
            key = frozenset((u, v))
            prev = edge_style.get(key)
            if prev is None or layer.priority > prev[0]:
                edge_style[key] = (layer.priority, layer.color)
    # Draw ascending by priority so the strongest route's colour wins any cell two edges share.
    for key, (priority, color) in sorted(edge_style.items(), key=lambda kv: kv[1][0]):
        u, v = tuple(key)
        canvas.draw_line(_route(u, v, pos, key in bidir), color, priority)

    # -- An arrow embedded in the trunk just before us, so the whole flow reads as a directed
    # run node → us (not a map you wander). A single glyph in the spine's own colour: it reserves
    # its cell, so the endpoint label routes around it rather than colliding with stray dots.
    ax, ay = pos[DST_NODE]
    canvas.marker(ax - _ARROW_GAP_DOTS, ay, _ARROW_GLYPH, drawn[best].color)

    # -- Markers for every node (endpoints and relays alike each draw once).
    for node in ordered_nodes:
        glyph, color_hex = glyph_of(node)
        canvas.marker(*pos[node], glyph, parse_hex(color_hex))

    _place_labels(canvas, ordered_nodes, pos, node_lane, width, rows * 2, label_of, label_rgb_of)
    return canvas.to_ansi_lines()


def _route(
    u: str,
    v: str,
    pos: dict[str, tuple[int, int]],
    bidir: bool,
) -> list[tuple[float, float]]:
    """The point chain for one edge (dot coordinates), drawn as multilane-highway flow.

    Two nodes on the same lane join with a level run; two on different lanes join with a
    *trapezium* — level out of the first marker, one smooth **shift** across the intervening
    lanes (a bezier lane change, level where it meets each platform; see :func:`_sbend`), then
    level into the second — so both nodes sit on a flat platform and there is no corner
    anywhere, only the eased level→curve→level of the shift. The shift takes a fixed share
    :data:`_CURVE_SPAN` of the column gap (a gentle drift, not a slope keyed off the drop) and
    sits centred in it, so the platforms flank it evenly; when the gap is too tight to hold a
    curve of even :data:`_MIN_SHIFT_DOTS`, the bend simply spans the whole gap. The endpoints,
    sitting on the centre lane,
    make the origin's diverging peels and us's converging merges fall out of this one rule —
    no endpoint special case. The lone exception is ``bidir``: a pair walked both ways draws as
    a single straight segment between the markers (a near-vertical when the layout stacks
    them), the one place an up-and-down line is the honest picture.
    """
    (xu, yu), (xv, yv) = pos[u], pos[v]
    if bidir:
        return [(xu, yu), (xv, yv)]
    if xu > xv:  # orient the trapezium left→right; balanced rank only ties, never inverts
        (xu, yu), (xv, yv) = (xv, yv), (xu, yu)
    if yu == yv:
        return [(xu, yu), (xv, yv)]
    dx = xv - xu
    shift = min(dx, max(_MIN_SHIFT_DOTS, round(dx * _CURVE_SPAN)))
    stub = (dx - shift) // 2
    # Level stub, a bezier S across the shift (level tangents at both ends, so it eases out of
    # and back into the platforms with no corner), then the level stub into the far marker.
    return [(xu, yu), *_sbend(xu + stub, yu, xv - stub, yv), (xv, yv)]


def _sbend(
    x0: float, y0: float, x1: float, y1: float
) -> list[tuple[float, float]]:
    """Sample a cubic-bezier S-curve from ``(x0, y0)`` to ``(x1, y1)``, level at both ends.

    Both control points sit level with their own endpoint (:data:`_BEND_K` of the span in from
    each side), so the curve's tangent is horizontal where it meets the platforms — the smooth
    lane change that drifts across and settles rather than cutting a hard diagonal. Sampled
    densely enough that the polyline rasterizes as a continuous curve; the convex hull keeps it
    inside the ``(x0, y0)–(x1, y1)`` box, so it never overshoots its lane or column.
    """
    cx0 = x0 + _BEND_K * (x1 - x0)
    cx1 = x1 - _BEND_K * (x1 - x0)
    samples = max(4, int(abs(x1 - x0) + abs(y1 - y0)))
    pts: list[tuple[float, float]] = []
    for i in range(samples + 1):
        t = i / samples
        mt = 1.0 - t
        a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
        pts.append((a * x0 + b * cx0 + c * cx1 + d * x1, a * y0 + b * y0 + c * y1 + d * y1))
    return pts


def _balanced_x(ordered_nodes: list[str], edges: set[tuple[str, str]]) -> dict[str, float]:
    """Each node's horizontal position in ``0..1`` — balanced rank from origin toward us.

    A node's fraction is its longest-path distance from the origin over that distance plus
    its longest remaining distance to us: the origin lands at ``0``, us at ``1``, and every
    other node between them in proportion to how far along its route it sits. So a path's
    relays spread *evenly* between the two ends however many hops the other paths take — a
    lone relay on a one-hop route lands mid-canvas rather than jammed against the origin with
    a long edge arcing across to us — and a relay shared by routes of different lengths still
    resolves to one x. Because every edge steps the from-origin distance up and the
    to-us distance down, the fraction rises strictly along each path: edges only ever run
    left to right.

    A pair walked in *both* directions is a 2-cycle in the edge set, and the longest-path
    relaxation would loop through it, inflating every downstream depth toward the node-count
    cap and jamming other relays hard against the ends. So the rank runs over the graph with
    each such pair (transitively) merged to one representative — the acyclic flow the picture
    really is; the pair, drawn as a single vertical, rightly shares an x anyway.
    """
    rep = _merge_bidir_pairs(ordered_nodes, edges)
    reps: list[str] = []
    seen: set[str] = set()
    for node in ordered_nodes:
        if rep[node] not in seen:
            seen.add(rep[node])
            reps.append(rep[node])
    rep_edges = {(rep[u], rep[v]) for u, v in edges if rep[u] != rep[v]}
    up = _longest_paths(reps, rep_edges)
    down = _longest_paths(reps, {(v, u) for u, v in rep_edges})
    frac = {
        r: (up[r] / (up[r] + down[r])) if (up[r] + down[r]) else 0.0 for r in reps
    }
    return {node: frac[rep[node]] for node in ordered_nodes}


def _merge_bidir_pairs(
    ordered_nodes: list[str], edges: set[tuple[str, str]]
) -> dict[str, str]:
    """Map each node to a representative, uniting any two joined by a both-ways edge.

    Two nodes walked in both directions form a 2-cycle; uniting them — transitively, so a
    chain of such pairs folds into one group — lets the balanced rank treat the flow as the
    acyclic run it otherwise is. A node in no such pair maps to itself. The choice of which
    member is the representative doesn't matter: every member is assigned the group's one
    fraction, and the group's rank is structural.
    """
    parent = {node: node for node in ordered_nodes}

    def find(node: str) -> str:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path-compress
            parent[node], node = root, parent[node]
        return root

    for u, v in edges:
        if (v, u) in edges:
            ru, rv = find(u), find(v)
            if ru != rv:
                parent[ru] = rv
    return {node: find(node) for node in ordered_nodes}


def _longest_paths(ordered_nodes: list[str], edges: set[tuple[str, str]]) -> dict[str, int]:
    """Longest-path depth of each node over ``edges`` (relaxed to a fixed point).

    Capped at the node count so a pathological cycle in the id set can't spin it forever;
    the graphs are acyclic fans, so it settles in a couple of passes.
    """
    depth = {node: 0 for node in ordered_nodes}
    for _ in range(len(ordered_nodes)):
        changed = False
        for u, v in edges:
            if depth[v] < depth[u] + 1:
                depth[v] = depth[u] + 1
                changed = True
        if not changed:
            break
    return depth


def _assign_lanes(
    drawn: Sequence[PathLayer],
    seqs: Sequence[tuple[str, ...]],
    owner: dict[str, int],
    best: int,
) -> dict[int, int]:
    """Seat each lane-bearing path on a horizontal lane, best centred, sharing routes close.

    Returns ``{path_index: lane}`` — only for the paths that own at least one node (a subsumed
    path threads others' markers and needs no lane of its own) — with lanes ``0`` (top) up. The
    best path is pinned to the centre lane, where it runs straight through the two endpoints as
    the graph's spine; the rest are ordered to minimise the total *jog* — the vertical distance
    a path travels to reach a relay another path owns — so routes that share hops sit near each
    other and a shared relay costs the shortest detour. Small graphs
    (``≤`` :data:`_MAX_EXACT_LANES` lanes) get the exact best order by search; larger ones fall
    back to a barycentre heuristic.
    """
    bearing = [i for i in range(len(drawn)) if any(owner[node] == i for node in seqs[i])]
    n = len(bearing)
    if n == 1:
        return {bearing[0]: 0}

    # Each bearing path's jog partners: the owning lanes of the hops it borrows from others.
    shared_owners = {
        i: [owner[node] for node in seqs[i] if owner[node] != i] for i in bearing
    }

    def jog(lane: dict[int, int]) -> int:
        return sum(abs(lane[i] - lane[o]) for i in bearing for o in shared_owners[i])

    centre_slot = (n - 1) // 2
    others = [i for i in bearing if i != best]

    if n - 1 <= _MAX_EXACT_LANES:
        best_order: Optional[tuple[int, ...]] = None
        best_cost: Optional[int] = None
        for perm in permutations(others):
            slots = list(perm)
            slots.insert(centre_slot, best)
            cost = jog({p: k for k, p in enumerate(slots)})
            if best_cost is None or cost < best_cost:
                best_cost, best_order = cost, tuple(slots)
        assert best_order is not None
        return {p: k for k, p in enumerate(best_order)}

    return _barycentre_lanes(bearing, best, centre_slot, shared_owners)


def _barycentre_lanes(
    bearing: list[int],
    best: int,
    centre_slot: int,
    shared_owners: dict[int, list[int]],
) -> dict[int, int]:
    """Heuristic lane order for a graph too large to search: barycentre sweeps.

    Each path is repeatedly re-seated at the average lane of the paths it shares relays with,
    with the best path pinned to the centre lane. A handful of sweeps settles sharing routes
    next to each other — the cheap crossing-minimiser, at the coarser grain of whole lanes.
    """
    lane = {p: float(k) for k, p in enumerate(bearing)}
    lane[best] = float(centre_slot)
    others = [i for i in bearing if i != best]
    for _ in range(_ORDER_SWEEPS):
        for i in others:
            if shared_owners[i]:
                lane[i] = sum(lane[o] for o in shared_owners[i]) / len(shared_owners[i])
        order = sorted(others, key=lambda i: lane[i])
        order.insert(centre_slot, best)
        lane = {p: float(k) for k, p in enumerate(order)}
    return {p: int(lane[p]) for p in order}


def _place_labels(
    canvas: MapCanvas,
    ordered_nodes: list[str],
    pos: dict[str, tuple[int, int]],
    node_lane: dict[str, float],
    width: int,
    mid_y: int,
    label_of: LabelOf,
    label_rgb_of: LabelRgbOf,
) -> None:
    """Write each node's label above or below its marker, clear of the lines if it can.

    Endpoints first, then relays down the lanes, so the named ends win any contest for a
    cell. Each label is tried on the side away from the graph's middle first (spreading the
    text outward off the busy centre), preferring a row the drawn lines don't already occupy.
    Any node hard against a canvas edge — an endpoint by construction, or a relay a balanced
    rank pins near the origin or us — slides its centre anchor inward far enough for the whole
    name to land, since a centred label overhanging the edge places nothing and would leave the
    marker silently unlabelled. When both stacked rows are blocked the label falls back to
    sitting beside its marker. A name is only ever shortened when it is wider than the entire
    canvas.
    """
    endpoints = [n for n in (DST_NODE, SRC_NODE) if n in pos]
    relays = sorted(
        (n for n in ordered_nodes if n not in (SRC_NODE, DST_NODE)),
        key=lambda n: (node_lane[n], pos[n][0]),
    )
    for node in [*endpoints, *relays]:
        label = label_of(node)
        if not label:
            continue
        if len(label) > width:
            label = label[: max(1, width - 1)] + "…"
        rgb = label_rgb_of(node)
        x, y = pos[node]
        # Clamp the centre cell so the whole label fits between the canvas edges: a marker near
        # an edge would otherwise centre its name off-canvas, place nothing, and be dropped. A
        # marker with room to spare keeps its true centre (the clamp is a no-op there).
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
        # Both rows blocked: sit the label beside the marker (clear of the drawn lines if it
        # can, else over them) rather than drop it.
        if not canvas.marker_label(x, y, label, rgb, avoid_dots=True):
            canvas.marker_label(x, y, label, rgb)
