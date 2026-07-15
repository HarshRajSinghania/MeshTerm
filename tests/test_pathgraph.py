"""Unit tests for the shared route-graph widget (the fan-lane braille renderer)."""

from __future__ import annotations

import re

from meshterm.ui.pathgraph import DST_NODE, SRC_NODE, PathLayer, render_path_graph

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

WHITE = (255, 255, 255)
GREEN = (74, 222, 128)
YELLOW = (250, 204, 21)


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
    """Endpoints now place like relays: the label lands even off the centre row."""
    layers = [PathLayer(tuple(f"{i:02x}" for i in range(6)), WHITE, 4)]
    lines = _render(
        layers,
        label_of=lambda node: "VeryLongStationName"
        if node in (SRC_NODE, DST_NODE)
        else None,
    )
    plain = _ANSI.sub("", "\n".join(lines))
    # The name is wider than the label budget: it must appear, ellipsized.
    assert "VeryLongSta…" in plain
