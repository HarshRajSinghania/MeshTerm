"""The Time Machine: the mesh's recorded history, explorable node by node.

The interactive face of the ``timemachine`` tool. The database has been recording every
overheard packet since the first session, but nothing surfaced that history beyond the
dashboard's two-hour window — this screen is the archaeology dig. A picker offers the
whole mesh or any node ever heard; each subject renders as a scrollable page of braille
charts and stats over a switchable window (``w`` cycles 24 h → 7 d → 30 d → all time):

* a **node** shows its reception volume over the window, its median-SNR band (coloured
  by quality), its hour-of-day rhythm (when does this node talk?), and the roll-up
  stats — first/last heard, medians, extremes;
* the **whole mesh** shows packets per day and nodes per day across the history, the
  mesh-wide hour-of-day rhythm, the arrivals of the window (nodes heard for the first
  time ever, in aligned name/hash/first-heard lanes), and the all-time totals.

Charts read chronologically — oldest at the left, now at the right, the app-wide
timeline direction — and draw through :mod:`~meshterm.ui.braillechart`, so the grey
baseline always marks zero: the SNR band's readings hang below it or rise above it
by their actual sign. Everything is stored data — no device is needed and nothing
transmits.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Callable, Optional

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.models import utcnow
from .braillechart import _TICK_GAP, GAP, axis_chart, chart_span, timeline_rows
from .menus import fit_cells, section_heading
from .theme import snr_style
from .tui.render import render_lines
from .tui.screen import Screen
from .widgets import (
    _DEFAULT_GLYPH,
    _NODE_GLYPHS,
    _age_seconds,
    _format_age,
    _recency_style,
    format_ago,
    highlighted_hash,
)

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import HeardNode
    from ..services.trace_runner import NodeResolver

#: The switchable history windows (``None`` = everything ever recorded).
_WINDOWS: tuple[tuple[str, Optional[timedelta]], ...] = (
    ("24 h", timedelta(days=1)),
    ("7 d", timedelta(days=7)),
    ("30 d", timedelta(days=30)),
    ("all time", None),
)

#: Picker sentinel for the whole-mesh overview page.
MESH = ("mesh",)

#: How many braille rows tall the volume/rhythm charts draw.
_CHART_ROWS = 2

#: How many braille rows tall the SNR band draws — one more than the volume charts,
#: because zero-anchoring (readings hang below the grey zero line by their actual
#: depth) spends some dots on honesty and the extra row buys the swing back.
_SNR_ROWS = 3


def bucketize(stamps: list[datetime], start: datetime, end: datetime, buckets: int) -> list[int]:
    """Fold timestamps into ``buckets`` equal slices of ``[start, end]``, oldest first."""
    counts = [0] * max(1, buckets)
    span = max(1.0, (end - start).total_seconds())
    for stamp in stamps:
        index = int((stamp - start).total_seconds() / span * buckets)
        counts[min(buckets - 1, max(0, index))] += 1
    return counts


def bucket_medians(
    pairs: list[tuple[datetime, float]], start: datetime, end: datetime, buckets: int
) -> list[Optional[float]]:
    """Per-bucket medians of timestamped readings (``None`` for empty buckets)."""
    grouped: list[list[float]] = [[] for _ in range(max(1, buckets))]
    span = max(1.0, (end - start).total_seconds())
    for stamp, value in pairs:
        index = int((stamp - start).total_seconds() / span * buckets)
        grouped[min(buckets - 1, max(0, index))].append(value)
    return [median(values) if values else None for values in grouped]


def _snr_cell_style(values: list[float]) -> str:
    """Colour one SNR-band cell by its readings' quality (the shared SNR palette)."""
    return snr_style(sum(values) / len(values))


def _time_axis(start: datetime, end: datetime) -> Callable[[float], str]:
    """A compact axis labeller over a real time span, closing on ``now``.

    A window wider than a couple of days reads its marks as bare dates (``Jul 4``); a
    tighter one, where the calendar day barely changes, reads them as times (``18:30``)
    — the same "shorten what you can" the day charts' dates use, so the volume and SNR
    axes stay legible instead of repeating a full ``Jul 04 18:30`` stamp at every tick.
    """
    wide = (end - start).total_seconds() > 2 * 86400

    def label_at(frac: float) -> str:
        if frac >= 1.0:
            return "now"
        when = (start + (end - start) * frac).astimezone()
        return f"{when:%b} {when.day}" if wide else f"{when:%H:%M}"

    return label_at


