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

from datetime import datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Callable, Optional

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.models import utcnow
from .braillechart import axis_caption, axis_chart, chart_span, timeline_rows
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

#: Character cells the charts keep clear for their side gutters.
_GUTTER = 4

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


def _chart_block(
    rows: list[Text], label_at: Callable[[float], str], chars: int
) -> list[RenderableType]:
    """Indent chart rows into the gutter and add the oldest→now caption line.

    The caption is the shared :func:`~meshterm.ui.braillechart.axis_caption`, so a
    wide chart gains intermediate time marks between its two edge labels for free.
    """
    out: list[RenderableType] = []
    for row in rows:
        line = Text("  ")
        line.append_text(row)
        out.append(line)
    caption = Text("  ")
    caption.append_text(axis_caption(chars, label_at))
    out.append(caption)
    return out


def _time_axis(start: datetime, end: datetime) -> Callable[[float], str]:
    """An axis labeller over a real time span: timestamps left of the closing ``now``."""
    def label_at(frac: float) -> str:
        if frac >= 1.0:
            return "now"
        return _when_label(start + (end - start) * frac)
    return label_at


def _hour_axis(frac: float) -> str:
    """The rhythm charts' labeller: the local hour of day at ``frac`` of the sweep."""
    return f"{round(frac * 23)} h"


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
    chars = max(20, width - 2 * _GUTTER)
    buckets = chars * 2

    out: list[RenderableType] = []
    stamps = [o.observed_at for o in observations]
    out.append(
        _heading("Volume", f"{len(observations)} receptions · now at the right")
    )
    out.extend(
        _chart_block(
            timeline_rows(bucketize(stamps, start, now, buckets), rows=_CHART_ROWS),
            _time_axis(start, now), chars,
        )
    )

    snr_pairs = [
        (o.observed_at, float(o.snr)) for o in observations if o.snr is not None
    ]
    if snr_pairs:
        medians = bucket_medians(snr_pairs, start, now, buckets)
        lo, hi = chart_span(medians)
        rows = timeline_rows(medians, rows=_SNR_ROWS, style=_snr_cell_style)
        out.append(Text())
        out.append(
            _heading(
                "SNR",
                f"median per slice · grey line = 0 · scale {lo:+.1f} → {hi:+.1f} dB",
            )
        )
        out.extend(_chart_block(rows, _time_axis(start, now), chars))

    hours = [0] * 24
    for stamp in stamps:
        hours[stamp.astimezone().hour] += 1
    out.append(Text())
    out.append(_heading("Rhythm", "receptions by local hour of day"))
    out.extend(_chart_block(timeline_rows(hours, rows=_CHART_ROWS), _hour_axis, 12))

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


def _day_axis(shown: list) -> Callable[[float], str]:
    """An axis labeller over the day charts: compact dates, ``today`` at the right.

    Dates render in the caption style the rest of the app speaks (``Jul 05``, not
    raw ISO), and the right edge reads ``today`` when the newest charted day is
    today — mirroring the node page's closing ``now``.
    """
    today = utcnow().strftime("%Y-%m-%d")

    def label_at(frac: float) -> str:
        iso = shown[min(len(shown) - 1, round(frac * (len(shown) - 1)))][0]
        if frac >= 1.0 and iso == today:
            return "today"
        try:
            return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %d")
        except ValueError:
            return iso
    return label_at


def _day_columns(values: list[int], chars: int) -> list[int]:
    """Stretch per-day counts into day-wide bars that fill the chart's width.

    One dot column per day leaves a short history as a sliver in a wide terminal —
    beneath how every other MeshTerm chart spends its width. Each day repeats over
    ``2 × chars // len(values)`` columns instead (at least one), so few days read
    as wide bars and a deep history falls back to the one-column-per-day density.
    """
    per_day = max(1, (chars * 2) // max(1, len(values)))
    return [value for value in values for _ in range(per_day)]


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
    days = ctx.repo.daily_activity()
    if since is not None:
        floor = since.strftime("%Y-%m-%d")
        days = [d for d in days if d[0] >= floor]
    if not days:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]

    # The y-axis gutter is sized from the whole window's peaks (not just the
    # visible slice), shared by both day charts so their gutters line up and
    # neither chart's date range shifts relative to the other's.
    label_w = max(len(str(max(d[1] for d in days))), len(str(max(d[2] for d in days))))
    chars = max(20, width - 2 * (label_w + 2))
    shown = days[-chars * 2 :]
    out: list[RenderableType] = []
    packets = [d[1] for d in shown]
    out.append(_heading("Packets per day", "UTC days · newest at the right"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(packets, chars), rows=_CHART_ROWS),
            max(packets), chars, _day_axis(shown), label_w=label_w,
        )
    )

    nodes = [d[2] for d in shown]
    out.append(Text())
    out.append(_heading("Nodes per day", "distinct nodes heard"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(nodes, chars), rows=_CHART_ROWS),
            max(nodes), chars, _day_axis(shown), label_w=label_w,
        )
    )

    # The node page's rhythm chart, mesh-wide: when does this *mesh* talk? Hours are
    # grouped by UTC in SQL and rotated here by the current local offset (see
    # Repository.hourly_activity for why one rotation is honest enough).
    offset = round(
        (datetime.now().astimezone().utcoffset() or timedelta()).total_seconds() / 3600
    )
    utc_hours = ctx.repo.hourly_activity(since=since)
    hours = [utc_hours[(h - offset) % 24] for h in range(24)]
    out.append(Text())
    out.append(_heading("Rhythm", "packets by local hour of day"))
    out.extend(axis_chart(timeline_rows(hours, rows=_CHART_ROWS), max(hours), 12, _hour_axis))

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
