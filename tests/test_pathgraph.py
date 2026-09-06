"""Unit tests for the shared route-graph widget (the diverge/converge braille renderer)."""

from __future__ import annotations

import re

import pytest

from meshterm.ui.pathgraph import (
    _GRAPH_PAD_DOTS,
    _LANE_PITCH_ROWS,
    _OCCURRENCE_SEP,
    DST_NODE,
    SRC_NODE,
    PathLayer,
    _assign_lanes,
    _balanced_x,
    _base_node,
    _bypass_vias,
    _coalesce_prefixes,
    _collapse,
    _layout_lanes,
    _route,
    _split_revisits,
    render_path_graph,
    revisited_hops,
)
from meshterm.ui.pathgraph import (
    _OFF_ROUTE as OFF_ROUTE,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

WHITE = (255, 255, 255)
GREEN = (74, 222, 128)
YELLOW = (250, 204, 21)
GREY = (120, 120, 120)


def _glyph(node: str) -> tuple[str, str]:
    # Deliberately off-palette marker colours, so colour assertions about *edges*
    # can never accidentally match a marker's escape sequence.
    if node in (SRC_NODE, DST_NODE):
        return ("★", "#123456")
    return ("●", "#654321")


def _render(layers, label_of=None):  # noqa: ANN001
    return render_path_graph(
        layers,
        60,
        glyph_of=_glyph,
        label_of=label_of or (lambda node: "you" if node in (SRC_NODE, DST_NODE) else node[:2]),
        label_rgb_of=lambda _node: WHITE,
    )


def _sgr(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"38;2;{r};{g};{b}m"


def test_no_layers_render_nothing() -> None:
    assert render_path_graph(
        [], 60, glyph_of=_glyph, label_of=lambda n: None, label_rgb_of=lambda n: WHITE
    ) == []


def test_identical_paths_collapse_to_the_top_layer() -> None:
    """The same route in two layers draws once, in the higher priority's colour."""
    layers = [
        PathLayer(("aa", "bb"), YELLOW, 2),
        PathLayer(("aa", "bb"), WHITE, 4),
    ]
    joined = "\n".join(_render(layers))
    assert _sgr(WHITE) in joined
    assert _sgr(YELLOW) not in joined


def test_emphasis_moves_the_highlight_not_the_layout() -> None:
    """Emphasis re-colours the drawn fan on top of a fixed geometry: the same layout, painted
    in different colours, so a caller can light a different route without it reflowing.
    """
    # A two-route fan whose spine (priority) is pinned to route 0. Only emphasis differs
    # between the two renders — were emphasis to drive geometry (as raising priority would),
    # route 1 becoming the highlight would seize the centre lane and shuffle the markers.
    def fan(emph: int):
        return [
            PathLayer(("aa",), WHITE if emph == 0 else GREY, 2, emphasis=1 if emph == 0 else 0),
            PathLayer(("bb",), WHITE if emph == 1 else GREY, 1, emphasis=1 if emph == 1 else 0),
        ]

    lit_spine = _render(fan(0))
    lit_alt = _render(fan(1))
    # Geometry is identical — strip the colour and the two frames are the same picture.
    assert _ANSI.sub("", "\n".join(lit_spine)) == _ANSI.sub("", "\n".join(lit_alt))
    # But the colouring moved: each frame still lights one route white, and the frames differ.
    assert _sgr(WHITE) in "\n".join(lit_spine) and _sgr(WHITE) in "\n".join(lit_alt)
    assert lit_spine != lit_alt


def test_bidir_clusters_finds_knots_not_pairs() -> None:
    """Three-plus mutually-bidirectional nodes are a cluster to contract; a tidy two-way pair
    (and any lone node) is not.
    """
    from meshterm.ui.pathgraph import bidir_clusters

    # A single two-way pair draws fine on its own — not a cluster.
    pair = [(SRC_NODE, "aa", "bb", DST_NODE), (SRC_NODE, "bb", "aa", DST_NODE)]
    assert bidir_clusters(pair) == []
    # aa<->bb and bb<->cc both ways make {aa, bb, cc} one strongly-connected knot.
    knot = [(SRC_NODE, "aa", "bb", "cc", DST_NODE), (SRC_NODE, "cc", "bb", "aa", DST_NODE)]
    groups = bidir_clusters(knot)
    assert len(groups) == 1 and set(groups[0]) == {"aa", "bb", "cc"}
    # Endpoints are never cluster members.
    assert SRC_NODE not in groups[0] and DST_NODE not in groups[0]


def test_emphasis_wins_a_shared_edge_over_a_higher_priority_spine() -> None:
    """A highlighted alternative paints its whole run — even the edge it shares with the spine —
    rather than dropping out where the higher-priority spine would otherwise own the colour.
    """
    # Both routes leave SRC through the same first relay (aa), so they share the SRC–aa edge;
    # the spine (priority 2, grey) would win that shared edge by priority, but the emphasised
    # alternative (priority 1, white) must win it by draw rank so its highlight stays unbroken.
    layers = [
        PathLayer(("aa", "bb"), GREY, 2),
        PathLayer(("aa", "cc"), WHITE, 1, emphasis=1),
    ]
    joined = "\n".join(_render(layers))
    assert _sgr(WHITE) in joined  # the emphasised route drew, shared edge and all


def test_distinct_layers_draw_in_their_own_colours() -> None:
    """Three different routes keep three edge colours on one canvas."""
    layers = [
        PathLayer(("aa", "bb"), YELLOW, 2),
        PathLayer(("cc",), GREEN, 3),
        PathLayer(("dd", "ee", "ff"), WHITE, 4),
    ]
    joined = "\n".join(_render(layers))
    for rgb in (YELLOW, GREEN, WHITE):
        assert _sgr(rgb) in joined


def test_labels_follow_the_callback_only_self_named() -> None:
    """A ``None`` label leaves the marker bare — a caller's unnamed-relay mode."""
    layers = [PathLayer(("ab", "cd"), WHITE, 4)]
    lines = _render(
        layers,
        label_of=lambda node: "Base" if node in (SRC_NODE, DST_NODE) else None,
    )
    plain = _ANSI.sub("", "\n".join(lines))
    assert "Base" in plain
    assert "ab" not in plain and "cd" not in plain


def test_endpoint_labels_may_leave_the_marker_row() -> None:
    """Endpoints place like relays: a long name lands whole, even off the centre row."""
    layers = [PathLayer(tuple(f"{i:02x}" for i in range(6)), WHITE, 4)]
    lines = _render(
        layers,
        label_of=lambda node: "VeryLongStationName"
        if node in (SRC_NODE, DST_NODE)
        else None,
    )
    plain = _ANSI.sub("", "\n".join(lines))
    # The name fits the 60-cell canvas, so it is kept whole — no fixed label budget clips it.
    assert "VeryLongStationName" in plain
    assert "…" not in plain


def test_revisited_hops_names_only_within_path_repeats() -> None:
    """The detector reports a hop touched twice by *one* path, in first-appearance order."""
    assert revisited_hops(("7f", "c1", "8e", "da", "ee", "c1", "27")) == ("c1",)
    assert revisited_hops(("aa", "bb", "aa", "cc", "bb")) == ("aa", "bb")
    assert revisited_hops(("aa", "bb", "cc")) == ()
    assert revisited_hops(()) == ()


def test_revisited_path_folds_into_a_cycle_that_piles_its_relays() -> None:
    """Why the flag exists: folded, a revisit makes a cycle the balanced rank cannot settle.

    ``c1`` twice closes a loop over ``8e``/``da``/``ee``. Ranked over that cyclic edge set the
    longest-path relaxation never reaches a fixed point, so the loop's members land within a
    hair of each other — one column of piled markers — while the relays outside it are squashed
    against the two ends. This pins the broken behaviour the flag is the escape from.

    Thresholds are loose because the folded numbers are not even *stable*: the relaxation
    iterates an edge **set**, which on an acyclic graph reaches the same fixed point whatever
    the order and on this cycle simply stops wherever the sweep budget ran out. So the pile's
    column swings with the interpreter's hash seed — the very frame-to-frame jumping the
    first-appearance node order exists to prevent. What every seed agrees on is the shape
    asserted here, and splitting the revisit is what restores the fixed point (see
    :func:`test_allow_duplicate_nodes_keeps_x_strictly_rising_along_the_walk`).
    """
    hops = ("7f", "c1", "8e", "da", "ee", "c1", "27")
    seq = (SRC_NODE, *hops, DST_NODE)
    ordered = list(dict.fromkeys(seq))
    x = _balanced_x(ordered, set(zip(seq, seq[1:])))
    # Evenly spread, these eight nodes would sit 1/7 apart and the four loop members would
    # span three of those gaps; folded, the whole loop fits inside a single one.
    loop = [x[node] for node in ("c1", "8e", "da", "ee")]
    assert max(loop) - min(loop) < 1 / 7
    assert x["7f"] < 0.15 and x["27"] > 0.85  # its neighbours crushed against the ends


def test_allow_duplicate_nodes_keeps_x_strictly_rising_along_the_walk() -> None:
    """Split, the rank regains the fixed point — and with it the widget's ordering invariant.

    ``_balanced_x`` promises the fraction rises strictly along every path, so edges only ever
    run left to right. A folded revisit breaks that (the cycle leaves the relaxation short of
    convergence, and even puts us at a lower rank than the hop before us); splitting restores
    it, which is also what makes the layout reproducible between repaints.
    """
    hops = ("7f", "c1", "8e", "da", "ee", "c1", "27")
    [layer] = _split_revisits([PathLayer(hops, WHITE, 3)])
    seq = (SRC_NODE, *layer.hops, DST_NODE)
    ordered = list(dict.fromkeys(seq))
    x = _balanced_x(ordered, set(zip(seq, seq[1:])))
    walked = [x[node] for node in seq]
    assert walked == sorted(walked) and len(set(walked)) == len(walked)
    assert x[SRC_NODE] == 0.0 and x[DST_NODE] == 1.0
    # A single walk with nothing else to balance against spreads dead evenly end to end.
    gaps = [b - a for a, b in zip(walked, walked[1:])]
    assert max(gaps) - min(gaps) < 1e-9


def test_allow_duplicate_nodes_spreads_a_revisited_path_evenly() -> None:
    """Split, the same walk is an acyclic run again: every hop its own evenly-spaced column."""
    hops = ("7f", "c1", "8e", "da", "ee", "c1", "27")
    layers = [PathLayer(hops, WHITE, 3)]
    plain = _ANSI.sub("", "\n".join(_render(layers)))
    split = _ANSI.sub(
        "",
        "\n".join(
            render_path_graph(
                layers, 60, glyph_of=_glyph,
                label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node[:2],
                label_rgb_of=lambda _node: WHITE,
                allow_duplicate_nodes=True,
            )
        ),
    )
    # Both visits of c1 draw their own marker, labelled identically — the node, twice.
    assert split.count("c1") == 2
    # Every other hop keeps exactly one marker: only the genuine revisit split.
    for hop in ("7f", "8e", "da", "ee", "27"):
        assert split.count(hop) == 1
    # Folded, that second marker does not exist — and the pile is tight enough that the label
    # placer sometimes cannot seat even the first one, so this is an at-most, not an exactly.
    assert plain.count("c1") <= 1


def test_allow_duplicate_nodes_still_merges_a_relay_two_paths_share() -> None:
    """Splitting is *within* a path only: the diverge/converge story survives untouched.

    Two routes riding one relay is the merge the widget exists for, and the flag must not
    break it — ``zz`` stays a single marker both lanes pass through.
    """
    layers = [PathLayer(("aa", "zz"), WHITE, 4), PathLayer(("bb", "zz"), GREY, 2)]
    plain = _ANSI.sub(
        "",
        "\n".join(
            render_path_graph(
                layers, 60, glyph_of=_glyph, label_of=lambda node: node[:2],
                label_rgb_of=lambda _node: WHITE, allow_duplicate_nodes=True,
            )
        ),
    )
    assert plain.count("zz") == 1


def test_split_revisits_qualifies_only_the_later_visits() -> None:
    """The first visit keeps the caller's bare id; later ones take a private qualifier."""
    [layer] = _split_revisits([PathLayer(("aa", "bb", "aa", "bb", "aa"), WHITE, 3)])
    assert layer.hops == ("aa", "bb", f"aa{_OCCURRENCE_SEP}1", f"bb{_OCCURRENCE_SEP}1",
                          f"aa{_OCCURRENCE_SEP}2")
    assert layer.color == WHITE and layer.priority == 3
    # Every qualified id maps back to the node the caller named it by.
    assert [_base_node(hop) for hop in layer.hops] == ["aa", "bb", "aa", "bb", "aa"]


def test_prefix_dupe_relay_folds_onto_its_only_wide_match() -> None:
    """A 1-byte hop beside the same node's wide id, across layers, becomes one node."""
    layers = [
        PathLayer(("e9043958308d", "be"), WHITE, 3),
        PathLayer(("3d63c6429436", "be1d1c1dbc4b"), GREY, 2),
    ]
    out = _coalesce_prefixes(layers)
    assert out[0].hops == ("e9043958308d", "be1d1c1dbc4b")  # "be" rewritten to the wide id
    assert out[1].hops == ("3d63c6429436", "be1d1c1dbc4b")


def test_ambiguous_prefix_relay_is_left_alone() -> None:
    """A short hop that prefixes two distinct present nodes is not guessed onto either."""
    layers = [
        PathLayer(("be",), WHITE, 3),
        PathLayer(("be1d1c1dbc4b",), GREY, 2),
        PathLayer(("bedd2bff0011",), GREY, 2),
    ]
    out = _coalesce_prefixes(layers)
    assert out[0].hops == ("be",)  # opens two present nodes — kept as its honest short self


def test_a_shared_relay_draws_a_single_marker() -> None:
    """A relay two paths pass through is one vertex, labelled once — never doubled."""
    layers = [PathLayer(("aa", "bb"), WHITE, 3), PathLayer(("cc", "bb"), YELLOW, 2)]
    plain = _ANSI.sub("", "\n".join(_render(layers)))
    assert plain.count("bb") == 1  # the shared relay is seated once
    assert plain.count("aa") == 1 and plain.count("cc") == 1


def _marker_rows(lines, glyph):  # noqa: ANN001, ANN202
    """The distinct canvas row indices a marker glyph is drawn on (endpoints ``★``, relays ``●``)."""
    return sorted({i for i, ln in enumerate(_ANSI.sub("", "\n".join(lines)).splitlines()) if glyph in ln})


def test_even_lane_count_centres_the_endpoints() -> None:
    """With an even number of lanes the origin and destination sit exactly midway between them.

    Two disjoint one-relay routes make two lanes — an upper and a lower. The endpoints ride
    neither: an extra padding row is opened between the two central lanes so they land on the
    exact centred row, rather than pinned to the upper lane (where the best path's discrete lane
    would put it) or snapped a row off centre.
    """
    layers = [
        PathLayer(("aa",), WHITE, 4),  # best -> upper lane
        PathLayer(("bb",), GREY, 2),   # -> lower lane
    ]
    lines = _render(layers, label_of=lambda _n: None)  # markers only — no label rows to muddy
    (star,) = _marker_rows(lines, "★")  # both endpoints share the one centre row
    top, bottom = _marker_rows(lines, "●")  # the two relays sit on their own lanes
    assert top < star < bottom  # centred between the lanes, not pinned to either
    assert star - top == bottom - star  # exact integer centring, thanks to the extra pad row


def test_odd_lane_count_seats_endpoints_on_the_straight_middle_lane() -> None:
    """An odd lane count gives the endpoints a real middle lane to ride — spine dead straight.

    Three disjoint one-relay routes make three evenly spaced lanes; the best path's relay owns
    the centre lane. The origin and destination share that centre row (no bend, no extra pad
    row), sitting midway between the two outer lanes.
    """
    layers = [
        PathLayer(("mm",), WHITE, 4),  # best -> centre lane
        PathLayer(("aa",), GREY, 2),   # -> a flanking lane
        PathLayer(("bb",), GREY, 1),   # -> the other flanking lane
    ]
    lines = _render(layers, label_of=lambda _n: None)
    (star,) = _marker_rows(lines, "★")
    top, mid, bottom = _marker_rows(lines, "●")  # three relays, one per lane
    assert star == mid  # endpoints ride the centre lane — same row as the best relay
    assert mid - top == bottom - mid  # lanes evenly spaced: no extra gap on an odd count


def test_every_label_shows_on_a_busy_graph() -> None:
    """Four distinct routes lay out as separated lanes — every relay label lands, none dropped."""
    names = {
        "aa": "Alpha", "bb": "Bravo", "cc": "Charlie", "dd": "Delta",
        "ee": "Echo", "ff": "Foxtrot", "gg": "Golf",
    }
    layers = [
        PathLayer(("aa", "bb"), WHITE, 4),
        PathLayer(("cc", "dd"), GREY, 2),
        PathLayer(("ee", "ff"), GREY, 2),
        PathLayer(("gg",), GREY, 2),
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers, label_of=lambda n: names.get(n[:2]))))
    for name in names.values():
        assert name in plain, f"{name} was dropped from the graph"