def _quarter_axis(frac: float) -> str:
    """The mesh rhythm's labeller: the local hour at ``frac`` of a full-day quarter-hour sweep.

    The 96 fifteen-minute slices span midnight to midnight, so the fraction maps onto the
    whole ``0 → 24 h`` day (the right edge closing on ``24 h``), landing the intermediate
    marks on clean six-hour boundaries.
    """
    return f"{round(frac * 24)} h"


def _heading(title: str, note: str) -> Text:
    """A section heading in the dashboard's voice: accent title, muted note."""
    text = Text(title, style="accent")
    text.append(f"  ·  {note}", style="muted")
    return text


class TimeMachineScreen(Screen):
    """One subject's history page: scrollable sections, ``w`` cycles the window."""

    floating = False
    footer_hint = "w window · ↑↓ PgUp/PgDn scroll · Esc back"

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        label: str,
        build: Callable[[Optional[timedelta], int], list[RenderableType]],
    ) -> None:
        """Create the page over its section builder.

        Args:
            session: The running TUI session (for repaints on window switch).
            label: The subject's display name (titles the screen).
            build: Renders the sections for ``(window, width)``; called once per
                window/width combination and cached — the data is stored history,
                so nothing needs re-querying per repaint.
        """
        super().__init__()
        self._session = session
        self._label = label
        self._build = build
        self._window_index = 1  # open on 7 d: enough depth to see shape, still fast
        self._cache: dict[tuple[int, int], list[str]] = {}
        self._set_title()

    def _set_title(self) -> None:
        name, _delta = _WINDOWS[self._window_index]
        self.title = f"{self._label} · {name}"

    def handle(self, action: str, data: str = "") -> None:
        """Scroll, cycle the window, or dismiss."""
        if action == "up":
            self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self.scroll_to_bottom()
        elif action == "text" and data.lower() == "w":
            self._window_index = (self._window_index + 1) % len(_WINDOWS)
            self._set_title()
            self.scroll_to_top()
            self._session.invalidate()
        elif action == "escape":
            self.resolve(None)

    def render_body(self, width: int) -> list[str]:
        """Render (or reuse) the current window's sections."""
        key = (self._window_index, width)
        lines = self._cache.get(key)
        if lines is None:
            _name, delta = _WINDOWS[self._window_index]
            lines = render_lines(Group(*self._build(delta, width)), width)
            self._cache[key] = lines
        self._scroll_total = max(1, len(lines))
        return lines


# --- the node page -----------------------------------------------------------------------


def _when_label(when: datetime) -> str:
    """A compact local timestamp for chart captions: ``Jul 04 18:30``."""
    return when.astimezone().strftime("%b %d %H:%M")


