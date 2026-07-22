"""Unit tests for the shared route-graph widget (the diverge/converge braille renderer)."""

from __future__ import annotations

import re

import pytest

from meshterm.ui.pathgraph import (
    DST_NODE,
    SRC_NODE,
    PathLayer,
    _balanced_x,
    _coalesce_prefixes,
    _collapse,
    _fold_detours,
    _route,
    render_path_graph,
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


def _owner_after_fold(layers, width=72):  # noqa: ANN001
    """Build the per-node owner map and run the detour fold, as ``render_path_graph`` does."""
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
    owner: dict[str, int] = {}
    for i in sorted(range(len(drawn)), key=lambda j: -drawn[j].priority):
        for node in seqs[i]:
            owner.setdefault(node, i)
    _fold_detours(drawn, seqs, owner, xfrac, width)
    lanes = sum(1 for i in range(len(drawn)) if any(owner[n] == i for n in seqs[i]))
    return owner, lanes


def test_detour_path_rides_its_siblings_lane_not_a_new_one() -> None:
    """A route that is a sibling *plus* one inserted relay folds onto that sibling's lane.

    ``cc`` inserts a relay before the ``bb`` its sibling also converges through. Given its own
    lane on the far side of the best spine, it would drag that relay clear across the graph to
    rejoin ``bb`` (the crossing). Folded, ``cc`` is re-owned to ``bb``'s lane — two lanes, no
    crossing. This is the real YUL-Poly shape: CDN-FENDALL1 → UpperSalaberry.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),          # best spine: SRC -> xx -> DST
        PathLayer(("bb",), GREY, 2),           # sibling:    SRC -> bb -> DST
        PathLayer(("cc", "bb"), GREY, 2),      # detour:     SRC -> cc -> bb -> DST
    ]
    owner, lanes = _owner_after_fold(layers)
    assert owner["cc"] == owner["bb"]  # cc rides bb's lane rather than earning its own
    assert lanes == 2


def test_disjoint_routes_are_never_folded() -> None:
    """Routes that share no convergence relay each keep their own lane — folding stays targeted."""
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("aa",), GREY, 2),
        PathLayer(("bb",), GREY, 2),
    ]
    _owner, lanes = _owner_after_fold(layers)
    assert lanes == 3


def test_two_detours_sharing_a_column_do_not_overprint() -> None:
    """Two siblings inserting a relay at the same column can't both fold onto the shared lane.

    ``cc`` and ``dd`` each sit one hop off the origin feeding the same ``bb``, so they share a
    column; folding both onto ``bb``'s lane would stack two markers in one cell. Exactly one
    folds; the other keeps a lane of its own.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("bb",), GREY, 2),
        PathLayer(("cc", "bb"), GREY, 2),
        PathLayer(("dd", "bb"), GREY, 1),
    ]
    owner, lanes = _owner_after_fold(layers)
    folded = [n for n in ("cc", "dd") if owner[n] == owner["bb"]]
    assert len(folded) == 1  # only one rides bb's lane; the other stays separate
    assert lanes == 3


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