def _packed_lanes(layers, width=60, max_rows=15):  # noqa: ANN001
    """Run the lane pipeline through layout and compression, as ``render_path_graph`` does.

    Returns ``(signed, columns, route_lanes)``: the packed signed lane per relay (``0`` on the
    best spine, ``<0`` above, ``>0`` below), each relay's cell column, and how many lanes the
    per-path assignment used *before* packing — so a test can assert the pack squeezed them.
    ``max_rows`` is the budget the layout weighs detours against: roomy by default (the
    renderer's own default), pass it small to force the fold fallback.
    """
    drawn = _collapse(_coalesce_prefixes(layers))
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]
    ordered: list[str] = []
    seen: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for seq in seqs:
        for node in seq:
            if node not in seen:
                seen.add(node)
                ordered.append(node)
        edges.update(zip(seq, seq[1:]))
    xfrac = _balanced_x(ordered, edges)
    best = max(range(len(drawn)), key=lambda j: drawn[j].priority)
    owner: dict[str, int] = {}
    for i in sorted(range(len(drawn)), key=lambda j: -drawn[j].priority):
        for node in seqs[i]:
            owner.setdefault(node, i)
    route_lanes = max(_assign_lanes(drawn, seqs, owner, best).values()) + 1
    span = width * 2 - 2 * _GRAPH_PAD_DOTS
    col_of = lambda node: (_GRAPH_PAD_DOTS + round(xfrac[node] * span)) >> 1  # noqa: E731
    signed = _layout_lanes(
        drawn, seqs, ordered, owner, best, col_of, max_rows, _LANE_PITCH_ROWS
    )
    columns = {node: col_of(node) for node in signed}
    return signed, columns, route_lanes


