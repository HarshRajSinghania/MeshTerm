"""Header tests: the one-line status bar's width-filling activity sparkline."""

from __future__ import annotations

from types import SimpleNamespace

from meshterm.ui.menu import _HEADER_ACTIVITY_LEVELS, _header


def _ctx(histogram=(), unread=0, active=True, alerts=0) -> SimpleNamespace:
    """A minimal stand-in exposing only what the header reads (simulator path)."""
    return SimpleNamespace(
        mock=True,
        chat=SimpleNamespace(unread_total=lambda: unread),
        events=SimpleNamespace(active=active),
        monitor=SimpleNamespace(activity_histogram=lambda: tuple(histogram)),
        watchtower=SimpleNamespace(unacked_count=lambda: alerts),
    )


def test_header_shows_the_watchtower_badge() -> None:
    """Unacknowledged alerts appear as the triangle badge; none means no badge."""
    assert "▲ 2" in _header(_ctx(alerts=2), {}, 80).plain
    assert "▲" not in _header(_ctx(alerts=0), {}, 80).plain


def test_header_sparkline_fills_the_row_exactly() -> None:
    """Every cell the fixed segments leave is sparkline — no short rows, no overflow."""
    for width in (60, 100):
        header = _header(_ctx(histogram=(21,) * 360), {}, width)
        assert header.cell_len == width
        assert "⣿" in header.plain  # maxed-out cells all the way to the edge


def test_header_wider_terminal_shows_deeper_history() -> None:
    """A burst 90 minutes ago is off a narrow header but visible on a wide one.

    One minute per dot column: 90 minutes back needs 45 cells of sparkline, so it
    renders as a lit cell only when the row leaves that much room.
    """
    histogram = tuple(0 if i != 90 else 21 for i in range(360))
    narrow = _header(_ctx(histogram=histogram), {}, 60).plain
    wide = _header(_ctx(histogram=histogram), {}, 120).plain
    full_bar = chr(0x2800 | 0x47)  # a maxed-out lone left dot column
    assert full_bar not in narrow
    assert full_bar in wide


def test_header_thresholds_step_the_bar_heights() -> None:
    """The documented 1/3/8/21 shoulders map counts to 1–4 dots."""
    assert _HEADER_ACTIVITY_LEVELS == (1, 3, 8, 21)
    histogram = (1, 0, 3, 0, 8, 0, 21, 0) + (0,) * 352
    header = _header(_ctx(histogram=histogram), {}, 80).plain
    spark = header[header.index("●") + 2 :]  # after the live-light and its space
    left = (0x00, 0x40, 0x44, 0x46, 0x47)
    assert [spark[i] for i in range(4)] == [chr(0x2800 | left[h]) for h in (1, 2, 3, 4)]


def test_header_survives_a_sliver_terminal() -> None:
    """When the fixed segments already overflow the width, the sparkline just stays out."""
    header = _header(_ctx(histogram=(9,) * 360), {}, 10)
    # No room left: no braille at all, nothing stretched past the edge.
    assert all(not (0x2800 <= ord(ch) <= 0x28FF) for ch in header.plain)