def _node_sections(
    ctx: "AppContext", node_id: str, label: str, window: Optional[timedelta], width: int
) -> list[RenderableType]:
    """Build one node's history page: volume, SNR band, rhythm, and the roll-up."""
    now = utcnow()
    since = now - window if window is not None else None
    observations = ctx.repo.node_observations(node_id, since=since)
    if not observations:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]
    start = since or observations[0].observed_at
    stamps = [o.observed_at for o in observations]
    snr_pairs = [
        (o.observed_at, float(o.snr)) for o in observations if o.snr is not None
    ]

    # Volume, SNR, and the rhythm share one y-axis gutter width (like the mesh page's
    # charts) so their left edges line up. A provisional width finds the peaks that size
    # the gutter, then the real width re-buckets Volume/SNR flush with it. The rhythm folds
    # every reception into 96 fifteen-minute local-time slices — a slice's tally can top a
    # single volume bucket's — so its peak joins the sizing too.
    slots = [0] * 96
    for stamp in stamps:
        local = stamp.astimezone()
        slots[local.hour * 4 + local.minute // 15] += 1

    def _layout(label_w: int) -> tuple[int, int]:
        chars = max(20, width - 2 * (label_w + 2))
        return chars, chars * 2

    chars, buckets = _layout(1)
    volume = bucketize(stamps, start, now, buckets)
    lo, hi = chart_span(bucket_medians(snr_pairs, start, now, buckets)) if snr_pairs else (0.0, 0.0)
    label_w = max(
        1, len(str(max(volume))), len(str(round(hi))), len(str(round(lo))), len(str(max(slots)))
    )
    chars, buckets = _layout(label_w)
    volume = bucketize(stamps, start, now, buckets)

    out: list[RenderableType] = []
    out.append(_heading("Volume", f"{len(observations)} receptions"))
    out.extend(
        axis_chart(
            timeline_rows(volume, rows=_CHART_ROWS),
            max(volume), chars, _time_axis(start, now), label_w=label_w,
        )
    )

    if snr_pairs:
        medians = bucket_medians(snr_pairs, start, now, buckets)
        lo, hi = chart_span(medians)
        rows = timeline_rows(medians, rows=_SNR_ROWS, style=_snr_cell_style)
        out.append(Text())
        out.append(_heading("SNR", "median dB per slice · grey line = 0"))
        out.extend(
            axis_chart(
                rows, hi, chars, _time_axis(start, now), label_w=label_w, floor=lo,
            )
        )

    out.append(Text())
    out.append(_heading("Rhythm", "receptions by local time of day · 15-min slices"))
    out.extend(
        axis_chart(
            timeline_rows(slots, rows=_CHART_ROWS), max(slots), 48,
            _quarter_axis, label_w=label_w,
        )
    )

    out.append(Text())
    out.append(_heading("Record", "this window"))
    first, last = observations[0].observed_at, observations[-1].observed_at
    line = Text("heard    ", style="muted")
    line.append(f"first {_when_label(first)} · last {_when_label(last)}")
    line.append(f"  ({format_ago(_age_seconds(last))})", style="muted")
    out.append(line)
    snrs = [p[1] for p in snr_pairs]
    if snrs:
        med = median(snrs)
        line = Text("snr      ", style="muted")
        line.append(f"{med:+.1f} dB median", style=snr_style(med))
        line.append(f"  ·  worst {min(snrs):+.1f} · best {max(snrs):+.1f}", style="muted")
        out.append(line)
    rssis = [o.rssi for o in observations if o.rssi is not None]
    if rssis:
        out.append(
            Text.assemble(
                ("rssi     ", "muted"), (f"{median(rssis):.0f} dBm median", ""),
                (f"  ·  weakest {min(rssis):.0f} · strongest {max(rssis):.0f}", "muted"),
            )
        )
    kinds: dict[str, int] = {}
    for o in observations:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1
    parts = " · ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
    out.append(Text.assemble(("kinds    ", "muted"), (parts, "")))
    return out


# --- the mesh page -----------------------------------------------------------------------


def _day_spans(days: int, chars: int) -> list[tuple[int, int]]:
    """Each day's lit-bar ``(start, width)`` within the chart's ``2 × chars`` dots.

    The one even split behind both the bars (:func:`_day_columns`) and their axis
    ticks (:func:`_day_centers`). When every bar can keep at least two dots, each
    of the ``n − 1`` day *boundaries* pays one unclaimed notch dot and the bars
    split the rest with a floor-edge walk, so no two differ by more than one dot
    column and the wider days interleave evenly among the narrower ones instead
    of pooling at either end. The notches sit strictly between days: the first
    bar starts flush at dot 0 (the axis border is separation enough — a leading
    blank dot would double the left margin the braille glyphs already carry)
    and the last ends flush at the right border, so both edges read alike. A
    denser history drops the notches and the bars simply tile all ``2 × chars``
    dots.

    Args:
        days: How many day bars the chart draws.
        chars: The chart's width in character cells.

    Returns:
        One ``(start dot, width in dots)`` per lit bar, oldest first.
    """
    dots = chars * 2
    n = max(1, days)
    lit = dots - (n - 1)  # what the bars keep once each boundary pays its notch dot
    notch = n > 1 and lit // n >= 2
    if not notch:
        lit = dots
    edges = [i * lit // n for i in range(n + 1)]
    return [
        (edges[i] + (i if notch else 0), edges[i + 1] - edges[i]) for i in range(n)
    ]


def _day_centers(days: int, chars: int) -> list[int]:
    """The chart cell each day's bar is centred on, mirroring :func:`_day_columns`.

    Reads the same :func:`_day_spans` split ``_day_columns`` draws with, so a tick
    placed at ``centers[i]`` lands under day ``i``'s bar rather than at an
    arbitrary fraction of the axis.

    Args:
        days: How many day bars the chart draws.
        chars: The chart's width in character cells.

    Returns:
        One centre cell (``0 .. chars - 1``) per day, oldest first.
    """
    return [
        min(chars - 1, (start + width // 2) // 2)
        for start, width in _day_spans(days, chars)
    ]


def _even_picks(n: int, k: int) -> list[int]:
    """``k`` bar indices out of ``n``, evenly spread with both ends included.

    The endpoints are always index ``0`` and ``n − 1``; the interior lands on the
    even fractions between. Rounding can collapse two picks onto one bar when ``k``
    approaches ``n`` on a short axis, so the result is de-duplicated and may hold
    fewer than ``k`` — the caller treats that as "``k`` does not fit" and steps down.

    Args:
        n: How many bars the chart draws.
        k: How many ticks to spread across them (``1 .. n``).

    Returns:
        The chosen bar indices, ascending.
    """
    if n <= 0:
        return []
    if k <= 1:
        return [0]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def _ticks_fit(picks: list[int], labels: list[str], centers: list[int], chars: int) -> bool:
    """Whether ``labels`` placed under ``picks`` clear each other on the axis.

    Replays the exact placement :func:`~meshterm.ui.braillechart._tick_axis` runs —
    each label centred on its bar's cell, clamped into the axis, needing
    :data:`~meshterm.ui.braillechart._TICK_GAP` cells past the previous label's end —
    and reports whether every one survives. Measuring the real labels (a bare ``"7"``
    is a third the width of ``"Jul 12"``) is the point: a fixed worst-case budget per
    label would drop ticks a variable-width axis has ample room for.
    """
    last_end = -_TICK_GAP
    for i, label in zip(picks, labels):
        cell = max(0, min(chars - 1, centers[i]))
        start = max(0, min(chars - len(label), cell - len(label) // 2))
        if start < last_end + _TICK_GAP:
            return False
        last_end = start + len(label)
    return True


def _fit_ticks(
    n: int, chars: int, centers: list[int], label_of: Callable[[list[int]], list[str]]
) -> list[tuple[int, str]]:
    """``(cell, label)`` ticks: as many bars as the *actual* labels leave room for.

    Tries every bar first, then steps the tick count down until an evenly spread
    subset (:func:`_even_picks`) clears the collision rule (:func:`_ticks_fit`) for
    the labels it would actually draw. Both ends stay annotated at every count.
    Because the labels are rebuilt for each candidate set — day numbers stay bare
    until a month rollover forces ``"Jul 1"`` — the fit tracks their true widths
    rather than reserving the longest label's width for all of them.

    Args:
        n: How many bars the chart draws.
        chars: The chart's width in character cells.
        centers: Each bar's centre cell (see :func:`_day_centers`).
        label_of: Builds the labels for a set of picked bar indices, in context
            (so month rollovers and the closing ``today``/``now`` land correctly).

    Returns:
        The ticks to pass to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    if n <= 0:
        return []
    for k in range(min(n, chars), 1, -1):
        picks = _even_picks(n, k)
        if len(picks) < k:
            continue  # rounding fused two picks — this many will not fit cleanly
        labels = label_of(picks)
        if _ticks_fit(picks, labels, centers, chars):
            return list(zip((centers[i] for i in picks), labels))
    picks = [0] if n == 1 else [0, n - 1]
    return list(zip((centers[i] for i in picks), label_of(picks)))


def _day_ticks(shown: list, chars: int) -> list[tuple[int, str]]:
    """``(cell, label)`` axis ticks under the day bars: short dates, thinned to fit.

    One tick per day where the labels all clear each other, else an evenly spaced
    subset keeping both ends (see :func:`_fit_ticks`); each label sits under its own
    bar (see :func:`_day_centers`). Dates are kept compact — the month is shown only
    on the first tick and whenever it rolls over, so most ticks read as a bare day
    number — and the newest bar reads ``today`` when it is, mirroring the node page's
    closing ``now``.

    Args:
        shown: The charted days, oldest first, each ``(iso_date, ...)``.
        chars: The chart's width in character cells.

    Returns:
        The ticks to pass to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    n = len(shown)
    centers = _day_centers(n, chars)
    # The day keys are local calendar days (see Repository.daily_activity), so "today"
    # is the local date too — datetime.now() is naive local, exactly what we compare.
    today = datetime.now().strftime("%Y-%m-%d")

    def label_of(picks: list[int]) -> list[str]:
        labels: list[str] = []
        prev_month: Optional[str] = None
        for i in picks:
            iso = shown[i][0]
            try:
                day = datetime.strptime(iso, "%Y-%m-%d")
            except ValueError:
                labels.append(iso)
                continue
            month = day.strftime("%b")
            if i == n - 1 and iso == today:
                label = "today"
            elif month != prev_month:
                label = f"{month} {day.day}"
            else:
                label = str(day.day)
            prev_month = month
            labels.append(label)
        return labels

    return _fit_ticks(n, chars, centers, label_of)


def _hour_ticks(shown: list, chars: int) -> list[tuple[int, str]]:
    """``(cell, label)`` axis ticks under the hourly bars: local clock times, thinned to fit.

    The hour-resolution sibling of :func:`_day_ticks` for the 24 h window: one tick
    per hour where the labels fit, else an evenly spaced subset keeping both ends
    (see :func:`_fit_ticks`), each label centred under its own bar (see
    :func:`_day_centers`). The hour keys are already local (see
    :meth:`~meshterm.persistence.repository.Repository.hourly_series`), so a label is
    just its key's ``HH:00``, no conversion; the newest bar reads ``now``, mirroring
    the node page's closing ``now``.

    Args:
        shown: The charted hours, oldest first, each ``(hour_iso, ...)`` a local
            ``YYYY-MM-DDTHH``.
        chars: The chart's width in character cells.

    Returns:
        The ticks to pass to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    n = len(shown)
    centers = _day_centers(n, chars)

    def label_of(picks: list[int]) -> list[str]:
        labels: list[str] = []
        for i in picks:
            if i == n - 1:
                labels.append("now")
                continue
            try:
                hour = datetime.strptime(shown[i][0], "%Y-%m-%dT%H")
            except ValueError:
                labels.append(shown[i][0])
                continue
            labels.append(f"{hour:%H:00}")
        return labels

    return _fit_ticks(n, chars, centers, label_of)


def _fill_days(
    active: list[tuple[str, int, int]], since: Optional[datetime], now: datetime
) -> list[tuple[str, int, int]]:
    """Fill a day series' gaps so an empty day shows as an empty bar, not a skip.

    ``daily_activity`` returns only days with traffic, so a quiet day used to vanish and
    its busy neighbours fused — a fortnight with two dead days reading as twelve adjacent
    bars. This walks every calendar day of the window and emits ``(iso, 0, 0)`` for the
    silent ones, so the chart's x-axis is real calendar time. The range runs from the
    window's floor (but never earlier than the first day ever recorded — we don't invent
    emptiness from before monitoring began) through today.

    Args:
        active: ``(iso_day, packets, nodes)`` for days with activity, oldest first.
        since: The window's start (``None`` = all of history).
        now: The current time (the series ends on its calendar day).

    Returns:
        ``(iso_day, packets, nodes)`` for every calendar day in range, oldest first.
    """
    if not active:
        return []
    by_iso = {iso: (packets, nodes) for iso, packets, nodes in active}
    # The keys are local calendar days, so bound the fill by local dates too — .date()
    # on the raw UTC-aware since/now would floor a day early in western zones.
    first = date.fromisoformat(active[0][0])
    floor = since.astimezone().date() if since is not None else first
    start = max(first, floor)
    end = max(start, now.astimezone().date())
    out: list[tuple[str, int, int]] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        packets, nodes = by_iso.get(iso, (0, 0))
        out.append((iso, packets, nodes))
        day += timedelta(days=1)
    return out


def _fill_hours(
    active: list[tuple[str, int, int]], since: datetime, now: datetime
) -> list[tuple[str, int, int]]:
    """Fill an hour series' gaps so a quiet hour shows as an empty bar, not a skip.

    The hour-resolution sibling of :func:`_fill_days`, feeding the 24 h window's
    charts: :meth:`~meshterm.persistence.repository.Repository.hourly_series` returns
    only hours with traffic, so a silent hour would fuse its busy neighbours. This
    walks every local clock hour of the window and emits ``(iso, 0, 0)`` for the quiet
    ones, so the x-axis is real clock time. The range runs from the window's floor
    (but never earlier than the first hour recorded — we don't invent emptiness from
    before monitoring began) through the current hour.

    The keys are local wall-clock ``YYYY-MM-DDTHH`` (matching the SQL grouping), so the
    walk steps a *naive* local clock: adding an hour advances the wall clock, which is
    DST-robust — a spring-forward gap fills as an empty bar and a fall-back repeat sums
    into one key, exactly as the grouping already did.

    Args:
        active: ``(hour_iso, packets, nodes)`` for hours with activity, oldest first,
            each ``hour_iso`` a local ``YYYY-MM-DDTHH``.
        since: The window's start.
        now: The current time (the series ends on its clock hour).

    Returns:
        ``(hour_iso, packets, nodes)`` for every local clock hour in range, oldest
        first.
    """
    if not active:
        return []

    def floor_hour(when: datetime) -> datetime:
        return when.astimezone().replace(tzinfo=None, minute=0, second=0, microsecond=0)

    by_iso = {iso: (packets, nodes) for iso, packets, nodes in active}
    first = datetime.strptime(active[0][0], "%Y-%m-%dT%H")
    start = max(first, floor_hour(since))
    end = max(start, floor_hour(now))
    out: list[tuple[str, int, int]] = []
    hour = start
    while hour <= end:
        iso = hour.strftime("%Y-%m-%dT%H")
        packets, nodes = by_iso.get(iso, (0, 0))
        out.append((iso, packets, nodes))
        hour += timedelta(hours=1)
    return out


def _day_columns(values: list[int], chars: int) -> list:
    """Stretch per-day counts into day-wide bars that fill the chart's width exactly.

    One dot column per day leaves a short history as a sliver in a wide terminal —
    beneath how every other MeshTerm chart spends its width — so each day repeats
    over its :func:`_day_spans` share of the chart's dot columns instead: an even
    split whose widths never differ by more than one dot and whose total always
    lands on exactly ``2 × chars`` (a plain floor division drops the remainder,
    leaving the bars short of the axis border and caption sized for the full
    width — misreading as the whole chart sitting shifted left).

    The dots the split leaves unclaimed — one per day *boundary*, when the bars
    are wide enough to afford them (see :func:`_day_spans`) — render as
    :data:`~meshterm.ui.braillechart.GAP` columns, blank clean down to the axis,
    so same-height neighbours read as separate bars instead of fusing into one
    solid block. A deeper history (bars of one dot) has no boundary to spare and
    the days simply abut.

    Args:
        values: Per-day counts, oldest first.
        chars: The chart's width in character cells.

    Returns:
        Exactly ``2 × chars`` dot-column readings, oldest first (a
        :data:`~meshterm.ui.braillechart.GAP` marks a day-boundary notch).
    """
    out: list = [GAP] * (chars * 2)
    for value, (start, width) in zip(values, _day_spans(len(values), chars)):
        out[start : start + width] = [value] * width
    return out


def _mesh_sections(
    ctx: "AppContext",
    window: Optional[timedelta],
    width: int,
    prefix_bytes: int = 0,
    resolve: "NodeResolver" = lambda label: label,
) -> list[RenderableType]:
    """Build the whole-mesh overview: days, rhythm, arrivals, and the all-time ledger.

    Args:
        ctx: The shared application context (repository reads only).
        window: The history window (``None`` = everything ever recorded).
        width: Render width in columns.
        prefix_bytes: Path-hash width to light in the arrival hashes (0 = none).
        resolve: Names a node hash from the device's contacts, for arrivals whose
            observations never carried a name.
    """
    now = utcnow()
    since = now - window if window is not None else None
    # The 24 h window charts an hour per column (its name is "24 h", not "1 day");
    # every wider window keeps the calendar-day columns. Both feed the same bar and
    # tick machinery — only the bucket resolution and the axis labels differ.
    hourly = since is not None and window is not None and window <= timedelta(days=1)
    if hourly:
        series = _fill_hours(ctx.repo.hourly_series(since), since, now)
    else:
        series = _fill_days(ctx.repo.daily_activity(), since, now)
    if not series:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]

    # The mesh-wide rhythm (charted below) folds the whole window into 96 fifteen-minute
    # slices, so a busy slice's tally can top any single day's — compute it up front so its
    # peak joins the day peaks in sizing one shared y-axis gutter. The slices come back
    # already in local time (rotated per-instant in SQL), so no offset shuffle here.
    slots = ctx.repo.quarter_hour_activity(since=since)

    # The y-axis gutter is sized from the whole window's peaks (not just the visible
    # slice) and shared by every chart, so all their gutters — and thus their left edges —
    # line up. The rhythm keeps its own finer width; only the gutter is common.
    label_w = max(
        len(str(max(d[1] for d in series))),
        len(str(max(d[2] for d in series))),
        len(str(max(slots))),
    )
    chars = max(20, width - 2 * (label_w + 2))
    shown = series[-chars * 2 :]
    out: list[RenderableType] = []
    packets = [d[1] for d in shown]
    ticks = _hour_ticks(shown, chars) if hourly else _day_ticks(shown, chars)
    pkt_title, node_title = (
        ("Packets per hour", "Nodes per hour") if hourly
        else ("Packets per day", "Nodes per day")
    )
    out.append(_heading(pkt_title, "local hours" if hourly else "local days"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(packets, chars), rows=_CHART_ROWS),
            max(packets), chars, label_w=label_w, ticks=ticks,
        )
    )

    nodes = [d[2] for d in shown]
    out.append(Text())
    out.append(_heading(node_title, "distinct nodes heard"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(nodes, chars), rows=_CHART_ROWS),
            max(nodes), chars, label_w=label_w, ticks=ticks,
        )
    )

    # The node page's rhythm chart, mesh-wide and four times finer: when does this *mesh*
    # talk? The 96 fifteen-minute slices (grouped in local time by SQL) keep their own
    # 48-cell width but share the day charts' gutter, so this chart's left edge lines up
    # with the two above it.
    out.append(Text())
    out.append(_heading("Rhythm", "packets by local time of day · 15-min slices"))
    out.extend(
        axis_chart(
            timeline_rows(slots, rows=_CHART_ROWS), max(slots), 48,
            _quarter_axis, label_w=label_w,
        )
    )

    arrivals = ctx.repo.first_seen(since=since)
    out.append(Text())
    out.append(_heading("Arrivals", "nodes heard for the first time ever"))
    if not arrivals:
        out.append(Text("none in this window", style="muted"))
    else:
        # Aligned lanes under column labels, the picker's presentation: the name
        # coloured by heat ("unknown" included), the hash lit at the routing width.
        # The FIRST HEARD header carries what used to be repeated on every row.
        # A nameless arrival first asks the resolver (the device may know the node
        # as a contact even though its stored observations never carried a name).
        listed = [
            (node, _known_name(resolve, node, name), first)
            for node, name, first in arrivals[:12]
        ]
        name_w = min(
            _PICK_NAME_MAX,
            max([len("unknown"), *(len(n) for _node, n, _f in listed if n)]),
        )
        out.append(
            Text(
                "  "
                + "NAME".ljust(name_w + 2)
                + "HASH".ljust(_PICK_HASH_W + 2)
                + "FIRST HEARD",
                style="muted",
            )
        )
        for node, name, first in listed:
            secs = _age_seconds(first)
            line = Text("  ", no_wrap=True, overflow="ellipsis")
            line.append(fit_cells(name or "unknown", name_w), style=_recency_style(secs))
            line.append("  ")
            line.append_text(highlighted_hash(node, prefix_bytes, width=_PICK_HASH_W))
            line.append("  ")
            line.append(_when_label(first))
            line.append(f"  ({format_ago(secs)})", style="muted")
            out.append(line)

    all_days = ctx.repo.daily_activity()
    total_nodes = len(ctx.repo.first_seen())
    busiest = max(all_days, key=lambda d: d[1])
    out.append(Text())
    out.append(_heading("Ledger", "everything ever recorded"))
    out.append(
        Text.assemble(
            ("history  ", "muted"),
            (f"{ctx.repo.observation_count()} observations", ""),
            (f" across {len(all_days)} active days · {total_nodes} nodes", "muted"),
        )
    )
    out.append(
        Text.assemble(
            ("busiest  ", "muted"), (busiest[0], "brand"),
            (f"  {busiest[1]} packets", "muted"),
        )
    )
    return out


# --- the picker loop ---------------------------------------------------------------------

#: Widest the picker's name lane grows (longer names ellipsize so the lanes stay put).
_PICK_NAME_MAX = 18

#: The picker's hash lane: heard-node ids are the observations' 12-hex key prefixes.
_PICK_HASH_W = 12


def _known_name(resolve: "NodeResolver", node: Optional[str], name: Optional[str]) -> Optional[str]:
    """The best display name for a heard node, or ``None`` when truly unknown.

    The app-wide fill-in-the-blanks rule: a node id without a stored name is not
    necessarily a mystery — the device's contact list may know it. The stored name
    wins (it's what the node itself last put on the air); the resolver fills the
    blanks; only a node neither source can name stays ``unknown``.
    """
    if name:
        return name
    if node:
        named = resolve(node)
        if named and named != node:
            return named
    return None


def _picker_header(name_w: int) -> str:
    """Column labels over the node picker's lanes (see :func:`_picker_row`).

    The four leading spaces cover the select screen's pointer column (2 cells) plus
    the one-cell type glyph and its gap, so each label lands over its lane.
    """
    return (
        "    "
        + "NAME".ljust(name_w + 2)
        + "HASH".ljust(_PICK_HASH_W + 2)
        + f"{'PKTS':>5}"
        + "  "
        + f"{'HEARD':>5}"
    )


def _picker_row(node: "HeardNode", name: Optional[str], name_w: int, prefix_bytes: int) -> Text:
    """One heard node as fixed, colour-coded picker lanes.

    The type glyph leads (the app's shared marker palette), the name — stored, or
    filled in by the contact resolver (see :func:`_known_name`) — is coloured by
    recency heat, ``unknown`` included, so a freshly heard mystery node still reads
    hot. The hash is a separate lane in the shared hash widget, its path-hash prefix
    lit at the device's routing width, exactly as the Nodes list draws keys. Packet
    count and age close the row, right-aligned under their headers.
    """
    glyph, glyph_style = _NODE_GLYPHS.get(node.node_type, _DEFAULT_GLYPH)
    secs = _age_seconds(node.last_seen)
    row = Text(no_wrap=True, overflow="ellipsis")
    row.append(glyph, style=glyph_style)
    row.append(" ")
    row.append(fit_cells(name or "unknown", name_w), style=_recency_style(secs))
    row.append("  ")
    row.append_text(highlighted_hash(node.node or "", prefix_bytes, width=_PICK_HASH_W))
    row.append("  ")
    row.append(f"{min(node.count, 99999):>5}", style="muted")
    row.append("  ")
    row.append(f"{_format_age(secs):>5}", style="muted")
    return row


async def _routing_prefix_bytes(ctx: "AppContext") -> int:
    """The device's path-hash width in bytes, or 0 when unknowable.

    Best-effort, exactly like the Nodes tool: the Time Machine reads stored history
    and must work with no radio at all, so an unreachable device (or firmware that
    doesn't report the mode) just leaves every hash un-highlighted.
    """
    try:
        if not (ctx.is_connected or ctx.settings.connect_on_start):
            return 0
        mode = await ctx.devstate.path_hash_mode()
    except Exception:  # noqa: BLE001 - optional read; absence just skips highlighting
        return 0
    return (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0


async def _contact_resolver(ctx: "AppContext") -> "NodeResolver":
    """A name resolver over the device's contacts, best-effort like the prefix read.

    Fills the picker's and arrivals' ``unknown`` blanks for nodes the companion
    knows as contacts but whose stored observations never carried a name (a
    telemetry-only sensor, a repeater heard before it advertised). With no device
    reachable the resolver simply knows nothing, and the stored names stand alone.
    """
    from ..services.trace_runner import make_node_resolver

    contacts = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - optional read; absence just leaves names stored-only
        contacts = []
    return make_node_resolver(contacts)


async def open_timemachine(ctx: "AppContext") -> None:
    """Run the Time Machine: pick a subject, explore its page, repeat until Esc.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi
    from .tui import Choice, Separator

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session
    prefix_bytes = await _routing_prefix_bytes(ctx)
    resolve = await _contact_resolver(ctx)

    while True:
        heard = ctx.repo.heard_nodes()
        if not heard:
            await session.message_dialog(
                Text(
                    "History is empty — the recorder fills it passively while "
                    "MeshTerm listens. Come back after the mesh has talked a while.",
                    style="muted",
                ),
                title="Time machine",
            )
            return
        # Stored names first, the contact resolver filling the blanks (the app-wide
        # rule: a node we *can* name never shows as unknown).
        listed = [
            (node, _known_name(resolve, node.node, node.name))
            for node in heard
            if node.node
        ]
        name_w = min(
            _PICK_NAME_MAX,
            max([len("unknown"), *(len(n) for _node, n in listed if n)]),
        )
        items: list = [
            Choice("🌐 The whole mesh — days, arrivals, the ledger", MESH),
            Separator(" "),
            section_heading("Nodes · most recently heard first"),
            Separator(_picker_header(name_w)),
        ]
        for node, name in listed:
            items.append(
                Choice(
                    _picker_row(node, name, name_w, prefix_bytes),
                    (node.node, name or node.node),
                )
            )
        picked = await session.select(
            "Time machine — pick a subject",
            items,
            prompt="Everything the recorder ever heard, explorable:",
        )
        if picked is None:
            return
        if picked == MESH:
            label = "the whole mesh"
            build = (  # noqa: E731
                lambda window, width, _pb=prefix_bytes: _mesh_sections(
                    ctx, window, width, _pb, resolve
                )
            )
        else:
            node_id, label = picked
            build = (  # noqa: E731 - a tiny binding closure reads better than a def here
                lambda window, width, _id=node_id, _lb=label: _node_sections(
                    ctx, _id, _lb, window, width
                )
            )
        screen = TimeMachineScreen(session=session, label=label, build=build)
        await session.run_screen(screen)