def _lane_count(signed) -> int:  # noqa: ANN001
    """Number of distinct rows the packed lanes span (spine included)."""
    return max(signed.values()) - min(signed.values()) + 1


def test_detour_nests_outside_its_sibling_when_rows_allow() -> None:
    """A route that is a sibling *plus* one inserted relay arcs outside that sibling's lane.

    ``cc`` inserts a relay before the ``bb`` its sibling also converges through. With rows to
    spare the detour *nests*: ``cc`` takes the lane just outside ``bb``'s on the same flank, so
    the sibling's straight SRC → bb run never passes over ``cc``'s marker and the detour reads
    as the wider arc through ``cc`` it really is. This is the real Lakeside shape:
    CDN-FENDALL1 → UpperSalaberry.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),          # best spine: SRC -> xx -> DST
        PathLayer(("bb",), GREY, 2),           # sibling:    SRC -> bb -> DST
        PathLayer(("cc", "bb"), GREY, 2),      # detour:     SRC -> cc -> bb -> DST
    ]
    signed, _columns, _route_lanes = _packed_lanes(layers)
    assert signed["xx"] == 0  # the best route holds the spine
    assert abs(signed["bb"]) == 1  # the sibling takes the first flank row
    assert signed["cc"] == 2 * signed["bb"]  # cc one lane outside bb, same flank
    assert _lane_count(signed) == 3


def test_detour_folds_onto_its_siblings_lane_when_rows_are_tight() -> None:
    """The nested lane is given up — cc rides bb's lane — only when the rows can't afford it.

    The same Lakeside shape at a row budget too short for the nested third lane falls back to
    the single-lane picture: ``cc`` is re-owned onto ``bb``'s lane as a waypoint the branch
    dips through, and the band stays two lanes tall. The last resort, not the default.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("bb",), GREY, 2),
        PathLayer(("cc", "bb"), GREY, 2),
    ]
    signed, _columns, _route_lanes = _packed_lanes(layers, max_rows=8)
    assert signed["cc"] == signed["bb"]  # folded: cc rides bb's lane
    assert _lane_count(signed) == 2


