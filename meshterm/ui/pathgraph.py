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
now draws the fan as a **multilane highway**: a node sits level in its lane, and a route changes
lane only *between* nodes, easing across on a single gentle shift the way a car drifts one lane
over and then settles. Concretely:

* **lanes** — the highest-*priority* path (the spine, e.g. the best-evidence route) holds the
  flow's centre, its relays in a straight run, and the alternatives fan above and below it a
  fixed pitch of text rows apart. Priority fixes the geometry; a separate *emphasis* rank picks
  which route is drawn highlighted (on top, winning any shared cell) without moving a marker, so
  a caller can light a different route without the picture reflowing. Rather than give every
  route a full-width lane of its own —
  which stacks the band as tall as the route count even where the routes barely overlap — the
  lanes are *packed by column*: the spine keeps its own relays, and each other node slides to the
  innermost free row above or below the spine *in its own column*, so a column with one off-spine
  node claims a single flanking row however many routes cross the graph, and only a column where
  routes genuinely stack pays the deeper rows. Which flank an alternative takes is then *balanced*
  so column-sharing routes split above and below rather than piling one flank two deep while the
  other sits empty (the band is only as tall as its deepest flank plus the other's): a five-route
  fan through at most two nodes per column draws three lanes deep — spine plus one flank each side
  — not five. The two endpoints sit at the vertical centre of the packed band, so the spine runs
  through them — dead straight when the flanks come out even and the lanes are odd, leaning gently
  to the centre otherwise (and where the lanes are even, one extra padding row is opened between
  the two central lanes so the endpoints land on an exact centred row between them);
* **columns** (x) place each node by *balanced* rank — its distance from the origin over its
  distance-plus-remaining-distance to us — so a path's relays spread evenly between the two
  ends however long the other paths are, and a shared relay lands in one place;
* **level seating** — every node is entered and left on a *level tangent*, so a relay that sits
  off its neighbours' lane reads as a gentle rise-and-settle, never a pointed peak or valley.
  Between two nodes on different lanes the shift leaves the first level, drifts across, and
  arrives level into the second — the only bends are the soft level→curve→level eases, never a
  bare right angle in open canvas. The curve spans the whole gap rather than a centred stretch
  flanked by flat platforms: a platform-to-curve corner draws a heavy braille *knee*, so the
  stroke runs continuously node to node and stays an even, thin arc (see :data:`_CURVE_SPAN`);
* **shared relays** draw as a single marker (a route re-using a hop is not a new node): the
  marker sits on its highest-priority path's lane, and a lower path that also rides it leans
  off its lane to meet the marker and back — which reads as the alternative *branching
  through the shared node*, exactly the story the evidence tells;
* **revisits** — that merge is right *across* paths (two routes rode one relay) and wrong
  *within* one: a single walk that touches the same hop twice is not a node two routes share,
  it is a **cycle**, and a left-to-right flow has nowhere to seat one. Ranked over the cyclic
  edge set the balanced-x relaxation never settles, so the loop's members land in near-identical
  columns — markers piled a cell apart, labels colliding, the back-edge dropping as a bare
  vertical — while the relays outside it are squashed against the ends. ``allow_duplicate_nodes``
  hands each revisit its own marker instead (see :func:`_split_revisits`), which keeps the flow
  acyclic and draws the walk in its true order; the cost is that one node *may* appear twice,
  so a caller that turns it on should say so on the surface (:func:`~meshterm.ui.widgets.
  revisit_note`). It is opt-in because the choice is a caller's to make: a path we *composed*
  (the Trophy case's scored walk) collapses a revisit deliberately, while an **observed** via
  chain — hops named by a one-byte hash, where a repeat is as likely two colliding nodes as a
  genuine loop — must be drawn as heard;
* **detours** — a route heard as a sibling route *plus* an inserted relay is that sibling with
  a detour, and the whole fan is measured against the row budget *before* its shape is
  committed: while the rows allow, the detour **nests** — its inserted relay takes the lane
  just outside its sibling's, on the same flank, so the sibling's straight run visibly skips
  the relay and the detour reads as the wider arc through it. Only a viewport too short for
  that extra lane **folds** the relay onto the sibling's lane itself (weakest detours first,
  re-measuring after each), where the sibling's run passes over it — the legible-but-lossier
  last resort, never the default (see :func:`_layout_lanes`);
* **bypasses** — the detour's mirror: beside the spine's ``A → B → C`` lives the shorter route
  that *skips* ``B``, and with ``A`` and ``C`` seated on one lane its ``A→C`` edge is a level
  run straight through the marker of the one node it doesn't ride — drawn, it reads as *via*
  ``B``, the story the evidence rules out. Where the skipped node's own column has room — a
  free lane just off it, or one the row budget affords opening — the edge bends around instead,
  arcing through an unmarked *virtual waypoint* in that column, the same wider-arc grammar a
  nested detour draws, so skipping reads as skipping (see :func:`_bypass_vias`); only a graph
  with genuinely no room left keeps the level pass-over;
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
from itertools import permutations, product
from math import ceil
from typing import Callable, Optional, Sequence

from .mapcanvas import MapCanvas
from .marks import (  # noqa: F401 - canonical home; re-exported for existing importers
    DST_NODE,
    RGB,
    SRC_NODE,
    GlyphOf,
    LabelOf,
    LabelRgbOf,
    parse_hex,
)
from .theme import mark_rgb

#: Joins a hop id to its occurrence index when ``allow_duplicate_nodes`` splits a path's
#: revisits into their own markers. Leans on the same guarantee the endpoint sentinels do —
#: NUL never appears in a hex hop id — so a qualified id can never collide with a real one,
#: and :func:`_base_node` maps it back before any caller callback ever sees it.
_OCCURRENCE_SEP = "\x00#"

#: Text rows between adjacent lane markers — the pitch one lane maps to. At the default there
#: are two blank rows padding each gap (room for a lane's label and its neighbour's), and an
#: *even* lane count opens one extra row between the two central lanes so the endpoints land on
#: an exact centred row (see the vertical-sizing block). A graph with more lanes than fit the
#: row budget scales the pitch down to fit, and a graph with few lanes never stretches *past* it
#: (spreading the lanes apart would only reintroduce the empty space the layout exists to
#: avoid), so a sparse graph draws compact rather than splayed.
_LANE_PITCH_ROWS = 3

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

#: The share of a lane change's horizontal column gap spent on the eased curve itself — the rest
#: would split into two flat platforms flanking it. Set to the whole gap: the curve runs
#: continuously from one node to the next with *no* separate flat segment. A shorter curve leaves
#: flat platforms but reintroduces a **corner** where a platform (slope 0) meets the climbing
#: curve, and that corner piles a heavy braille *knee* (a cell filled 3–4 dots) that reads thick —
#: the more so the shallower the curve's end tangent. Spanning the whole gap removes the corner,
#: so the stroke stays an even, thin arc end to end; the nodes still seat level because the curve's
#: end tangents are kept horizontal (see :data:`_BEND_K`), giving each marker a flat point to sit on
#: without a platform run. (In a very tight column a full-lane shift is unavoidably steep right up to
#: the node, so its marker sits on a gentle slope rather than dead level — the honest cost of keeping
#: the stroke thin there.)
_CURVE_SPAN = 1.0

#: The shortest horizontal run a lane change is given even for a one-lane hop, so a tight
#: column gap still bends across a few dots rather than snapping over in one abrupt step.
_MIN_SHIFT_DOTS = 4

#: How far (as a fraction of the shift's horizontal span) the bezier control points sit in from
#: each end — both placed level with their own end, so the curve leaves and enters each node
#: horizontally. This flat end tangent is what seats a node level now that the curve spans the
#: whole gap (:data:`_CURVE_SPAN`) with no separate platform. It trades against thinness: a higher
#: value holds the tangent flatter for longer (a better-seated node) but steepens the curve's
#: middle to compensate (a thicker centre), while a lower value spreads the drop more evenly
#: (thinner) but tilts the node's tangent (a marker on a slope). Tuned to the balance that keeps
#: the marker's seating close to level while the stroke stays an even, thin arc between nodes.
_BEND_K = 0.4

#: Dots left of our marker the flow arrow sits — one cell, so it embeds in the trunk as ``▶★``
#: and marks the node → us direction without crowding the endpoint.
_ARROW_GAP_DOTS = 2

#: Most distinct paths the lane order is optimised over by exhaustive search. Beyond it the
#: search space (``(n-1)!`` orders of the non-central lanes) is too large, so a barycentre
#: heuristic seats the lanes instead. The widget's real inputs sit far under this.
_MAX_EXACT_LANES = 8

#: Sweeps of the barycentre lane-ordering heuristic used past :data:`_MAX_EXACT_LANES`.
_ORDER_SWEEPS = 8

#: Most non-best routes the side-balancer weighs by exhaustive 2-colouring (see
#: :func:`_balance_sides`). Past it the ``2**m`` side assignments grow too many, so the routes
#: keep the side the jog order handed them. The widget's real inputs sit far under this.
_MAX_BALANCE_ROUTES = 16

#: The glyph used for the flow arrow embedded in the trunk just before us,
#: so the whole flow reads better as a directed run node → us (not a map you wander).
#_ARROW_GLYPH = "▶"
_ARROW_GLYPH = ""

#: How far a layer's emphasis outranks its layout priority when edges compete for a cell.
#: Emphasis is the draw-time highlight — the selected path drawn on top and winning any shared
#: cell — and must beat any spread of priority values, so it is scaled well past the small
#: priorities the widget ever sees. Layout geometry ignores emphasis entirely; only the colour
#: and z-order of the drawn edges follow it (see :func:`_draw_rank`), so re-emphasising a
#: different path repaints the same picture in new colours rather than relaying it.
_EMPHASIS_BOOST = 1_000_000


@dataclass(frozen=True)
class PathLayer:
    """One path drawn on the route graph.

    Attributes:
        hops: The relay node ids between the endpoints, in walk order (empty = the
            path runs endpoint to endpoint straight across).
        color: The edge colour the path draws in.
        priority: **Layout** rank — which path is the spine. The highest-priority path takes
            the straight centre lane and owns any relay it shares (weaker paths jog to meet it),
            so it fixes every node's column and lane. Geometry depends on this alone, never on
            :attr:`emphasis`: a caller that wants the drawn picture to hold still while it
            re-highlights keeps each path's priority constant.
        emphasis: **Draw** rank — the highlight, layered over the fixed geometry. Where edges
            share a cell (or cross), the higher-emphasis path wins the colour and draws on top;
            it moves no marker. Defaults to ``0`` (all paths equal, so the colour falls to
            :attr:`priority` as before). A caller that highlights by selection varies *this*,
            not priority, so the layout stays put and only the colours change.
    """

    hops: tuple[str, ...]
    color: RGB
    priority: int
    emphasis: int = 0


def _mid_row(y_dot: float) -> int:
    """Snap a dot row onto the upper-middle dot of its character cell.

    A braille cell is four dot rows tall; a horizontal line drawn on the top or bottom row
    hugs the glyph's edge and reads as sitting too high or too low. Snapping every lane's y
    to :data:`_CELL_MID_DOT` keeps markers — and the level runs along a lane — centred in the
    cell's pixel space.
    """
    return round((y_dot - _CELL_MID_DOT) / 4) * 4 + _CELL_MID_DOT


def _collapse(layers: Sequence[PathLayer]) -> list[PathLayer]:
    """Fold layers with identical hop sequences onto one drawn path.

    Re-walking a known route highlights it rather than doubling it. Geometry keeps the
    strongest ``priority`` of the folded copies (so a shared route seats where its best
    instance would), while the highlight — the drawn colour and its on-top order — follows the
    most-*emphasised* copy, and the emphasis carries across. So two routes that only differ in
    a cluster-internal order the caller has contracted away land on one line, and selecting
    either lights it, even when a higher-priority copy owns the layout.
    """
    drawn: list[PathLayer] = []
    by_hops: dict[tuple[str, ...], int] = {}
    for layer in layers:
        at = by_hops.get(layer.hops)
        if at is None:
            by_hops[layer.hops] = len(drawn)
            drawn.append(layer)
        else:
            cur = drawn[at]
            top = layer if _draw_rank(layer) > _draw_rank(cur) else cur
            drawn[at] = PathLayer(
                cur.hops,
                top.color,
                max(layer.priority, cur.priority),
                max(layer.emphasis, cur.emphasis),
            )
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
        PathLayer(
            tuple(resolved(hop) for hop in layer.hops),
            layer.color, layer.priority, layer.emphasis,
        )
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


def revisited_hops(hops: Sequence[str]) -> tuple[str, ...]:
    """The hops one path touches more than once, in first-appearance order.

    The test a caller applies before deciding to draw with ``allow_duplicate_nodes`` — and the
    hops it then names in its warning (:func:`~meshterm.ui.widgets.revisit_note`), since a
    graph drawing one node twice owes the reader that much. Judged on the ids as given: at the
    one-byte width an observed via chain addresses its hops by, a repeat is as likely two
    different nodes colliding on a hash byte as a packet genuinely walking a loop, and neither
    the path nor this widget can tell them apart — which is exactly what the warning says.
    """
    counts: dict[str, int] = {}
    for hop in hops:
        if hop:
            counts[hop] = counts.get(hop, 0) + 1
    return tuple(hop for hop, seen in counts.items() if seen > 1)


def _split_revisits(layers: Sequence[PathLayer]) -> list[PathLayer]:
    """Give every revisit *within* a path its own node id, so no walk folds into a cycle.

    The k-th occurrence of a hop in one layer becomes ``hop\\x00#k`` (the first keeps the bare
    id), which leaves the flow acyclic and lets the balanced rank spread the walk evenly again.
    Counting runs per layer but the qualifier is positional, so occurrence *k* of a hop means the
    same id in every layer that reaches that far: two routes riding one relay once still merge on
    the bare id — the diverge/converge story the widget exists to tell survives untouched, and
    only a genuine within-path repeat splits.
    """
    split: list[PathLayer] = []
    for layer in layers:
        seen: dict[str, int] = {}
        hops: list[str] = []
        for hop in layer.hops:
            nth = seen.get(hop, 0)
            seen[hop] = nth + 1
            hops.append(hop if nth == 0 else f"{hop}{_OCCURRENCE_SEP}{nth}")
        split.append(PathLayer(tuple(hops), layer.color, layer.priority, layer.emphasis))
    return split


def _base_node(node: str) -> str:
    """An occurrence-qualified internal id back to the caller's own node id (else unchanged)."""
    return node.split(_OCCURRENCE_SEP, 1)[0]


def _unqualified(
    glyph_of: GlyphOf, label_of: LabelOf, label_rgb_of: LabelRgbOf
) -> tuple[GlyphOf, LabelOf, LabelRgbOf]:
    """Wrap the per-node callbacks so a split revisit reaches them as the node it really is.

    Occurrence qualifiers are the widget's private bookkeeping: a caller supplies callbacks keyed
    on its own hop ids and gets back a marker, a label and a hue per *node*, so both markers of a
    revisited hop draw identically — the same glyph, the same name, the same colour — and the
    picture says "here twice" rather than inventing a second identity.
    """

    def glyph(node: str) -> tuple[str, str]:
        return glyph_of(_base_node(node))

    def label(node: str) -> Optional[str]:
        return label_of(_base_node(node))

    def label_rgb(node: str) -> RGB:
        return label_rgb_of(_base_node(node))

    return glyph, label, label_rgb


def _draw_rank(layer: PathLayer) -> int:
    """A layer's edge draw rank: emphasis dominates, layout priority breaks ties.

    Governs only which colour wins a shared cell and which edge draws on top — never node
    placement (that is :attr:`PathLayer.priority` alone). So a caller can re-emphasise a
    different path (bump its :attr:`~PathLayer.emphasis`) and the graph repaints in new colours
    over the very same layout, rather than reflowing because the spine changed.
    """
    return layer.priority + layer.emphasis * _EMPHASIS_BOOST


def render_path_graph(
    layers: Sequence[PathLayer],
    width: int,
    *,
    glyph_of: GlyphOf,
    label_of: LabelOf,
    label_rgb_of: LabelRgbOf,
    min_rows: int = 5,
    max_rows: int = 15,
    lane_pitch: int = _LANE_PITCH_ROWS,
    allow_duplicate_nodes: bool = False,
) -> list[str]:
    """Draw the diverge/converge route-flow graph and return its ANSI lines.

    Args:
        layers: The paths to draw. The highest ``priority`` takes the straight centre lane and
            fixes the geometry; a separate ``emphasis`` (default ``0``) decides which path is
            drawn highlighted — on top, winning any shared cell — without moving a marker, so
            re-emphasising a path repaints the same layout in new colours. Layers with identical
            hop sequences collapse to one drawn path owned by the highest priority among them.
        width: Canvas width in character cells.
        glyph_of: Marker glyph + colour per node id (endpoints keyed by
            :data:`SRC_NODE` / :data:`DST_NODE`). The colour is whatever
            :func:`~meshterm.ui.theme.mark_rgb` takes — a literal ``#rrggbb`` or a theme
            style name.
        label_of: Label text per node id (``None``/``""`` = bare marker). A label is
            kept whole unless it is wider than the canvas, when it is ellipsized to fit.
        label_rgb_of: Label colour per node id.
        min_rows: The fewest canvas rows to draw, however few the lanes.
        max_rows: The most canvas rows to spend; a graph with more lanes than fit
            compresses its lane spacing rather than growing past this.
        lane_pitch: Text rows between adjacent lane markers (so ``lane_pitch - 1`` blank rows
            pad each gap). An even lane count opens one extra row between the two central lanes
            so the endpoints land on an exact centred row. The row budget compresses the pitch
            when there are many lanes, and never stretches it when there are few. Defaults to
            :data:`_LANE_PITCH_ROWS`.
        allow_duplicate_nodes: Draw a hop a path touches *twice* as two markers rather than
            folding it into one. Off by default, because folding is right for a path the caller
            composed. Turn it on for an **observed** walk, where the fold would make a cycle the
            left-to-right flow cannot seat and the layout collapses (see the module docstring):
            the walk then draws in its true order, at the cost of one node possibly appearing
            twice — which the caller should flag on the surface (:func:`~meshterm.ui.widgets.
            revisit_note`, over :func:`revisited_hops`). Relays shared *between* paths merge
            either way.

    Returns:
        One ANSI string per canvas row (empty when there are no layers to draw).
    """
    if not layers:
        return []

    drawn = _collapse(_coalesce_prefixes(layers))
    if allow_duplicate_nodes:
        # After the prefix/identical folds, so a revisit is counted over the ids actually drawn:
        # a hop that only *looks* repeated at two hash widths coalesces to one id first, and is
        # then correctly seen as the single visit it is.
        drawn = _split_revisits(drawn)
        glyph_of, label_of, label_rgb_of = _unqualified(glyph_of, label_of, label_rgb_of)
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
    span = width * 2 - 2 * _GRAPH_PAD_DOTS

    def col_of(node: str) -> int:
        return (_GRAPH_PAD_DOTS + round(xfrac[node] * span)) >> 1

    # Seat every node on a signed lane, sizing the whole fan against the row budget before
    # committing to a shape: detour routes nest outside their sibling while the rows allow it,
    # and fold onto the sibling's lane only as the last resort. See :func:`_layout_lanes`.
    signed = _layout_lanes(
        drawn, seqs, ordered_nodes, owner, best, col_of, max_rows, lane_pitch
    )

    # A pair walked in *both* directions draws as the one honest vertical (the edge pass
    # below) — and is likewise never bent around anything.
    bidir = {frozenset((u, v)) for (u, v) in edges if (v, u) in edges}
    # An edge that would run level straight through a marker it *skips* bends around it
    # instead, through a virtual waypoint in the skipped node's own column — wherever the
    # column has room (see :func:`_bypass_vias`). The via lanes join the band extent below,
    # so the vertical sizing affords any lane a bypass opens.
    vias = _bypass_vias(seqs, bidir, signed, col_of, max_rows, lane_pitch)
    via_lanes = [lane for hops in vias.values() for _m, lane in hops]

    # The endpoints sit at the vertical centre of the compressed lane band, where the strongest
    # route runs through them as the graph's spine; the alternatives fan above and below. When the
    # flanks balance out evenly the spine's own lane *is* that centre and it runs dead straight;
    # when the band is lopsided (or even-numbered) the centre falls between lanes, and the best
    # path eases gently to reach the endpoints rather than seating the whole graph off-centre —
    # a lean the balancer keeps small by flattening the flanks first.
    low = min([*signed.values(), *via_lanes], default=0)
    high = max([*signed.values(), *via_lanes], default=0)
    max_lane = high - low
    centre_lane = max_lane / 2.0
    node_lane: dict[str, float] = {
        node: (centre_lane if node in (SRC_NODE, DST_NODE) else float(signed[node] - low))
        for node in ordered_nodes
    }

    # -- Vertical sizing. Each lane sits on its own text row, ``lane_pitch`` rows apart (so
    # ``lane_pitch - 1`` blank rows pad each gap). On an *even* lane count the two central lanes
    # are opened one extra row apart, so the endpoints — pinned to the band's centre — land on
    # the exact middle row between them rather than on a fractional row that would snap off it;
    # an *odd* count already seats a central lane there for them to ride. A graph too tall for
    # ``max_rows`` scales every row down proportionally (never up), so it stays compact.
    even_lanes = max_lane % 2 == 1  # N = max_lane + 1 lanes; even ⟺ max_lane odd
    lower_centre = max_lane // 2 + 1  # first lane below the centre (only meaningful when even)

    def slot(lane: float) -> float:
        """The text row (pre-scaling) a lane index maps to, with the even-count centre gap."""
        rows_out = lane * lane_pitch
        if even_lanes and lane > lower_centre - 1:
            # the lower-central lane and everything below it are pushed one row down; the
            # endpoints' half-lane takes half of it, landing them on the widened gap's middle.
            rows_out += min(1.0, lane - (lower_centre - 1))
        return rows_out

    if max_lane <= 0:
        rows = min_rows
        lane_rows = 0.0
        scale = 0.0
    else:
        lane_rows = slot(float(max_lane))  # total lane span, in rows
        ideal = lane_rows * 4 + 2 * _GRAPH_END_DOTS
        rows = max(min_rows, min(max_rows, ceil(ideal / 4)))
        avail = rows * 4 - 2 * _GRAPH_END_DOTS
        scale = min(1.0, avail / (lane_rows * 4)) if lane_rows else 0.0

    canvas = MapCanvas(width, rows)
    band = lane_rows * 4 * scale
    # Centre the band, anchored on a cell-mid row so the (unscaled) integer lane rows land dead
    # on their cells — no per-lane snap drift that would nudge a centred endpoint off its middle.
    top = float(_mid_row((rows * 4 - band) / 2))

    def x_of(node: str) -> int:
        return _GRAPH_PAD_DOTS + round(xfrac[node] * span)

    pos: dict[str, tuple[int, int]] = {
        node: (x_of(node), _mid_row(top + slot(node_lane[node]) * 4 * scale))
        for node in ordered_nodes
    }
    # Each bypass via becomes a dot point at the skipped marker's exact x, seated on its own
    # lane row through the same slot/snap the real nodes ride — the arc's level peak sits dead
    # over the node it clears.
    via_pts: dict[frozenset[str], list[tuple[float, float]]] = {
        key: [
            (float(pos[m][0]), float(_mid_row(top + slot(float(lane - low)) * 4 * scale)))
            for m, lane in hops
        ]
        for key, hops in vias.items()
    }
    # -- Edges. Collect every edge once, keyed by its unordered node pair: an edge two routes
    # share — or a pair walked in *both* directions — must draw a single time, else it silts up
    # as a doubled line a dot off itself (two routes' Bresenham runs never land on the exact
    # same dots). Each pair keeps the colour and draw rank of the strongest route through it —
    # by draw rank, so the *emphasised* (highlighted) route wins a shared edge over a merely
    # higher-priority spine, and the highlight paints the whole selected route rather than
    # dropping out where it overlaps another. A two-way pair (``bidir``, above) draws as the
    # one honest vertical rather than a lane change.
    edge_style: dict[frozenset[str], tuple[int, RGB]] = {}
    for layer, seq in sorted(zip(drawn, seqs), key=lambda ls: _draw_rank(ls[0])):
        rank = _draw_rank(layer)
        for u, v in zip(seq, seq[1:]):
            key = frozenset((u, v))
            prev = edge_style.get(key)
            if prev is None or rank > prev[0]:
                edge_style[key] = (rank, layer.color)
    # Draw ascending by draw rank so the strongest/most-emphasised route's colour wins any cell
    # two edges share and sits on top.
    for key, (rank, color) in sorted(edge_style.items(), key=lambda kv: kv[1][0]):
        u, v = tuple(key)
        canvas.draw_line(
            _route(u, v, pos, key in bidir, via_pts.get(key, ())), color, rank
        )

    # -- An arrow embedded in the trunk just before us, so the whole flow reads as a directed
    # run node → us (not a map you wander). A single glyph in the spine's own colour: it reserves
    # its cell, so the endpoint label routes around it rather than colliding with stray dots.
    ax, ay = pos[DST_NODE]
    canvas.marker(ax - _ARROW_GAP_DOTS, ay, _ARROW_GLYPH, drawn[best].color)

    # -- Markers for every node (endpoints and relays alike each draw once).
    for node in ordered_nodes:
        glyph, colour = glyph_of(node)
        canvas.marker(*pos[node], glyph, mark_rgb(colour))

    _place_labels(canvas, ordered_nodes, pos, node_lane, width, rows * 2, label_of, label_rgb_of)
    return canvas.to_ansi_lines()


def _route(
    u: str,
    v: str,
    pos: dict[str, tuple[int, int]],
    bidir: bool,
    vias: Sequence[tuple[float, float]] = (),
) -> list[tuple[float, float]]:
    """The point chain for one edge (dot coordinates), drawn as multilane-highway flow.

    Two nodes on the same lane join with a level run; two on different lanes join with one smooth
    **shift** — a bezier S that leaves the first marker level, drifts across the intervening lanes,
    and settles level into the second (see :func:`_sbend`), so there is no corner anywhere, only
    the eased level→curve→level of the shift. The shift spans the whole column gap
    (:data:`_CURVE_SPAN`) rather than a centred stretch flanked by flat platforms: a platform would
    meet the climbing curve at a corner, and that corner draws a heavy braille *knee*, so the curve
    runs continuously node to node instead and the marker seats on the curve's own level end tangent.
    The endpoints, sitting at the centre of the lane band, make the origin's diverging peels and
    us's converging merges fall out of this one rule — no endpoint special case.

    ``vias`` are an edge's bypass waypoints (:func:`_bypass_vias`), threaded between the two
    markers in x order: the run applies the same level-or-shift grammar anchor to anchor —
    marker to via to via to marker — so a bypass eases out, sits level for an instant dead
    over the marker it clears, and eases back in, never cutting through it. The lone exception
    is ``bidir``: a pair walked both ways draws as a single straight segment between the markers
    (a near-vertical when the layout stacks them), the one place an up-and-down line is the
    honest picture — and never a bent one.
    """
    (xu, yu), (xv, yv) = pos[u], pos[v]
    if bidir:
        return [(xu, yu), (xv, yv)]
    if xu > xv:  # orient the trapezium left→right; balanced rank only ties, never inverts
        (xu, yu), (xv, yv) = (xv, yv), (xu, yu)
    anchors: list[tuple[float, float]] = [(xu, yu), *sorted(vias), (xv, yv)]
    pts: list[tuple[float, float]] = [anchors[0]]
    for (xa, ya), (xb, yb) in zip(anchors, anchors[1:]):
        if ya == yb:
            pts.append((xb, yb))
            continue
        dx = xb - xa
        shift = min(dx, max(_MIN_SHIFT_DOTS, round(dx * _CURVE_SPAN)))
        stub = (dx - shift) // 2
        # A bezier S across the whole gap (level tangents at both ends, so it eases out of and
        # back into each anchor with no corner — and no platform corner to pile a heavy knee).
        pts.extend([*_sbend(xa + stub, ya, xb - stub, yb), (xb, yb)])
    return pts


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


def bidir_clusters(sequences: Sequence[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """The groups of three or more nodes that form a bidirectional cluster (a flow SCC).

    Two nodes walked in both directions are a 2-cycle the graph draws as one tidy vertical
    pair — fine on its own. Three or more mutually linked that way are a strongly-connected
    knot the left-to-right flow cannot order: :func:`_merge_bidir_pairs` collapses them all
    onto one column, where their markers and labels pile up illegibly. A caller can pass its
    path sequences (endpoints included) here to find those knots and contract each to a single
    super-node *before* drawing, so the cluster reads as one marker rather than a jam — the one
    honest way to seat a cycle in a DAG layout.

    Returns each cluster's member ids in first-appearance order (endpoints excluded); a lone
    node or the tidy two-node pair is not a cluster and is not returned.
    """
    edges = {pair for seq in sequences for pair in zip(seq, seq[1:])}
    ordered = list(dict.fromkeys(node for seq in sequences for node in seq))
    rep = _merge_bidir_pairs(ordered, edges)
    groups: dict[str, list[str]] = {}
    for node in ordered:
        if node in (SRC_NODE, DST_NODE):
            continue
        groups.setdefault(rep[node], []).append(node)
    return [tuple(members) for members in groups.values() if len(members) >= 3]


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


def _detour_nests(
    drawn: Sequence[PathLayer],
    seqs: Sequence[tuple[str, ...]],
    owner: dict[str, int],
) -> dict[int, int]:
    """Map each *detour* path to the sibling route it branches off — detection only.

    A route heard as another route *plus* an inserted relay or two — same convergence into us,
    one extra hop on the way — is that sibling with a detour, not an independent track: a
    bearing path whose relays, minus the ones it alone owns, exactly match a
    higher-or-equal-priority sibling's relays, where that sibling truly owns them (is their
    lane). What to *do* with the pair is the layout's call (:func:`_layout_lanes`): while the
    row budget allows, the detour nests just outside its sibling's lane
    (:func:`_compress_lanes`), and only a budget too tight for that folds it onto the sibling's
    lane itself (:func:`_fold_detour`). Returns ``{detour_index: sibling_index}``, weakest
    detours first in iteration order; ``owner`` is not modified.
    """
    relays = [
        frozenset(n for n in seq if n not in (SRC_NODE, DST_NODE)) for seq in seqs
    ]
    nests: dict[int, int] = {}
    # Weakest first, so a marginal detour pairs with its stronger sibling, never the reverse.
    for i in sorted(range(len(drawn)), key=lambda j: drawn[j].priority):
        own_i = {n for n in seqs[i] if owner[n] == i}
        residual = relays[i] - own_i
        if not own_i or not residual:
            continue
        for q in range(len(drawn)):
            if q == i or drawn[q].priority < drawn[i].priority or relays[q] != residual:
                continue
            if not all(owner[n] == q for n in relays[q]):  # q must own (be the lane of) them
                continue
            nests[i] = q
            break
    return nests


def _fold_detour(
    i: int,
    q: int,
    seqs: Sequence[tuple[str, ...]],
    owner: dict[str, int],
    col_of: Callable[[str], int],
) -> None:
    """Re-own detour path ``i``'s extra relays onto its sibling ``q``'s lane — the last resort.

    The single-lane picture a too-short viewport falls back to: the detour's owned relays are
    re-owned to the sibling, riding its lane as waypoints the branch dips through — at the cost
    of the sibling's own straight run passing over them — provided none shares a cell column
    with a node *already on that lane* (including a detour folded there earlier, so two siblings
    that insert a relay at the same column don't overprint; the refused one keeps its own lane).
    The folded path then owns nothing, earns no lane, and costs no band. Mutates ``owner`` in
    place.
    """
    own_i = {n for n in seqs[i] if owner[n] == i}
    q_cols = {
        col_of(n) for n, o in owner.items() if o == q and n not in (SRC_NODE, DST_NODE)
    }
    new_cols = {col_of(n) for n in own_i}
    if len(new_cols) == len(own_i) and q_cols.isdisjoint(new_cols):
        for n in own_i:
            owner[n] = q


def _band_rows(n_lanes: int, lane_pitch: int) -> int:
    """The canvas rows a band of ``n_lanes`` needs at full pitch — the sizing block's mirror.

    Mirrors ``render_path_graph``'s vertical sizing (its ``slot`` span plus the end margins) so
    the lane layout can weigh a candidate shape against ``max_rows`` *before* committing to it,
    rather than drawing it compressed and finding out.
    """
    max_lane = n_lanes - 1
    if max_lane <= 0:
        return 0
    lane_rows = max_lane * lane_pitch + (1 if max_lane % 2 == 1 else 0)
    return ceil((lane_rows * 4 + 2 * _GRAPH_END_DOTS) / 4)


def _layout_lanes(
    drawn: Sequence[PathLayer],
    seqs: Sequence[tuple[str, ...]],
    ordered_nodes: list[str],
    owner: dict[str, int],
    best: int,
    col_of: Callable[[str], int],
    max_rows: int,
    lane_pitch: int,
) -> dict[str, int]:
    """Seat every relay on a signed lane, spending rows on detours before folding them.

    The whole fan is weighed against the row budget before the shape is committed: the detour
    routes (:func:`_detour_nests`) first *nest* — each keeps its own lane just outside the
    sibling it branches off (:func:`_compress_lanes`), so the sibling's straight run visibly
    skips the inserted relay rather than passing over its marker. Only when that band would not
    fit ``max_rows`` at full pitch is a detour *folded* onto its sibling's lane
    (:func:`_fold_detour`) — weakest first, one at a time, re-laying and re-measuring until the
    band fits or no detours remain — so the everything-on-one-lane picture is the last resort,
    never the default. Whatever still overflows after every fold is the honest minimum and is
    left to the renderer's pitch compression. Mutates ``owner`` in place where it folds.

    Returns ``{node: signed_lane}`` for every relay — ``0`` the spine, ``<0`` above, ``>0``
    below — as :func:`_compress_lanes` yields it.
    """
    nests = _detour_nests(drawn, seqs, owner)
    while True:
        lane_of_path = _assign_lanes(drawn, seqs, owner, best)
        signed = _compress_lanes(ordered_nodes, lane_of_path, owner, best, col_of, nests)
        if not nests:
            return signed
        lanes = max(signed.values(), default=0) - min(signed.values(), default=0) + 1
        if _band_rows(lanes, lane_pitch) <= max_rows:
            return signed
        # Too tall for the viewport: fold the weakest detour onto its sibling's lane and try
        # again. A fold the column guard refuses still leaves the nest map (the route reverts
        # to a plain lane of its own), so the loop always runs out of detours and terminates.
        victim = min(nests, key=lambda i: drawn[i].priority)
        _fold_detour(victim, nests.pop(victim), seqs, owner, col_of)


def _bypass_vias(
    seqs: Sequence[tuple[str, ...]],
    bidir: set[frozenset[str]],
    signed: dict[str, int],
    col_of: Callable[[str], int],
    max_rows: int,
    lane_pitch: int,
) -> dict[frozenset[str], list[tuple[str, int]]]:
    """Bend each edge that runs level through a marker it skips — where the column has room.

    The subset pair is the trigger: beside an ``A → B → C → D`` route lives the shorter
    ``A → C → D``, and with ``A`` and ``C`` seated on one lane the shorter route's ``A→C``
    edge is a level run straight through ``B``'s cell — drawn, it reads as *via B*, the one
    story the evidence rules out (worse still under emphasis, where the subset's highlight
    repaints the spine's own run and the skipped relay looks selected). Any marker sitting
    between an edge's ends on their shared lane is by construction a node that edge skips:
    had the route visited it, the walk would hold ``A→B`` and ``B→C``, never ``A→C``. So each
    such edge is given a *virtual waypoint* — an unmarked via point in the skipped node's own
    column, on the innermost lane above or below it that is genuinely free — and the edge arcs
    through the via instead: out, level for an instant over the skipped marker's shoulder, and
    back — the same wider-arc grammar a nested detour draws, so a skip reads as a skip.

    Room is measured, never assumed. A lane at that column is free when no marker seats there
    and no route *runs level* through it across that column (a via on such a lane would peak
    tangent on that route's line and read as touching it); and a via may open a lane *outside*
    the current band only while the grown band still fits ``max_rows`` at full pitch
    (:func:`_band_rows`) — the same budget the detour fold answers to. The innermost free lane
    wins, the no-growth side breaking a depth tie (above on a dead heat); an edge whose skipped
    column truly has no room — every lane taken, growth unaffordable — keeps today's level
    pass-over, the honest last resort. Edges are visited in walk order (a set of string pairs
    would iterate hash-seeded and let two runs claim a contested lane differently), so the
    picture is identical on every repaint.

    Returns ``{edge pair: [(skipped node, via signed lane), …]}``, vias left to right, in the
    signed-lane space of ``signed``. The caller folds the via lanes into the band extent — so
    the vertical sizing affords any lane a bypass opened — and seats each via at the skipped
    node's exact x on that lane's row. Bidirectional pairs draw as the one honest vertical and
    are never bent.
    """
    if not signed:
        return {}
    low = min(signed.values())
    high = max(signed.values())
    centre = (low + high) / 2.0  # the endpoints' lane — integral only when a lane truly is

    def lane_of(node: str) -> float:
        return centre if node in (SRC_NODE, DST_NODE) else float(signed[node])

    cols = {node: col_of(node) for node in (*signed, SRC_NODE, DST_NODE)}

    # Every drawn edge once, in walk order; the level ones keep their lane and column span.
    level: list[tuple[frozenset[str], float, int, int]] = []
    seen: set[frozenset[str]] = set()
    for seq in seqs:
        for u, v in zip(seq, seq[1:]):
            key = frozenset((u, v))
            if key in seen or key in bidir:
                continue
            seen.add(key)
            if lane_of(u) == lane_of(v):
                c0, c1 = sorted((cols[u], cols[v]))
                level.append((key, lane_of(u), c0, c1))

    # What a via must not land on: every seated marker, and every column a level run sweeps
    # on its own lane (kissing another route's straight run reads as touching it).
    taken: set[tuple[int, float]] = {(cols[n], float(seat)) for n, seat in signed.items()}
    taken.add((cols[SRC_NODE], centre))
    taken.add((cols[DST_NODE], centre))
    for _key, lane, c0, c1 in level:
        for col in range(c0 + 1, c1):
            taken.add((col, lane))

    vias: dict[frozenset[str], list[tuple[str, int]]] = {}
    for key, lane, c0, c1 in level:
        skipped = sorted(
            (n for n, seat in signed.items() if float(seat) == lane and c0 < cols[n] < c1),
            key=lambda n: cols[n],
        )
        for m in skipped:
            # The innermost free lane each side of the skipped node, then the better of the
            # two: shallower first, the side that keeps the band's height on a depth tie.
            pick: Optional[tuple[int, int, int, int]] = None
            for side, sign in ((0, -1), (1, 1)):
                for depth in range(1, high - low + 3):
                    cand = signed[m] + sign * depth
                    if (cols[m], float(cand)) in taken:
                        continue
                    grows = int(cand < low or cand > high)
                    if grows:
                        n_lanes = max(high, cand) - min(low, cand) + 1
                        if _band_rows(n_lanes, lane_pitch) > max_rows:
                            break  # deeper on this side only grows further — give it up
                    if pick is None or (depth, grows, side) < pick[:3]:
                        pick = (depth, grows, side, cand)
                    break  # the innermost free lane on this side is found
            if pick is None:
                continue  # no room anywhere — the level pass-over stands
            via_lane = pick[3]
            taken.add((cols[m], float(via_lane)))
            low, high = min(low, via_lane), max(high, via_lane)
            vias.setdefault(key, []).append((m, via_lane))
    return vias


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


def _compress_lanes(
    ordered_nodes: list[str],
    lane_of_path: dict[int, int],
    owner: dict[str, int],
    best: int,
    col_of: Callable[[str], int],
    nests: Optional[dict[int, int]] = None,
) -> dict[str, int]:
    """Squeeze the per-path lanes onto the fewest rows, one node at a time within each column.

    :func:`_assign_lanes` seats each path on a lane of its own, so the band is as tall as the
    graph has routes — even where the routes only ever run one or two abreast. But a lane is a
    whole-width row: two routes need distinct rows only in the *columns* where they both carry a
    node, and endpoint-to-endpoint every route already shares the origin and us. So this pass
    keeps the best path's relays on the spine (offset ``0``) and slides every other node to the
    innermost free lane *above or below* it *in its own column*: a column with one node above
    the spine uses the first row above however many routes fan past it, and only a column where
    several routes truly stack claims the deeper rows.

    Which **side** each alternative takes is not inherited from its jog-order lane — that seats
    routes sharing a relay *adjacent*, which piles column-sharing routes onto the *same* flank
    and leaves the band as tall as one flank's deepest stack plus the other's, even when no
    single column holds more than two nodes. Instead :func:`_balance_sides` 2-colours the
    alternatives to flatten the deeper flank (the spine may lean off dead-centre to reach the
    band's middle, which is fine), so a five-route fan through at most two nodes per column draws
    three lanes — spine plus one flank each side — not five. The best path's relays still hold
    the straight spine; the returned lanes are signed offsets from it (``<0`` above, ``>0``
    below), which the caller shifts to a ``0``-based band.

    A *nested* detour (``nests``, from :func:`_detour_nests`) is held a whole lane **outside**
    the flank sibling it branches off: its route is pinned to the sibling's side and its own
    relays take a depth *floor* one past the sibling's, so the sibling's straight run — which
    sweeps through the detour relay's column on its own lane — never passes over the relay's
    marker. The detour then reads as the wider arc it is: out past the sibling, through its
    inserted relay, and back in to the shared node. (A detour off the *spine* needs no floor —
    the first flank row is already outside lane ``0``.)

    Returns ``{node: signed_lane}`` for every relay (endpoints are placed on the band centre by
    the caller, not here). A node a column holds alone off the spine always lands on ``±1`` —
    or ``±2`` when it is a nested detour's — so a sparse multi-route graph collapses to the
    three-lane spine-and-two-flanks it really is.
    """
    nests = nests or {}
    best_lane = lane_of_path[best]
    relays = [node for node in ordered_nodes if node not in (SRC_NODE, DST_NODE)]
    spine = {node for node in relays if owner[node] == best}
    off_spine = [node for node in relays if node not in spine]
    # The non-best bearing routes, in the jog-order _assign_lanes settled (each route keyed by
    # its signed lane relative to best), so ties fall to the arrangement that already reads clean.
    routes = sorted(
        {owner[node] for node in off_spine},
        key=lambda r: (lane_of_path[r] - best_lane, r),
    )
    cols_of_route = {
        r: {col_of(node) for node in off_spine if owner[node] == r} for r in routes
    }
    # A nested detour's own relays sit a lane outside its sibling's (the floor), on the same
    # flank (the tie); a detour whose sibling is the spine is just an ordinary flank route.
    floor = {r: 1 for r in routes}
    tie: dict[int, int] = {}
    for d, s in nests.items():
        if d in floor and s in floor:
            floor[d] = floor[s] + 1
            tie[d] = s
    orig_side = {r: (1 if lane_of_path[r] - best_lane > 0 else -1) for r in routes}
    side = _balance_sides(routes, cols_of_route, orig_side, floor, tie)

    rank = {r: i for i, r in enumerate(routes)}  # nearest-to-spine order within a flank
    columns: dict[int, list[str]] = {}
    for node in off_spine:
        columns.setdefault(col_of(node), []).append(node)

    signed: dict[str, int] = {node: 0 for node in spine}
    for members in columns.values():
        above = sorted(
            (n for n in members if side[owner[n]] < 0),
            key=lambda n: (floor[owner[n]], rank[owner[n]]),
        )
        below = sorted(
            (n for n in members if side[owner[n]] > 0),
            key=lambda n: (floor[owner[n]], rank[owner[n]]),
        )
        depth = 0
        for node in above:
            depth = max(depth + 1, floor[owner[node]])
            signed[node] = -depth
        depth = 0
        for node in below:
            depth = max(depth + 1, floor[owner[node]])
            signed[node] = depth
    return signed


def _balance_sides(
    routes: list[int],
    cols_of_route: dict[int, set[int]],
    orig_side: dict[int, int],
    floor: dict[int, int],
    tie: dict[int, int],
) -> dict[int, int]:
    """Choose a flank (``-1`` above / ``+1`` below the spine) for each alternative route.

    The band a :func:`_compress_lanes` pack draws is ``(deepest stack above) + (deepest stack
    below) + 1``: two alternatives need distinct rows only where they share a column, so a flank
    is only as deep as its most-crowded column. The jog order that seats sharing routes adjacent
    puts them on the *same* flank, which can stack one flank two deep while the other sits empty
    — a needlessly tall band. So the routes are 2-coloured, exhaustively while they are few
    (:data:`_MAX_BALANCE_ROUTES`; past it the jog-order sides stand):

    * a nested detour is pinned to its sibling's flank (``tie``) — nesting outside the sibling
      is the point, so a colouring that strands the pair apart is not a candidate — and a
      column's stack is measured with the detour's depth *floor*, so its outside row counts;
    * never above the **ceiling** the jog order itself draws — balancing may flatten a band, never
      grow one. A column that genuinely stacks three nodes forces one flank two deep whatever the
      colouring, and filling the other flank to match it (prettier, but taller) is refused;
    * under that ceiling, **flatten the deeper flank**, so a fan piled two deep on one side while
      the other is empty splits across both and the band shrinks;
    * then even the two flanks, then keep the jog-order side. So a graph the jog order already
      draws flat is left exactly as it is — its lower-band one-sided alternative has the *same*
      deepest flank, so the balance tie-break holds it airy rather than lopsiding it a row shorter
      — while a graph with a needlessly deep flank is the one that actually moves.

    Returns ``{route_index: side}``.
    """
    if not routes:
        return {}
    # The jog-order baseline, with each nested detour pulled onto its sibling's flank — the
    # tie-respecting shape the ceiling and the agreement tie-break are both measured against
    # (and the one assignment guaranteed to survive its own ceiling).
    forced = dict(orig_side)
    for d, s in tie.items():
        forced[d] = forced[s]
    if len(routes) > _MAX_BALANCE_ROUTES:
        return forced

    def flanks(assign: dict[int, int]) -> tuple[int, int]:
        deep = {-1: 0, 1: 0}
        for sign in (-1, 1):
            cols: dict[int, list[int]] = {}
            for r in routes:
                if assign[r] == sign:
                    for col in cols_of_route[r]:
                        cols.setdefault(col, []).append(r)
            for members in cols.values():
                # The same innermost-out stacking the pack applies: floors push a nested
                # detour's row outward even where the column holds nothing else.
                depth = 0
                for f in sorted(floor[r] for r in members):
                    depth = max(depth + 1, f)
                deep[sign] = max(deep[sign], depth)
        return deep[-1], deep[1]

    orig_above, orig_below = flanks(forced)
    ceiling = orig_above + orig_below  # the jog order's band height − 1; never draw taller

    best_assign: Optional[dict[int, int]] = None
    best_key: Optional[tuple[int, int, int]] = None
    for combo in product((-1, 1), repeat=len(routes)):
        assign = dict(zip(routes, combo))
        if any(assign[d] != assign[s] for d, s in tie.items()):  # a nest split off its sibling
            continue
        deep_above, deep_below = flanks(assign)
        if deep_above + deep_below > ceiling:  # taller than the jog order would draw — reject
            continue
        agree = sum(1 for r in routes if assign[r] == forced[r])
        key = (max(deep_above, deep_below), abs(deep_above - deep_below), -agree)
        if best_key is None or key < best_key:
            best_key, best_assign = key, assign
    assert best_assign is not None  # `forced` honours the ties and meets its own ceiling
    return best_assign


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
