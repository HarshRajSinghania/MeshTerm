"""Scrolling braille bar charts: the one way MeshTerm draws a value-over-time row.

Every timeline in the app — the header's activity pulse, the channel manager's
per-channel sparklines, the dashboard's tall packet chart, the Time Machine's
histories — renders through this module, so they all share the same three rules:

* **Time flows left → right.** The rightmost column is *now*; history trails away
  to the left. Chronological (oldest-first) series feed :func:`timeline_rows`
  directly; the newest-first histograms the monitor and repository keep are passed
  as-is to :func:`activity_sparkline`, which reverses them into place.
* **Grey is zero.** Every chart draws a faint one-dot baseline at value zero and
  bars grow *from that line*, so a silent stretch reads as a flatline, never a
  hole. When a series carries negative values (an SNR history, say) the baseline
  sits at zero's height inside the chart — positive readings rise above it,
  negative ones hang below — rather than being nailed to the chart floor.
* **Two readings per cell.** Braille offers two dot columns per character cell;
  every chart uses both, so each cell shows two consecutive readings and the
  chart draws at twice the horizontal resolution of the cells it occupies.

The low-level cell assembly is shared: bars are expressed as inclusive dot-row
spans anchored on the baseline, and each character cell takes the bar style when
any bar dot falls in it, the faint baseline style when only the zero line does,
and stays blank braille otherwise (blank braille, not a space, so the grid stays
monospace under fonts with slightly odd braille metrics).
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Callable, Optional, Sequence, Union

from rich.text import Text

#: Braille dot bit for each dot row counted from the *bottom* of a cell (row 0 is
#: the cell's lowest dot), left and right columns. The Unicode braille block
#: numbers its rows top-down (dots 1,2,3,7 left / 4,5,6,8 right); these tables
#: flip that so chart math can stay in bottom-up "height" space throughout.
_LEFT_BITS = (0x40, 0x04, 0x02, 0x01)
_RIGHT_BITS = (0x80, 0x20, 0x10, 0x08)

#: How a lit cell is styled: a fixed Rich style name, or a callable given the
#: cell's present readings (one or two values) returning the style — the hook the
#: Time Machine uses to colour an SNR band by each slice's quality.
CellStyle = Union[str, Callable[[list[float]], str]]


def chart_span(
    values: Sequence[Optional[float]], span: Optional[tuple[float, float]] = None
) -> tuple[float, float]:
    """The vertical range a chart of ``values`` draws over, zero always included.

    The grey baseline *is* zero, so the span is the data's extent folded around it:
    all-positive data spans ``0 → peak`` (the baseline on the floor), all-negative
    data ``floor → 0`` (the baseline on the ceiling, bars hanging), mixed data both.
    Callers that caption their chart's scale should quote this same span so the
    label and the drawing can never disagree.

    Args:
        values: The chart's readings (``None`` marks an empty slot).
        span: An optional wider range to honour (it too is folded around zero).

    Returns:
        ``(lo, hi)`` with ``lo <= 0 <= hi``; ``(0.0, 0.0)`` for an empty series.
    """
    present = [v for v in values if v is not None]
    if span is not None:
        present = [*present, *span]
    lo = min([0.0, *present])
    hi = max([0.0, *present])
    return lo, hi


def timeline_rows(
    values: Sequence[Optional[float]],
    *,
    rows: int = 1,
    span: Optional[tuple[float, float]] = None,
    style: CellStyle = "ok",
    baseline_style: str = "faint",
) -> list[Text]:
    """Render a chronological series as a braille bar chart, newest at the right.

    ``values`` is oldest-first, one reading per dot column (two per character
    cell), scaled onto :func:`chart_span`'s zero-folded range: the series' peak
    fills the space above the baseline, its floor the space below, and any
    non-zero reading lights at least one dot so a lone packet never vanishes.
    ``None`` (no reading) and ``0`` alike draw only the faint zero baseline.

    Args:
        values: Per-slot readings, oldest first (the rightmost is "now"). An odd
            count is padded with one silent column so whole cells always render.
        rows: How many braille rows tall the chart is (four dot rows each).
        span: A wider range to scale against (see :func:`chart_span`), so several
            charts — or a chart and its caption — can share one scale.
        style: Style for lit cells: a Rich style name, or a callable given each
            cell's present readings (for per-slice colouring).
        baseline_style: Style for the zero line where nothing covers it.

    Returns:
        ``rows`` :class:`Text` lines, top row first, ``ceil(len(values)/2)``
        characters wide.
    """
    lo, hi = chart_span(values, span)
    total = rows * 4
    base = _baseline_row(lo, hi, total)
    up = total - base  # dot rows available to a full-scale positive bar
    down = base + 1  # …and to a full-scale negative one (baseline row included)

    bars: list[Optional[tuple[int, int]]] = []
    for value in values:
        if value is None or value == 0:
            bars.append(None)
        elif value > 0:
            height = max(1, round(value / hi * up))
            bars.append((base, base + height - 1))
        else:
            height = max(1, round(value / lo * down))
            bars.append((base - height + 1, base))
    return _assemble(bars, list(values), rows, base, style, baseline_style)


def activity_sparkline(
    histogram: Sequence[int],
    levels: tuple[int, ...],
    buckets: int,
    *,
    style: str = "ok",
) -> Text:
    """A one-row activity sparkline over a newest-first histogram, "now" rightmost.

    Bar heights are absolute, stepped at ``levels`` (the count a bucket must reach
    for each extra dot), so the same traffic always draws the same bar regardless
    of what else is on screen and a lone packet never vanishes. The histogram
    arrives newest-first — the natural order the monitor and repository keep — and
    is reversed here, so the current bucket lands on the right edge and traffic
    slides *left* as it ages, like every other MeshTerm timeline.

    Args:
        histogram: Per-bucket counts, newest first; padded/cropped to ``buckets``.
        levels: Ascending count thresholds for bar heights 1..4.
        buckets: How many buckets to draw (half this many characters).
        style: Style for lit cells.

    Returns:
        A styled Rich :class:`Text` of ``buckets / 2`` braille characters.
    """
    window = (tuple(histogram) + (0,) * buckets)[:buckets]
    bars: list[Optional[tuple[int, int]]] = []
    for count in reversed(window):
        height = bisect_right(levels, count)
        bars.append((0, height - 1) if height else None)
    return _assemble(bars, [float(c) for c in reversed(window)], 1, 0, style, "faint")[0]


def _baseline_row(lo: float, hi: float, total: int) -> int:
    """The dot row (from the chart bottom) the zero baseline sits on.

    All-positive data pins it to the floor, all-negative to the ceiling; a mixed
    span places it proportionally, clamped one row in from either edge so both
    directions keep at least one dot row to draw in.

    Args:
        lo: The span's floor (``<= 0``).
        hi: The span's ceiling (``>= 0``).
        total: The chart's height in dot rows.

    Returns:
        The baseline's dot row, ``0 .. total - 1``.
    """
    if lo == 0:
        return 0
    if hi == 0:
        return total - 1
    return min(total - 2, max(1, round((total - 1) * -lo / (hi - lo))))


def _column_bits(bar: Optional[tuple[int, int]], floor: int, table: tuple[int, ...]) -> int:
    """The braille bits one column's bar lights within a cell row.

    Args:
        bar: The bar's inclusive dot-row span (chart coordinates), or ``None``.
        floor: The cell row's bottom dot row in chart coordinates.
        table: :data:`_LEFT_BITS` or :data:`_RIGHT_BITS`.

    Returns:
        The OR of the covered dots' bits (0 when the bar misses this cell row).
    """
    if bar is None:
        return 0
    lo, hi = max(bar[0], floor), min(bar[1], floor + 3)
    bits = 0
    for dot in range(lo, hi + 1):
        bits |= table[dot - floor]
    return bits


def _assemble(
    bars: list[Optional[tuple[int, int]]],
    values: list[Optional[float]],
    rows: int,
    base: int,
    style: CellStyle,
    baseline_style: str,
) -> list[Text]:
    """Assemble bar spans into styled braille rows (the shared cell walk).

    Every bar span includes the baseline row by construction, so a half-silent
    cell keeps the zero line continuous inside the lit character; a fully silent
    cell shows just the faint baseline dots on whichever row holds them; anything
    else stays blank braille to keep the grid monospace.

    Args:
        bars: Per-column inclusive dot-row spans (``None`` = no bar).
        values: The readings behind the columns, aligned with ``bars`` (fed to a
            callable ``style`` two at a time, per cell).
        rows: Chart height in braille rows.
        base: The zero baseline's dot row.
        style: Fixed style, or per-cell callable (see :data:`CellStyle`).
        baseline_style: Style for baseline-only cells.

    Returns:
        ``rows`` :class:`Text` lines, top row first.
    """
    if len(bars) % 2:
        bars = [*bars, None]
        values = [*values, None]
    lines: list[Text] = []
    for row in range(rows):
        floor = (rows - 1 - row) * 4
        base_bit_left = _LEFT_BITS[base - floor] if floor <= base <= floor + 3 else 0
        base_bit_right = _RIGHT_BITS[base - floor] if floor <= base <= floor + 3 else 0
        line = Text()
        for i in range(0, len(bars), 2):
            left = _column_bits(bars[i], floor, _LEFT_BITS)
            right = _column_bits(bars[i + 1], floor, _RIGHT_BITS)
            if left or right:
                mask = left | right | base_bit_left | base_bit_right
                if callable(style):
                    present = [v for v in values[i : i + 2] if v is not None]
                    cell_style = style(present) if present else baseline_style
                else:
                    cell_style = style
                line.append(chr(0x2800 | mask), style=cell_style)
            elif base_bit_left:
                line.append(chr(0x2800 | base_bit_left | base_bit_right), style=baseline_style)
            else:
                line.append(chr(0x2800))
        lines.append(line)
    return lines