def test_disjoint_routes_are_never_folded() -> None:
    """Routes that share no convergence relay each keep their own lane — folding stays targeted."""
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("aa",), GREY, 2),
        PathLayer(("bb",), GREY, 2),
    ]
    signed, _columns, _route_lanes = _packed_lanes(layers, max_rows=8)
    assert _lane_count(signed) == 3  # even at a tight budget, disjoint routes are not merged
    assert signed["aa"] == -signed["bb"]  # one flank each, balanced about the spine


def test_two_detours_sharing_a_column_nest_at_distinct_depths() -> None:
    """Two siblings inserting a relay at the same column stack outward — never one cell.

    ``cc`` and ``dd`` each sit one hop off the origin feeding the same ``bb``, so they share a
    column. Nested with room to spare, they take distinct rows outside ``bb``'s lane on its
    flank; markers never overprint.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("bb",), GREY, 2),
        PathLayer(("cc", "bb"), GREY, 2),
        PathLayer(("dd", "bb"), GREY, 1),
    ]
    signed, columns, _route_lanes = _packed_lanes(layers)
    assert columns["cc"] == columns["dd"]  # the two inserted relays truly share a column
    assert signed["cc"] != signed["dd"]  # so they hold distinct rows — no overprint
    for n in ("cc", "dd"):
        assert signed[n] * signed["bb"] > 0 and abs(signed[n]) > abs(signed["bb"])


def test_two_detours_sharing_a_column_do_not_overprint_when_folded() -> None:
    """At a tight budget the fold still refuses to stack two markers in one cell.

    Folding both ``cc`` and ``dd`` onto ``bb``'s lane would overprint their shared column, so
    at most one folds; the other keeps a row of its own and the two stay on distinct lanes.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("bb",), GREY, 2),
        PathLayer(("cc", "bb"), GREY, 2),
        PathLayer(("dd", "bb"), GREY, 1),
    ]
    signed, columns, _route_lanes = _packed_lanes(layers, max_rows=8)
    assert columns["cc"] == columns["dd"]
    assert signed["cc"] != signed["dd"]  # never stacked into one cell, however tight
    assert _lane_count(signed) == 3


