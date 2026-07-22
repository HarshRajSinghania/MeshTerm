"""Unit tests for the shared route-graph widget (the diverge/converge braille renderer)."""

from __future__ import annotations

import re

from meshterm.ui.pathgraph import (
    DST_NODE,
    SRC_NODE,
    PathLayer,
    _coalesce_prefixes,
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
