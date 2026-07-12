"""Braille chart widget tests: orientation, the zero baseline, scaling, and styles.

The module is pure Text-in/Text-out, so every behaviour is asserted straight off
the rendered braille bitmasks.
"""

from __future__ import annotations

from meshterm.ui.braillechart import (
    GAP,
    activity_sparkline,
    axis_chart,
    chart_span,
    timeline_rows,
    y_axis_labels,
)

#: Dot bits for a column filled bottom-up to height 0–4 (left / right column).
_L = (0x00, 0x40, 0x44, 0x46, 0x47)
_R = (0x00, 0x80, 0xA0, 0xB0, 0xB8)
_BASE = chr(0x2800 | 0x40 | 0x80)  # the two-bottom-dot zero flatline
_BLANK = chr(0x2800)


def _ch(*bits: int) -> str:
    mask = 0
    for b in bits:
        mask |= b
    return chr(0x2800 | mask)


# --- chart_span ---------------------------------------------------------------------


def test_chart_span_always_folds_zero_in() -> None:
    """Positive, negative, and mixed series all keep zero inside the span."""
    assert chart_span([3, 7]) == (0.0, 7.0)
    assert chart_span([-3, -7]) == (-7.0, 0.0)
    assert chart_span([-3, 7]) == (-3.0, 7.0)
    assert chart_span([None, None]) == (0.0, 0.0)
    assert chart_span([1], span=(-5, 10)) == (-5.0, 10.0)


# --- timeline_rows: all-positive series ------------------------------------------------


def test_timeline_peak_fills_the_height() -> None:
    """The series' peak fills every dot row; readings pair up two per cell."""
    rows = timeline_rows([12, 12, 0, 0], rows=3)
    assert len(rows) == 3
    assert all(len(r.plain) == 2 for r in rows)
    assert rows[0].plain[0] == _ch(_L[4], _R[4])  # the peak pair tops out
    assert rows[0].plain[1] == _BLANK
    assert rows[2].plain[1] == _BASE  # the silent pair keeps the zero line


def test_timeline_each_dot_column_has_its_own_height() -> None:
    """Two readings sharing a cell rise independently — braille's full resolution."""
    rows = timeline_rows([12, 6], rows=3)
    assert rows[0].plain[0] == _ch(_L[4])
    assert rows[1].plain[0] == _ch(_L[4], _R[2])
    assert rows[2].plain[0] == _ch(_L[4], _R[4])


def test_timeline_never_hides_a_lone_packet() -> None:
    """A tiny non-zero value still lights a dot, in the lit style not the floor's."""
    rows = timeline_rows([1, 0, 1000, 1000], rows=3)
    bottom = rows[2]
    assert bottom.plain[0] == _ch(_L[1], _R[1])  # bar dot + the baseline continuing
    assert bottom.spans[0].style == "ok"


def test_timeline_all_silent_is_a_flatline() -> None:
    """With nothing to draw the chart is a faint floor line, not a hole."""
    rows = timeline_rows([0, 0, 0, 0, 0, 0], rows=2)
    assert rows[1].plain == _BASE * 3
    assert rows[0].plain == _BLANK * 3
    assert all(span.style == "faint" for span in rows[1].spans)


def test_timeline_pads_an_odd_tail_column() -> None:
    """An odd reading count still renders whole cells, the floor continuing under it."""
    rows = timeline_rows([4], rows=1)
    assert len(rows[0].plain) == 1
    assert rows[0].plain[0] == _ch(_L[4], _R[1])  # full bar + the padded column's floor


# --- timeline_rows: GAP (a hard break in the baseline) --------------------------------


def test_timeline_gap_breaks_the_baseline_but_zero_keeps_it() -> None:
    """A GAP column draws nothing — not even the zero line — while a 0 keeps its dot.

    The cell pairs a GAP (left) with a plain 0 (right): the left column is fully
    blank down to the floor, the right column still shows its faint baseline dot —
    the distinction that lets a day-boundary notch reach the axis while an empty
    day stays a grey flatline.
    """
    rows = timeline_rows([GAP, 0], rows=1)
    assert rows[0].plain[0] == _ch(_R[1])  # only the right (0) column's baseline dot
    assert rows[0].spans and rows[0].spans[0].style == "faint"


def test_timeline_gap_column_stays_blank_beside_a_full_bar() -> None:
    """A GAP paired with a tall bar keeps its own column blank to the axis."""
    rows = timeline_rows([GAP, 4], rows=1)  # left GAP, right full-height bar
    # Right column carries the whole bar; the left (GAP) column lights no dot at all,
    # not even the bottom baseline the bar's cell would otherwise share.
    assert rows[0].plain[0] == _ch(_R[4])
    assert not (ord(rows[0].plain[0]) & 0x40)  # no bottom-left dot


def test_chart_span_ignores_gap_columns() -> None:
    """GAP carries no magnitude, so it never widens the span."""
    assert chart_span([GAP, 3, 7]) == (0.0, 7.0)
    assert chart_span([GAP, GAP]) == (0.0, 0.0)


# --- timeline_rows: negative and mixed series ------------------------------------------