def test_routes_pack_onto_shared_flanks_when_their_columns_differ() -> None:
    """Routes that diverge at different columns share a flank lane, so the band packs tight.

    Four routes: a three-relay best spine, and three alternatives that each swap a *different*
    one of the spine's relays (a different column). Given a full-width lane apiece they draw
    four deep, yet at no column do more than two nodes coexist — so the pack squeezes them onto
    the spine plus a single row above and below: three lanes, not four. This is the SUTTON-680M
    shape (five routes, ≤2 nodes per column) drawn small.
    """
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),  # spine
        PathLayer(("xx", "bb", "cc"), GREY, 2),   # swaps the first relay  (col of aa)
        PathLayer(("aa", "yy", "cc"), GREY, 2),   # swaps the middle relay (col of bb)
        PathLayer(("aa", "bb", "zz"), GREY, 2),   # swaps the last relay   (col of cc)
    ]
    signed, columns, route_lanes = _packed_lanes(layers)
    assert route_lanes == 4  # a lane per route before packing
    assert _lane_count(signed) == 3  # packed down to spine + one flank each side
    assert signed["aa"] == signed["bb"] == signed["cc"] == 0  # best rides the straight spine
    assert all(signed[n] != 0 for n in ("xx", "yy", "zz"))  # alternatives leave the spine
    # Two alternatives sharing a flank must sit in different columns (no overprint).
    for lane in {signed["xx"], signed["yy"], signed["zz"]}:
        on_lane = [n for n in ("xx", "yy", "zz") if signed[n] == lane]
        assert len({columns[n] for n in on_lane}) == len(on_lane)


def test_routes_stacking_in_one_column_keep_separate_lanes() -> None:
    """The pack never collapses routes that genuinely coexist in a column onto one row.

    Three alternatives all diverge through the *same* column off the origin, so three nodes
    truly stack there. Packing can't squeeze that — the column needs a distinct row per node —
    so the spine plus three stacked alternatives hold their own lanes; the floor is honest.
    """
    layers = [
        PathLayer(("aa", "zz"), WHITE, 4),  # spine relay aa, then shared zz near us
        PathLayer(("bb", "zz"), GREY, 3),   # bb shares aa's column
        PathLayer(("cc", "zz"), GREY, 2),   # cc shares aa's column
        PathLayer(("dd", "zz"), GREY, 1),   # dd shares aa's column
    ]
    signed, columns, _route_lanes = _packed_lanes(layers)
    diverging = ("aa", "bb", "cc", "dd")
    assert len({columns[n] for n in diverging}) == 1  # they all stack in one column
    assert len({signed[n] for n in diverging}) == 4  # so each keeps a row of its own


def test_balancing_splits_same_column_pairs_across_both_flanks() -> None:
    """Two routes that share a column split above and below, so their flank never stacks deep.

    The best spine runs through two relays ``c1`` and ``c2``. Two alternatives *swap* ``c1`` for a
    relay of their own (``x1``/``y1``, both landing in ``c1``'s column); two more swap ``c2``
    (``x2``/``y2``, both in ``c2``'s column). Seated by the jog order alone each pair lands on the
    *same* flank — one column two deep above the spine, the other two deep below — a five-lane
    band. Balancing the flanks splits each pair across the spine instead, so every column holds one
    node above and one below and the band is the spine plus a single row each side: three lanes.
    This is the 7bc505 shape drawn tight — the win the straight-spine-only pack left on the table.
    """
    layers = [
        PathLayer(("c1", "c2"), WHITE, 4),   # spine: SRC -> c1 -> c2 -> us
        PathLayer(("x1", "c2"), GREY, 2),    # swaps c1 -> x1 (x1 in c1's column)
        PathLayer(("y1", "c2"), GREY, 2),    # swaps c1 -> y1 (shares x1's column)
        PathLayer(("c1", "x2"), GREY, 2),    # swaps c2 -> x2 (x2 in c2's column)
        PathLayer(("c1", "y2"), GREY, 2),    # swaps c2 -> y2 (shares x2's column)
    ]
    signed, columns, route_lanes = _packed_lanes(layers)
    assert route_lanes == 5  # five full-width lanes before packing
    assert _lane_count(signed) == 3  # balanced down to spine + one flank each side
    assert signed["c1"] == signed["c2"] == 0  # spine relays stay on the straight centre
    for a, b in (("x1", "y1"), ("x2", "y2")):
        assert columns[a] == columns[b]  # each pair genuinely shares a column
        assert signed[a] == -signed[b]  # so the balancer seats one above, one below


