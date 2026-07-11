"""Header tests: the one-line status bar's width-filling activity sparkline."""

from __future__ import annotations

from types import SimpleNamespace

from meshterm.ui.menu import _HEADER_ACTIVITY_LEVELS, _header


def _ctx(histogram=(), unread=0, alerts=0) -> SimpleNamespace:
    """A minimal stand-in exposing only what the header reads (simulator path)."""
    return SimpleNamespace(
        mock=True,
        chat=SimpleNamespace(unread_total=lambda: unread),
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


def test_header_puts_now_at_the_right_edge() -> None:
    """The current minute's traffic draws in the row's final cell, not its first."""
    header = _header(_ctx(histogram=(21,) + (0,) * 359), {}, 80).plain
    spark = [ch for ch in header if 0x2800 <= ord(ch) <= 0x28FF]
    # The newest bucket is the final cell's *right* dot column (its left column is
    # the minute before, silent bar the continuing baseline dot).
    assert spark[-1] == chr(0x2800 | 0xB8 | 0x40)
    assert all(ch == chr(0x2800 | 0x40 | 0x80) for ch in spark[:-1])  # older = flatline


def test_header_wider_terminal_shows_deeper_history() -> None:
    """A burst 90 minutes ago is off a narrow header but visible on a wide one.

    One minute per dot column: 90 minutes back needs 45 cells of sparkline, so it
    renders as a lit cell only when the row leaves that much room.
    """
    histogram = tuple(0 if i != 90 else 21 for i in range(360))

    def burst_cells(row: str) -> list[str]:
        # Anything lit above the bottom dot row (bits beyond dots 7/8) is the burst;
        # the baseline flatline and blank braille never set those bits.
        return [ch for ch in row if 0x2800 <= ord(ch) <= 0x28FF and ord(ch) & 0x3F]

    assert not burst_cells(_header(_ctx(histogram=histogram), {}, 60).plain)
    assert burst_cells(_header(_ctx(histogram=histogram), {}, 120).plain)


def test_header_thresholds_step_the_bar_heights() -> None:
    """The documented 1/3/8/21 shoulders map counts to 1–4 dots."""
    assert _HEADER_ACTIVITY_LEVELS == (1, 3, 8, 21)
    histogram = (21, 8, 3, 1) + (0,) * 356
    header = _header(_ctx(histogram=histogram), {}, 80).plain
    spark = [ch for ch in header if 0x2800 <= ord(ch) <= 0x28FF]
    left = (0x00, 0x40, 0x44, 0x46, 0x47)
    right = (0x00, 0x80, 0xA0, 0xB0, 0xB8)
    # Flipped for display, the four newest minutes read 1,3,8,21 left→right in the
    # row's final two cells: heights (1,2) then (3,4).
    assert spark[-2:] == [chr(0x2800 | left[1] | right[2]), chr(0x2800 | left[3] | right[4])]


def test_header_tight_row_drops_to_compact_separators() -> None:
    """When roomy separators would squeeze the pulse, they narrow to ' · '."""
    roomy = _header(_ctx(), {}, 100).plain
    assert "  ·  " in roomy
    tight = _header(_ctx(unread=12, alerts=3), {}, 60).plain
    assert "  ·  " not in tight and " · " in tight
    # The compaction buys real sparkline room even on a badge-laden 60-column row.
    spark = [ch for ch in tight if 0x2800 <= ord(ch) <= 0x28FF]
    assert len(spark) >= 14


def test_header_survives_a_sliver_terminal() -> None:
    """When the fixed segments already overflow the width, the sparkline just stays out."""
    header = _header(_ctx(histogram=(9,) * 360), {}, 10)
    # No room left: no braille at all, nothing stretched past the edge.
    assert all(not (0x2800 <= ord(ch) <= 0x28FF) for ch in header.plain)