def test_timeline_negative_series_hangs_from_a_top_baseline() -> None:
    """All-negative data pins zero to the ceiling and bars grow downward."""
    rows = timeline_rows([-4, -2], rows=1)
    # Left column full hang (0..3); right hangs two dots (2..3); the baseline row
    # (the cell's top dots) is inside both bars.
    assert rows[0].plain[0] == _ch(0x40, 0x04, 0x02, 0x01, 0x10, 0x08)


def test_timeline_mixed_series_puts_zero_mid_chart() -> None:
    """With readings both sides of zero, positives rise and negatives hang."""
    rows = timeline_rows([-10, 5], rows=3, span=(-10, 10))
    # Baseline lands on dot row 6: the full -10 hangs 0..6, the +5 rises 6..8.
    assert rows[0].plain[0] == _ch(0x80)  # the rise's head, above the baseline
    assert rows[1].plain[0] == _ch(0x46, 0x18)  # hang and rise straddle the zero row
    assert rows[2].plain[0] == _ch(0x47)  # the hang's tail, below everything


def test_timeline_baseline_shows_mid_chart_in_silent_cells() -> None:
    """A silent cell of a mixed-span chart draws the zero line where zero *is*."""
    rows = timeline_rows([-10, 5, None, None], rows=3, span=(-10, 10))
    assert rows[1].plain[1] == _ch(0x02, 0x10)  # both columns' dot on row 6, faint
    assert rows[1].spans[-1].style == "faint"
    assert rows[0].plain[1] == _BLANK
    assert rows[2].plain[1] == _BLANK


# --- styles -----------------------------------------------------------------------------


def test_timeline_per_cell_style_callable_sees_the_cells_readings() -> None:
    """A callable style is fed each lit cell's present readings."""
    seen: list[list[float]] = []

    def style(values: list[float]) -> str:
        seen.append(values)
        return "warn"

    rows = timeline_rows([2, 4, None, 6], rows=1, style=style)
    assert seen == [[2, 4], [6]]
    assert all(span.style == "warn" for span in rows[0].spans)


# --- activity_sparkline -------------------------------------------------------------------


def test_sparkline_puts_now_on_the_right() -> None:
    """A newest-first histogram renders with the current bucket at the right edge."""
    # Newest bucket maxed, everything older silent: only the final cell's *right*
    # dot column lights (with the floor dot keeping the left column's baseline).
    text = activity_sparkline((21, 0, 0, 0), (1, 3, 8, 21), 4)
    assert text.plain == _BASE + _ch(_R[4], 0x40)


def test_sparkline_thresholds_step_the_bar_heights() -> None:
    """The level shoulders map counts to 1–4 dots, oldest to the left."""
    # Newest-first (1, 3, 8, 21) reads 21,8,3,1 left→right once flipped.
    text = activity_sparkline((1, 3, 8, 21), (1, 3, 8, 21), 4)
    assert text.plain == _ch(_L[4], _R[3]) + _ch(_L[2], _R[1])


def test_sparkline_pads_and_crops_to_the_window() -> None:
    """A short histogram pads with silence; a long one crops to the newest buckets."""
    padded = activity_sparkline((21,), (1, 3, 8, 21), 6)
    assert padded.plain == _BASE * 2 + _ch(_R[4], 0x40)
    cropped = activity_sparkline((0, 0, 21, 21, 9, 9), (1, 3, 8, 21), 2)
    assert cropped.plain == _BASE  # only the two newest (silent) buckets survive


# --- y_axis_labels / axis_chart -------------------------------------------------------


def test_y_axis_labels_blanks_a_silent_chart() -> None:
    """A zero peak draws no marks at all."""
    assert y_axis_labels(0, 3) == ["", "", ""]


def test_y_axis_labels_top_row_reads_the_peak() -> None:
    """The top row always reads the peak; lower rows are proportional."""
    assert y_axis_labels(10, 2) == ["10", "5"]


def test_y_axis_labels_blanks_a_repeated_mark() -> None:
    """A low peak rounds a lower row to the same value as the one above: left blank."""
    assert y_axis_labels(1, 3) == ["1", "", ""]


def test_axis_chart_mirrors_marks_on_both_gutters() -> None:
    """Each row's mark is framed by tick glyphs and repeated on both edges."""
    rows = timeline_rows([9] * 8, rows=2)
    out = axis_chart(rows, 9, 4, lambda f: "now" if f >= 1.0 else "old")
    assert len(out) == 4  # 2 chart rows + bottom border + caption
    assert out[0].plain == "9 ┤⣿⣿⣿⣿├ 9"
    assert out[1].plain == "4 ┤⣿⣿⣿⣿├ 4"
    assert out[2].plain.startswith("  └") and out[2].plain.endswith("┘")


def test_axis_chart_honours_a_shared_label_width() -> None:
    """An explicit label_w widens the gutter past the peak's own digit count.

    Lets several stacked charts (the Time Machine's per-day pair) share one gutter
    width sized from the wider chart, so their marks line up column for column.
    """
    rows = timeline_rows([9] * 8, rows=1)
    out = axis_chart(rows, 9, 4, lambda f: "x", label_w=3)
    assert out[0].plain.startswith("  9 ┤")