def test_balancing_never_draws_a_taller_band_than_the_jog_order() -> None:
    """A column that truly stacks three nodes is not padded taller in the name of symmetry.

    ``ee``, ``ff`` and ``gg`` all fall in one column (three parallel detours off ``bf``), so one
    flank is unavoidably two deep whatever the colouring. Filling the opposite flank to match it
    would centre the picture but cost a row. The balancer refuses that: it never draws a band
    taller than the jog order already would, so the three-stack packs as two-up-one-down (four
    lanes), not two-up-two-down (five). This is the c5ba shape — the ceiling that guards it.
    """
    layers = [
        PathLayer(("bf", "tt"), WHITE, 4),         # spine: SRC -> bf -> tt -> us
        PathLayer(("bf", "ee", "dd"), GREY, 2),    # ee in the mid column, on to dd
        PathLayer(("bf", "ee", "ww"), GREY, 2),    # reuses ee, on to ww
        PathLayer(("bf", "ff", "dd"), GREY, 2),    # ff shares ee's column, rejoins dd
        PathLayer(("bf", "gg", "ww"), GREY, 2),    # gg shares ee's column, rejoins ww
    ]
    signed, columns, _route_lanes = _packed_lanes(layers)
    mid = ("ee", "ff", "gg")
    assert len({columns[n] for n in mid}) == 1  # the three detours genuinely stack in one column
    assert len({signed[n] for n in mid}) == 3  # each holds a distinct row — the honest floor
    assert _lane_count(signed) == 4  # spine + two-up-one-down, not padded to a symmetric five


def test_edge_pinned_relay_labels_are_not_dropped() -> None:
    """A relay a balanced rank pins near a canvas edge still shows its whole name.

    A long chain hung off ``cc`` drives its balanced rank hard against the origin and pushes
    the shared ``bb`` hard against us, so both land near an edge where a *centred* long label
    overhangs the canvas. Those anchors must slide inward — like an endpoint's — rather than
    place nothing and leave the marker silently unlabelled.
    """
    names = {"aa": "UpperStation", "bb": "RightEdgeStation", "cc": "LeftEdgeStation"}
    tail = tuple(f"g{i}" for i in range(1, 13))
    layers = [
        PathLayer(("bb",), WHITE, 3),
        PathLayer(("aa", "bb"), GREY, 2),
        PathLayer(("cc", *tail, "bb"), GREY, 1),
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers, label_of=lambda n: names.get(n))))
    for name in names.values():
        assert name in plain, f"{name} was dropped near a canvas edge"


def _vias_for(layers, width=60, max_rows=15):  # noqa: ANN001
    """Run the pre-render pipeline through the bypass pass, as ``render_path_graph`` does.

    Returns ``(vias, signed)``: the bypass via map keyed by unordered edge pair — each entry
    ``[(skipped node, via signed lane), …]`` — and the packed signed lane per relay, so a test
    can assert a via landed relative to the seated nodes.
    """
    drawn = _collapse(_coalesce_prefixes(layers))
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]
    ordered: list[str] = []
    seen: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for seq in seqs:
        for node in seq:
            if node not in seen:
                seen.add(node)
                ordered.append(node)
        edges.update(zip(seq, seq[1:]))
    xfrac = _balanced_x(ordered, edges)
    best = max(range(len(drawn)), key=lambda j: drawn[j].priority)
    owner: dict[str, int] = {}
    for i in sorted(range(len(drawn)), key=lambda j: -drawn[j].priority):
        for node in seqs[i]:
            owner.setdefault(node, i)
    span = width * 2 - 2 * _GRAPH_PAD_DOTS
    col_of = lambda node: (_GRAPH_PAD_DOTS + round(xfrac[node] * span)) >> 1  # noqa: E731
    signed = _layout_lanes(
        drawn, seqs, ordered, owner, best, col_of, max_rows, _LANE_PITCH_ROWS
    )
    bidir = {frozenset((u, v)) for (u, v) in edges if (v, u) in edges}
    vias = _bypass_vias(seqs, bidir, signed, col_of, max_rows, _LANE_PITCH_ROWS)
    return vias, signed


