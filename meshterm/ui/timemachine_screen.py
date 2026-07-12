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
from .braillechart import GAP, axis_chart, chart_span, timeline_rows
from .theme import snr_style
from .tui.render import render_lines
from .tui.screen import Screen
from .widgets import (
    _DEFAULT_GLYPH,
    _NODE_GLYPHS,
    _age_seconds,
    _format_age,
    _recency_style,
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
        self.title = f"⏳ {self._label} · {name}"

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
    line.append(f"  ({_format_age(_age_seconds(last))} ago)", style="muted")
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


def _day_centers(days: int, chars: int) -> list[int]:
    """The chart cell each day's bar is centred on, mirroring :func:`_day_columns`.

    Replays the same even split ``_day_columns`` uses to spread ``days`` bars across
    ``2 × chars`` dot columns, so a tick placed at ``centers[i]`` lands under day
    ``i``'s bar rather than at an arbitrary fraction of the axis.

    Args:
        days: How many day bars the chart draws.
        chars: The chart's width in character cells.

    Returns:
        One centre cell (``0 .. chars - 1``) per day, oldest first.
    """
    n = max(1, days)
    centers: list[int] = []
    dot = 0  # running dot-column offset, two per character cell
    base, extra = divmod(chars, n) if n <= chars else divmod(chars * 2, n)
    for i in range(n):
        span = (base + (1 if i < extra else 0)) * (2 if n <= chars else 1)
        centers.append(min(chars - 1, (dot + span // 2) // 2))
        dot += span
    return centers


def _day_ticks(shown: list, chars: int) -> list[tuple[int, str]]:
    """``(cell, label)`` axis ticks under the day bars: short dates, thinned to fit.

    One tick per day where they all fit, else an evenly spaced subset keeping both
    ends; each label sits under its own bar (see :func:`_day_centers`). Dates are
    kept compact — the month is shown only on the first tick and whenever it rolls
    over, so most ticks read as a bare day number — and the newest bar reads
    ``today`` when it is, mirroring the node page's closing ``now``.

    Args:
        shown: The charted days, oldest first, each ``(iso_date, ...)``.
        chars: The chart's width in character cells.

    Returns:
        The ticks to pass to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    n = len(shown)
    centers = _day_centers(n, chars)
    today = utcnow().strftime("%Y-%m-%d")
    # A dated label is at most "Jul 12" (6 cells); keep a couple of cells between them.
    fit = max(2, chars // 8)
    if n <= fit:
        picks = list(range(n))
    else:
        picks = sorted({round(i * (n - 1) / (fit - 1)) for i in range(fit)})
    ticks: list[tuple[int, str]] = []
    prev_month: Optional[str] = None
    for i in picks:
        iso = shown[i][0]
        try:
            day = datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            ticks.append((centers[i], iso))
            continue
        month = day.strftime("%b")
        if i == n - 1 and iso == today:
            label = "today"
        elif month != prev_month:
            label = f"{month} {day.day}"
        else:
            label = str(day.day)
        prev_month = month
        ticks.append((centers[i], label))
    return ticks


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
    first = date.fromisoformat(active[0][0])
    floor = since.date() if since is not None else first
    start = max(first, floor)
    end = max(start, now.date())
    out: list[tuple[str, int, int]] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        packets, nodes = by_iso.get(iso, (0, 0))
        out.append((iso, packets, nodes))
        day += timedelta(days=1)
    return out


def _day_columns(values: list[int], chars: int) -> list:
    """Stretch per-day counts into day-wide bars that fill the chart's width exactly.

    One dot column per day leaves a short history as a sliver in a wide terminal —
    beneath how every other MeshTerm chart spends its width — so each day repeats
    over a share of the chart's columns instead. The share is an even split with
    the one-off remainder spread across the oldest days, so the total always lands
    on exactly ``2 × chars`` dot columns: a plain floor division drops the
    remainder instead, leaving the bars short of the axis border and caption
    sized for the full width (misreading as the whole chart sitting shifted left).

    A day wide enough to span more than one character opens with a
    :data:`~meshterm.ui.braillechart.GAP` dot column — the left half of its leading
    braille cell, blank clean down to the axis — so same-height neighbours read as
    separate bars instead of fusing into one solid block. A history deeper than the
    chart is wide falls back to one dot column per day (no room for a full character
    each, so no notch), the dot-column total still landing exactly on ``2 × chars``.

    Args:
        values: Per-day counts, oldest first.
        chars: The chart's width in character cells.

    Returns:
        Exactly ``2 × chars`` dot-column readings, oldest first (a
        :data:`~meshterm.ui.braillechart.GAP` marks a day-boundary notch).
    """
    n = max(1, len(values))
    out: list = []
    if n <= chars:
        # Each day spans at least one whole character: split in character units so
        # every day's block starts on an even dot-column index (a fresh cell),
        # which is what makes "blank the leading cell's left half" well-defined.
        base, extra = divmod(chars, n)
        for i, value in enumerate(values):
            width_chars = base + (1 if i < extra else 0)
            if width_chars > 1:
                out.append(GAP)
                out.extend([value] * (width_chars * 2 - 1))
            else:
                out.extend([value] * (width_chars * 2))
    else:
        # More days than characters: no day gets a whole one, so no notch is drawn
        # (nothing to space apart) — just split the dot columns themselves.
        base, extra = divmod(chars * 2, n)
        for i, value in enumerate(values):
            out.extend([value] * (base + (1 if i < extra else 0)))
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
    days = _fill_days(ctx.repo.daily_activity(), since, now)
    if not days:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]

    # The mesh-wide rhythm (charted below) folds the whole window into 96 fifteen-minute
    # slices, so a busy slice's tally can top any single day's — compute it up front so its
    # peak joins the day peaks in sizing one shared y-axis gutter.
    offset_slots = round(
        (datetime.now().astimezone().utcoffset() or timedelta()).total_seconds() / 900
    )
    utc_slots = ctx.repo.quarter_hour_activity(since=since)
    slots = [utc_slots[(s - offset_slots) % 96] for s in range(96)]

    # The y-axis gutter is sized from the whole window's peaks (not just the visible
    # slice) and shared by every chart, so all their gutters — and thus their left edges —
    # line up. The rhythm keeps its own finer width; only the gutter is common.
    label_w = max(
        len(str(max(d[1] for d in days))),
        len(str(max(d[2] for d in days))),
        len(str(max(slots))),
    )
    chars = max(20, width - 2 * (label_w + 2))
    shown = days[-chars * 2 :]
    out: list[RenderableType] = []
    packets = [d[1] for d in shown]
    day_ticks = _day_ticks(shown, chars)
    out.append(_heading("Packets per day", "UTC days"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(packets, chars), rows=_CHART_ROWS),
            max(packets), chars, label_w=label_w, ticks=day_ticks,
        )
    )

    nodes = [d[2] for d in shown]
    out.append(Text())
    out.append(_heading("Nodes per day", "distinct nodes heard"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(nodes, chars), rows=_CHART_ROWS),
            max(nodes), chars, label_w=label_w, ticks=day_ticks,
        )
    )

    # The node page's rhythm chart, mesh-wide and four times finer: when does this *mesh*
    # talk? The 96 fifteen-minute slices (grouped by UTC in SQL, rotated into local time
    # above) keep their own 48-cell width but share the day charts' gutter, so this chart's
    # left edge lines up with the two above it.
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
            line.append(_fit(name or "unknown", name_w), style=_recency_style(secs))
            line.append("  ")
            line.append_text(highlighted_hash(node, prefix_bytes, width=_PICK_HASH_W))
            line.append("  ")
            line.append(_when_label(first))
            line.append(f"  ({_format_age(secs)} ago)", style="muted")
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


def _fit(text: str, width: int) -> str:
    """Left-justify ``text`` to ``width`` columns, ellipsizing anything that overflows."""
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


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
    row.append(_fit(name or "unknown", name_w), style=_recency_style(secs))
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
        device = await ctx.device()
        mode = await device.get_path_hash_mode()
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
            device = await ctx.device()
            contacts = await device.get_contacts()
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
                title="⏳ Time machine",
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
            Separator(""),
            Separator("Nodes, most recently heard first", style="accent"),
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
            "⏳ Time machine — pick a subject",
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
