"""Header tests: the one-line status bar's width-filling activity sparkline."""

from __future__ import annotations

from types import SimpleNamespace

from meshterm.ui.menu import _header


def _ctx(histogram=(), unread=0, alerts=0, battery=None) -> SimpleNamespace:
    """A minimal stand-in exposing only what the header reads (simulator path).

    ``battery`` is the poller's cached reading (a ``percent``/``charging`` object) or
    ``None`` for a companion with no pack — the default, so most tests see the old
    battery-free header.
    """
    return SimpleNamespace(
        mock=True,
        chat=SimpleNamespace(unread_total=lambda: unread),
        monitor=SimpleNamespace(
            activity_histogram=lambda: tuple(histogram),
            activity_session_flags=lambda: (True,) * len(tuple(histogram)),
        ),
        watchtower=SimpleNamespace(unacked_count=lambda: alerts),
        battery=SimpleNamespace(reading=lambda: battery),
    )


def test_header_shows_the_watchtower_badge() -> None:
    """Unacknowledged alerts appear as the triangle badge; none means no badge."""
    assert "▲ 2" in _header(_ctx(alerts=2), {}, 80).plain
    assert "▲" not in _header(_ctx(alerts=0), {}, 80).plain


def test_header_segments_style_each_run_on_its_own() -> None:
    """A segment's styles sit side by side, never layered one under the next.

    The segments are built as their own ``Text`` objects and joined (see
    :func:`~meshterm.ui.menu._header_segments`), which makes it easy to reach for a
    constructor ``style=`` — but that becomes the object's *base* style and blankets
    everything appended after it, so the version string would render brand-under-muted and
    each badge's count would inherit its glyph's error red.
    """
    header = _header(_ctx(unread=3, alerts=2), {}, 120)
    covering = {
        header.plain[span.start : span.end]: span.style
        for span in header.spans
        if span.style in ("brand", "err")
    }
    assert covering.get("MeshTerm") == "brand"  # not "MeshTerm v1.2.3"
    assert set(covering) == {"MeshTerm", "●", "▲"}  # neither badge's count is swept in


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


def test_header_scales_to_the_window_peak() -> None:
    """The pulse scales relative to a peak, not a fixed threshold ladder.

    A 16/12/8/4 ramp climbs one dot per step against the robust ceiling ``activity_peak``
    computes for it (~14.8 here — a floored 90th percentile of the counts), so the four
    newest minutes read heights 1,2,3,4 left→right in the row's final two cells.
    """
    histogram = (16, 12, 8, 4) + (0,) * 356
    header = _header(_ctx(histogram=histogram), {}, 80).plain
    spark = [ch for ch in header if 0x2800 <= ord(ch) <= 0x28FF]
    left = (0x00, 0x40, 0x44, 0x46, 0x47)
    right = (0x00, 0x80, 0xA0, 0xB0, 0xB8)
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


def _spark(row: str) -> list[str]:
    return [ch for ch in row if 0x2800 <= ord(ch) <= 0x28FF]


def test_header_pins_the_battery_gauge_to_the_right_edge() -> None:
    """A companion with a pack shows its charge in the corner; the pulse cedes the room."""
    reading = SimpleNamespace(percent=87, charging=False)
    header = _header(_ctx(histogram=(21,) * 360, battery=reading), {}, 80)
    assert header.cell_len == 80  # still fills the row exactly, gauge included
    assert header.plain.rstrip().endswith("87%")  # the gauge sits flush right
    # The pulse gave up real cells to make room (fewer sparkline cells than a bare row,
    # even counting the gauge's own full-cell glyph).
    with_batt = len(_spark(header.plain))
    bare = len(_spark(_header(_ctx(histogram=(21,) * 360), {}, 80).plain))
    assert with_batt < bare


def test_header_hides_the_gauge_without_a_battery() -> None:
    """No pack, no gauge — the pulse keeps the whole row it always had."""
    header = _header(_ctx(histogram=(21,) * 360, battery=None), {}, 80)
    assert "%" not in header.plain
    assert header.cell_len == 80