def test_subset_route_bypasses_the_relay_it_skips() -> None:
    """ABCD beside ACD: the skip edge arcs around the unridden relay via a free flank lane.

    The spine runs SRC → c1 → c2 → us with a balanced flank each side, so the endpoints ride
    the spine's own lane and the subset route's SRC → c2 edge is a level run straight through
    ``c1``'s marker — the Furthur → Hilltop-Repeater shape, where the selected route lit the
    spine's run through C14903 it never rides. The edge must bend around ``c1`` through a
    virtual waypoint in its column — and on the flank ``x1`` does *not* occupy, since ``x1``
    shares ``c1``'s column and its lane there is taken.
    """
    layers = [
        PathLayer(("c1", "c2"), WHITE, 4),   # spine: SRC -> c1 -> c2 -> us
        PathLayer(("x1", "c2"), GREY, 3),    # swaps c1 (x1 shares c1's column, one flank)
        PathLayer(("c1", "y2"), GREY, 2),    # swaps c2 (the other flank — spine stays centred)
        PathLayer(("c2",), GREY, 1),         # the subset: skips c1, rides c2
    ]
    vias, signed = _vias_for(layers)
    [(skipped, lane)] = vias[frozenset((SRC_NODE, "c2"))]
    assert skipped == "c1"  # the bypass shields exactly the relay the route skips
    assert lane == -signed["x1"]  # x1 holds its flank of c1's column — the via takes the other


def test_bypass_opens_a_lane_when_the_rows_afford_it() -> None:
    """With no flank to borrow, the bypass opens one — the band grows and the arc rides it."""
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),  # the whole graph on one straight lane
        PathLayer(("aa", "cc"), GREY, 2),         # the shortcut skipping bb
    ]
    vias, _signed = _vias_for(layers)
    [(skipped, lane)] = vias[frozenset(("aa", "cc"))]
    assert skipped == "bb"
    assert lane == -1  # a fresh lane just off the spine — above on a dead heat
    # And the render truly opens it: the endpoints leave the relays' row for the band centre.
    lines = _render(layers, label_of=lambda _n: None)
    (star,) = _marker_rows(lines, "★")
    (relay_row,) = _marker_rows(lines, "●")  # all three relays still level on the spine
    assert star < relay_row  # endpoints sit between the opened bypass lane and the spine


def test_bypass_gives_up_when_the_rows_cannot_afford_a_lane() -> None:
    """No free lane and no headroom: the honest level pass-over stands — never a taller band."""
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),
        PathLayer(("aa", "cc"), GREY, 2),
    ]
    vias, _signed = _vias_for(layers, max_rows=5)  # too short for a second lane at full pitch
    assert vias == {}


def test_route_threads_bypass_vias_level_at_each_peak() -> None:
    """A bent edge leaves level, peaks level on its via, and lands level — no corners."""
    pts = _route("u", "v", {"u": (0, 9), "v": (80, 9)}, bidir=False, vias=[(40, 1)])
    assert pts[0] == (0, 9) and pts[-1] == (80, 9)  # the markers still anchor the ends
    assert (40, 1) in pts  # the via is an anchor the edge truly passes through
    near_peak = [y for x, y in pts if 36 <= x <= 44]
    assert near_peak and all(y <= 2.0 for y in near_peak)  # level over the skipped marker
    assert pts[0][1] == pts[1][1] and pts[-1][1] == pts[-2][1]  # seated level at both ends


def test_bypass_render_keeps_every_label() -> None:
    """The arcs a bypass adds never crowd a name off the canvas."""
    names = {"c1": "C14903", "c2": "Cartier", "x1": "Poly", "y2": "Upper"}
    layers = [
        PathLayer(("c1", "c2"), GREY, 4),
        PathLayer(("x1", "c2"), GREY, 3),
        PathLayer(("c1", "y2"), GREY, 2),
        PathLayer(("c2",), WHITE, 1, emphasis=1),  # the subset selected, as on the node page
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers, label_of=lambda n: names.get(n))))
    for name in names.values():
        assert name in plain, f"{name} was dropped from the graph"


def test_a_subsumed_route_adds_no_duplicate_markers() -> None:
    """A route whose hops are all carried by a stronger one draws through them, not doubled."""
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),  # the backbone owns aa, bb, cc
        PathLayer(("aa", "cc"), GREY, 2),  # a shortcut over the same relays — no new node
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers)))
    for hop in ("aa", "bb", "cc"):
        assert plain.count(hop) == 1  # each relay seated exactly once


def test_route_seats_each_node_on_a_level_platform() -> None:
    """A lane-changing edge leaves and enters level, so neither node is a pointed apex."""
    pts = _route("u", "v", {"u": (0, 1), "v": (40, 9)}, bidir=False)
    assert pts[0] == (0, 1) and pts[-1] == (40, 9)  # the two markers are the ends
    assert pts[0][1] == pts[1][1]  # level out of u — u sits flat
    assert pts[-1][1] == pts[-2][1]  # level into v — v sits flat


def test_route_same_lane_is_one_level_run() -> None:
    """Two nodes on the same lane join with a single straight horizontal segment."""
    assert _route("u", "v", {"u": (0, 5), "v": (40, 5)}, bidir=False) == [(0, 5), (40, 5)]


def test_route_orients_left_to_right() -> None:
    """A right-to-left node pair is flipped, so the trapezium always reads as forward flow."""
    forward = _route("u", "v", {"u": (0, 1), "v": (40, 9)}, bidir=False)
    reverse = _route("u", "v", {"u": (40, 9), "v": (0, 1)}, bidir=False)
    assert reverse == forward  # same picture whichever way the pair is handed in


def test_route_bidirectional_is_a_single_straight_segment() -> None:
    """A two-way pair is the sole straight (near-vertical) edge — never a trapezium."""
    assert _route("u", "v", {"u": (20, 1), "v": (24, 30)}, bidir=True) == [(20, 1), (24, 30)]


def test_bidir_pair_does_not_jam_a_sibling_relay_against_the_origin() -> None:
    """A both-ways pair's 2-cycle must not inflate the rank and squash another relay left.

    ``aa`` and ``bb`` are walked in both directions (the vertical-drawn pair); ``cc`` is a
    plain relay one hop off the origin feeding ``bb``. Ranked over the raw edge set the cycle
    would drive ``cc`` toward ``0``; collapsing the pair leaves ``cc`` at its natural third of
    the way across, and the pair shares one x (they draw as a single vertical).
    """
    layers = [
        PathLayer(("bb",), WHITE, 3),
        PathLayer(("aa", "bb"), GREY, 2),
        PathLayer(("bb", "aa"), GREY, 2),  # the reverse — makes aa<->bb a 2-cycle
        PathLayer(("cc", "bb"), GREY, 1),
    ]
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in layers]
    ordered = list(dict.fromkeys(n for seq in seqs for n in seq))
    edges = {pair for seq in seqs for pair in zip(seq, seq[1:])}
    x = _balanced_x(ordered, edges)
    assert x[SRC_NODE] == 0.0 and x[DST_NODE] == 1.0
    assert x["cc"] == pytest.approx(1 / 3)  # its natural spot, not jammed toward 0
    assert x["aa"] == x["bb"]  # the pair shares one x — it draws as a single vertical
    assert x["cc"] < x["aa"]  # and still sits left of the pair it feeds


def test_labels_ellipsize_only_when_wider_than_the_canvas() -> None:
    """A name shortens only when it physically cannot fit — wider than the whole canvas."""
    name = "x" * 30
    lines = render_path_graph(
        [PathLayer(("aa",), WHITE, 4)],
        20,
        glyph_of=_glyph,
        label_of=lambda node: name if node in (SRC_NODE, DST_NODE) else None,
        label_rgb_of=lambda _node: WHITE,
    )
    plain = _ANSI.sub("", "\n".join(lines))
    assert name not in plain  # the full 30-cell name cannot fit a 20-cell canvas
    assert "…" in plain  # so it is ellipsized to what fits
    assert all(len(_ANSI.sub("", ln)) <= 20 for ln in lines)  # never overruns the width


def test_the_emphasised_path_is_the_only_one_drawn_in_colour() -> None:
    """Off the highlighted path, every marker and label recedes to the off-route grey.

    The fan says *which* route it is about only if everything not on it dims: the unused
    lines already draw grey, and a full-hue marker sitting on one is the loudest thing in
    the picture. The endpoints are on every path, so they keep their colour.
    """
    layers = [
        PathLayer(hops=("aa",), color=WHITE, priority=2, emphasis=1),
        PathLayer(hops=("bb", "cc"), color=GREY, priority=1),
    ]
    body = "\n".join(render_path_graph(
        layers, 60,
        glyph_of=_glyph,
        label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node,
        label_rgb_of=lambda _node: GREEN,
    ))
    lit = [ln for ln in body.split("\n") if "aa" in _ANSI.sub("", ln)]
    dim = [ln for ln in body.split("\n") if "bb" in _ANSI.sub("", ln)]
    assert lit and _sgr(GREEN) in lit[0], "the selected route's label keeps its hue"
    assert dim and _sgr(GREEN) not in dim[0]
    assert _sgr(OFF_ROUTE) in dim[0], "an off-route label draws in the off-route grey"
    # The marker recedes with the label: the relay glyph's own colour is drawn for the
    # on-route node and never for the off-route one, which draws grey twice over (mark + label).
    assert _sgr((0x65, 0x43, 0x21)) in body, "an on-route relay keeps its marker colour"
    assert body.count(_sgr(OFF_ROUTE)) > 1


def test_a_fan_with_nothing_emphasised_fades_nothing() -> None:
    """No emphasis is no selection: with every layer equal there is no route to recede from,
    and the caller's colours are used exactly as given.
    """
    layers = [
        PathLayer(hops=("aa",), color=WHITE, priority=2),
        PathLayer(hops=("bb",), color=GREY, priority=1),
    ]
    body = "\n".join(render_path_graph(
        layers, 60, glyph_of=_glyph,
        label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node,
        label_rgb_of=lambda _node: GREEN,
    ))
    assert _sgr(OFF_ROUTE) not in body
    assert body.count(_sgr(GREEN)) >= 2  # both relays' labels keep the hue they were given


def test_a_lone_path_is_never_faded() -> None:
    """One path has nothing to be read against, emphasised or not."""
    body = "\n".join(render_path_graph(
        [PathLayer(hops=("aa",), color=WHITE, priority=1, emphasis=1)], 60,
        glyph_of=_glyph, label_of=lambda node: node[:2], label_rgb_of=lambda _node: GREEN,
    ))
    assert _sgr(OFF_ROUTE) not in body


def test_the_highlight_lights_a_relay_reached_by_a_short_hash() -> None:
    """Membership is decided in the graph's own id space, after the prefix coalesce: a route
    that names a relay ``3d`` still lights the wide marker its hop is folded into.
    """
    layers = [
        PathLayer(hops=("3d63c6429436",), color=GREY, priority=2),
        PathLayer(hops=("3d",), color=WHITE, priority=1, emphasis=1),
    ]
    body = "\n".join(render_path_graph(
        layers, 60, glyph_of=_glyph,
        label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node[:2],
        label_rgb_of=lambda _node: GREEN,
    ))
    assert _sgr(OFF_ROUTE) not in body, "the one relay drawn is on the selected route"
    assert _sgr(GREEN) in body
