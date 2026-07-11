"""The live mesh dashboard: everything going on around this node, on one screen.

The interactive face of the ``dashboard`` tool. One full-screen, always-repainting view
stacks four reads of the mesh, coarsest first:

* **Activity** — a tall braille bar chart of *every* packet the hub hears (adverts,
  telemetry, RX-logged packets, messages, acks), one dot column per minute — braille's
  full horizontal resolution, two minutes per character — stretched across whatever
  width the terminal offers, newest at the left with the count scale mirrored on both
  edges. The header indicator's big sibling, drawn from the same
  :meth:`~meshterm.services.monitor_service.MonitorService` buckets. A pulse line
  beneath it reads the rate, who's been heard, and the busiest node of the window.
* **Traffic** — the session's tallies by packet class, each with a proportional bar,
  from the monitor's kind counters.
* **RF health** — the trailing window's reception quality: median SNR (on the trace
  tool's quality bar) and RSSI from stored observations, plus the radio's own live
  numbers — noise floor, last RSSI/SNR, airtime, battery — polled from the device the
  way Device info reads them.
* **Feed** — the latest packets, newest first: time, class, node, SNR/RSSI. Seeded
  from stored history so the screen opens full, then streamed live off the event hub.

The screen holds no subscriptions of its own — the opener (:func:`open_dashboard`)
wires the hub subscription, the device-stats poll, and the once-a-second repaint, and
tears them all down when the screen resolves. PgUp/PgDn/Home/End scroll, Esc backs out.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from statistics import median
from typing import TYPE_CHECKING, Any, Optional

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.events import EventKind, MeshEvent
from ..core.models import NODE_TYPE_REPEATER, Observation, utcnow
from ..persistence.repository import ACTIVITY_WINDOW
from .theme import snr_style
from .trace_screen import snr_bar
from .tui.render import render_lines
from .tui.screen import Screen

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between full repaints while the dashboard is open (ages, rates, spinner-less).
_REFRESH_S = 1.0

#: Seconds between polls of the device's own statistics (noise floor, airtime, battery).
_STATS_POLL_S = 10.0

#: How many feed rows are kept (the body scrolls, so this is history depth, not layout).
_FEED_CAP = 100

#: The feed's fixed node-name lane width; longer names ellipsize so the columns hold.
_FEED_NAME_WIDTH = 18

#: How many braille rows tall the activity chart draws (each row is four dot rows).
_CHART_ROWS = 3

#: Braille dot masks for one cell-column filled bottom-up to height 0–4 (left column:
#: dots 7, 3, 2, 1 top-down; right column: dots 8, 6, 5, 4).
_COL_LEFT = (0x00, 0x40, 0x44, 0x46, 0x47)
_COL_RIGHT = (0x00, 0x80, 0xA0, 0xB0, 0xB8)

#: Display style per packet class, shared by the traffic panel and the feed.
_KIND_STYLES = {
    "advert": "accent",
    "telemetry": "brand",
    "packet": "muted",
    "message": "ok",
    "ack": "faint",
}

#: The order the traffic panel lists packet classes in (heard ones not listed sort last).
_KIND_ORDER = ("advert", "telemetry", "packet", "message", "ack")


def _fit(text: str, width: int) -> str:
    """Left-justify ``text`` to ``width`` columns, ellipsizing anything longer."""
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


def _span_label(minutes: int) -> str:
    """A compact duration — ``45 min`` under two hours, else ``2.5 h`` / ``3 h``."""
    if minutes < 120:
        return f"{minutes} min"
    text = f"{minutes / 60:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + " h"


def _axis_labels(peak: int, rows: int) -> list[str]:
    """Each chart row's top-edge count, top row first, dupes and zeros blanked.

    The top row always reads the peak; lower rows read the proportional counts at
    their upper edges, but a mark that would repeat the one above (a low peak makes
    neighbouring rows round to the same value) or read zero is left blank, so the
    scale never shows the same number twice.
    """
    labels: list[str] = []
    seen: set[int] = set()
    for i in range(rows):
        value = round(peak * (rows - i) / rows)
        if peak and value and value not in seen:
            labels.append(str(value))
            seen.add(value)
        else:
            labels.append("")
    return labels


def braille_bars(values: list[int], *, rows: int = _CHART_ROWS) -> list[Text]:
    """Render ``values`` as a braille bar chart, one *dot column* per value.

    Braille offers two dot columns per character cell — the font's full horizontal
    resolution — so consecutive values pair up into one cell, each rising to its own
    height. Bars are scaled so the window's peak fills all ``rows × 4`` dot rows, and
    any non-zero value lights at least one dot, so a lone packet never vanishes. A
    silent dot column keeps a faint one-dot floor on the bottom row — the flatline
    convention of the app's sparklines — so the chart floor is always visible.

    Args:
        values: The bucket counts, drawn left to right (two per character cell; an
            odd count is padded with one silent column).
        rows: How many braille rows tall the chart is.

    Returns:
        ``rows`` :class:`Text` lines, top row first, ``ceil(len(values) / 2)``
        characters wide.
    """
    peak = max(values, default=0)
    total_dots = rows * 4
    heights = [
        (0 if peak == 0 or v <= 0 else max(1, round(v / peak * total_dots)))
        for v in values
    ]
    if len(heights) % 2:
        heights.append(0)
    lines: list[Text] = []
    for row in range(rows):
        floor = (rows - 1 - row) * 4  # dot rows below this braille row
        line = Text()
        for left, right in zip(heights[0::2], heights[1::2]):
            lf = min(4, max(0, left - floor))
            rf = min(4, max(0, right - floor))
            if lf or rf:
                mask = _COL_LEFT[lf] | _COL_RIGHT[rf]
                if row == rows - 1:
                    # Keep the floor continuous under a half-silent cell: the silent
                    # dot column still shows its bottom baseline dot.
                    mask |= (0x40 if lf == 0 else 0) | (0x80 if rf == 0 else 0)
                line.append(chr(0x2800 | mask), style="ok")
            elif row == rows - 1:
                line.append(chr(0x2800 | 0x40 | 0x80), style="faint")  # the flatline
            else:
                line.append(chr(0x2800))  # blank braille keeps the grid monospace
        lines.append(line)
    return lines


class DashboardScreen(Screen):
    """The live mesh overview. Renders state and scrolls; the opener feeds it."""

    floating = False
    footer_hint = "PgUp/PgDn scroll · Esc back"

    def __init__(
        self,
        *,
        session: Any,
        resolve: Any,
        window: list[Observation],
        activity: Any,
        kind_counts: Any,
        hub_active: Any,
    ) -> None:
        """Create the dashboard over its data feeds.

        Args:
            session: The running TUI session (for repaints).
            resolve: Maps a node hash to a friendly contact name when known.
            window: The stored observations seeding the trailing window (oldest first).
            activity: Zero-arg callable returning the monitor's all-packet histogram.
            kind_counts: Zero-arg callable returning the monitor's session kind tallies.
            hub_active: Zero-arg callable: whether the event hub is pumping.
        """
        super().__init__()
        self.title = "dashboard · mesh overview"
        self._session = session
        self._resolve = resolve
        self._activity = activity
        self._kind_counts = kind_counts
        self._hub_active = hub_active
        #: The trailing window of observations (stored seed + live), oldest first.
        self._window: deque[Observation] = deque(window, maxlen=4000)
        #: The feed: latest events of every class, newest first.
        self._feed: deque[Text] = deque(maxlen=_FEED_CAP)
        for obs in list(self._window)[-_FEED_CAP:][::-1]:
            self._feed.append(self._observation_row(obs))
        #: The device's own numbers, refreshed by the opener's poll (Device info's
        #: dynamic rows): ``stats`` from get_stats, ``battery`` from get_battery.
        self.stats: dict = {}
        self.battery: dict = {}

    # --- live feed -----------------------------------------------------------------

    def on_event(self, event: MeshEvent) -> None:
        """Fold one hub event into the window and the feed, and repaint."""
        obs = event.observation
        if obs is not None:
            self._window.append(obs)
            self._feed.appendleft(self._observation_row(obs))
        elif event.kind == EventKind.MESSAGE and event.message is not None:
            msg = event.message
            where = f"ch {msg.channel}" if msg.is_channel else "direct"
            row = self._feed_row(
                utcnow(), "message",
                self._name(msg.sender) if msg.sender else where,
                note=where if not msg.is_channel else None, snr=msg.snr,
            )
            self._feed.appendleft(row)
        elif event.kind == EventKind.ACK and event.ack is not None:
            self._feed.appendleft(
                self._feed_row(utcnow(), "ack", event.ack.code or "delivery confirmed")
            )
        self._prune()
        self._session.invalidate()

    def _prune(self) -> None:
        """Drop window observations that aged past the trailing window."""
        cutoff = utcnow() - ACTIVITY_WINDOW
        while self._window and self._window[0].observed_at < cutoff:
            self._window.popleft()

    # --- input -----------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Scroll the body or dismiss the screen."""
        if action in ("up", "pageup"):
            self.scroll_pages(-1) if action == "pageup" else self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self.scroll_to_bottom()
        elif action == "escape":
            self.resolve(None)

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the four stacked sections for the current state."""
        self._prune()
        sections: list[RenderableType] = [
            *self._activity_section(width),
            Text(),
            *self._traffic_section(),
            Text(),
            *self._rf_section(),
            Text(),
            *self._feed_section(),
        ]
        lines = render_lines(Group(*sections), width)
        self._scroll_total = max(1, len(lines))
        return lines

    # -- activity --

    def _activity_section(self, width: int) -> list[RenderableType]:
        """The all-packet chart — newest minute at the left — plus the pulse line.

        One dot column per minute, two per character cell, stretched across every
        cell the terminal offers between the two scale gutters; a wider terminal
        simply shows more history. The scale is mirrored on both edges so the counts
        are readable from either end of a wide chart.
        """
        histogram = list(self._activity())  # newest first, one count per minute
        # Size the label lane from the whole histogram's peak (not just the visible
        # slice) so the gutters never shift as a burst scrolls out of view.
        label_w = max(1, len(str(max(histogram, default=0))))
        chars = max(10, width - 2 * (label_w + 2))
        minutes = chars * 2
        shown = (histogram + [0] * minutes)[:minutes]
        peak = max(shown)

        heading = Text("Activity", style="accent")
        heading.append("  ·  every packet heard · one minute per dot column",
                       style="muted")
        chart_rows = braille_bars(shown)
        marks = _axis_labels(peak, len(chart_rows))
        out: list[RenderableType] = [heading]
        for mark, row in zip(marks, chart_rows):
            line = Text(f"{mark:>{label_w}} " + ("┤" if mark else "│"), style="muted")
            line.append_text(row)
            line.append("├" if mark else "│", style="muted")
            if mark:
                line.append(f" {mark}", style="muted")
            out.append(line)
        axis = Text(" " * label_w + " └" + "─" * chars + "┘", style="muted")
        out.append(axis)
        span = "−" + _span_label(minutes)
        caption = Text(" " * (label_w + 2))
        caption.append("now", style="faint")
        caption.append(" " * max(1, chars - len("now") - len(span)))
        caption.append(span, style="faint")
        out.append(caption)
        out.append(Text())
        out.append(self._pulse_line(shown, width))
        return out

    def _pulse_line(self, shown: list[int], width: int) -> Text:
        """Rates, who's been heard, and the window's busiest transmitter.

        ``shown`` is the chart's visible slice (newest first, one bucket per minute),
        so the quoted rates describe exactly what the chart draws. The word "heard"
        after the node count is a luxury: it is kept only when the line fits ``width``
        without wrapping.
        """
        recent = sum(shown[:15]) / max(1, min(15, len(shown)))
        overall = sum(shown) / max(1, len(shown))
        span = _span_label(len(shown))
        nodes = {o.node for o in self._window if o.node and o.kind != "packet"}
        repeaters = {
            o.node
            for o in self._window
            if o.node and o.node_type == NODE_TYPE_REPEATER
        }

        def compose(heard: bool) -> Text:
            line = Text("pulse    ", style="muted")
            line.append(f"{recent:.1f} pkt/min", style="brand")
            line.append(f" (15 m) · {overall:.1f} ({span})", style="muted")
            line.append("  ·  ", style="muted")
            line.append(str(len(nodes)))
            suffix = f" node{'s' if len(nodes) != 1 else ''}"
            line.append(suffix + (" heard" if heard else ""), style="muted")
            if repeaters:
                line.append(
                    f" ({len(repeaters)} repeater{'s' if len(repeaters) != 1 else ''})",
                    style="muted",
                )
            return line

        line = compose(heard=True)
        if len(line.plain) > width:
            line = compose(heard=False)
        busiest = self._busiest()
        if busiest is not None:
            name, count = busiest
            line.append("\nbusiest  ", style="muted")
            line.append(name, style="brand")
            line.append(
                f"  {count} packet{'s' if count != 1 else ''} in the window",
                style="muted",
            )
        return line

    def _busiest(self) -> Optional[tuple[str, int]]:
        """The window's most-heard attributable node, or ``None`` in silence."""
        counts = Counter(
            o.node for o in self._window if o.node and o.kind != "packet"
        )
        if not counts:
            return None
        node, count = counts.most_common(1)[0]
        return self._name(node), count

    # -- traffic --

    def _traffic_section(self) -> list[RenderableType]:
        """Session tallies by packet class, each with a proportional bar."""
        heading = Text("Traffic", style="accent")
        heading.append("  ·  this session, by packet class", style="muted")
        counts = self._kind_counts()
        if not counts:
            return [heading, Text("nothing heard yet", style="muted")]
        order = {k: i for i, k in enumerate(_KIND_ORDER)}
        peak = max(counts.values())
        label_w = max(len(k) for k in counts)
        count_w = len(str(peak))
        rows: list[RenderableType] = [heading]
        for kind in sorted(counts, key=lambda k: (order.get(k, len(order)), k)):
            count = counts[kind]
            row = Text(f"{kind.ljust(label_w)}  ", style="muted")
            row.append(f"{count:>{count_w}}  ")
            # Bars step in half characters — braille's two dot columns per cell —
            # so the lane resolves 48 levels across its 24 cells.
            halves = max(1, round(count / peak * 48))
            style = _KIND_STYLES.get(kind, "brand")
            row.append("⣿" * (halves // 2), style=style)
            if halves % 2:
                row.append("⡇", style=style)  # a lone left dot column: the half step
            rows.append(row)
        return rows

    # -- rf health --

    def _rf_section(self) -> list[RenderableType]:
        """The window's reception quality plus the radio's own live numbers."""
        heading = Text("RF health", style="accent")
        heading.append("  ·  reception over 2 h · radio live", style="muted")
        rows: list[RenderableType] = [heading]

        snrs = [o.snr for o in self._window if o.snr is not None and o.kind != "packet"]
        rssis = [o.rssi for o in self._window if o.rssi is not None and o.kind != "packet"]
        if snrs:
            med = median(snrs)
            line = Text("snr      ", style="muted")
            line.append(f"{med:+.1f} dB median  ", style=snr_style(med))
            line.append_text(snr_bar(med))
            lo, hi = min(snrs), max(snrs)
            line.append(f"  worst {lo:+.1f} · best {hi:+.1f}", style="muted")
            rows.append(line)
        if rssis:
            line = Text("rssi     ", style="muted")
            line.append(f"{median(rssis):.0f} dBm median")
            line.append(f"  weakest {min(rssis):.0f} · strongest {max(rssis):.0f}",
                        style="muted")
            rows.append(line)
        if not snrs and not rssis:
            rows.append(Text("no receptions in the window yet", style="muted"))

        stats = self.stats
        if stats.get("noise_floor") is not None:
            line = Text("radio    ", style="muted")
            line.append(f"{stats['noise_floor']} dBm noise floor")
            if stats.get("last_rssi") is not None:
                line.append(f" · last RSSI {stats['last_rssi']} dBm", style="muted")
            if stats.get("last_snr") is not None:
                line.append(f" · last SNR {stats['last_snr']:+.1f} dB", style="muted")
            rows.append(line)
        extras = Text()
        if stats.get("tx_air_secs") is not None:
            extras.append("airtime  ", style="muted")
            extras.append(f"TX {stats['tx_air_secs']} s · RX {stats.get('rx_air_secs', 0)} s")
        level = self.battery.get("level")
        if level:
            if extras.plain:
                extras.append("  ·  ", style="muted")
            else:
                extras.append("battery  ", style="muted")
            extras.append(f"{int(level) / 1000:.2f} V")
        if extras.plain:
            rows.append(extras)
        return rows

    # -- feed --

    def _feed_section(self) -> list[RenderableType]:
        """The latest packets, newest first, with a live-light in the heading."""
        heading = Text("Feed", style="accent")
        heading.append("  ·  newest first  ", style="muted")
        if self._hub_active():
            heading.append("● live", style="ok")
        else:
            heading.append("○ waiting for a device", style="muted")
        rows: list[RenderableType] = [heading]
        if not self._feed:
            rows.append(Text("nothing heard yet", style="muted"))
        rows.extend(self._feed)
        return rows

    def _observation_row(self, obs: Observation) -> Text:
        """One observation as a feed row (packets show their relay path)."""
        note = None
        if obs.kind == "packet" and obs.path is not None:
            hops = [h for h in obs.path.split(",") if h]
            note = "via " + " → ".join(self._name(h) for h in hops) if hops else "direct"
        label = self._name(obs.node) if obs.node else (obs.name or "?")
        return self._feed_row(
            obs.observed_at, obs.kind, label, note=note, snr=obs.snr, rssi=obs.rssi
        )

    def _feed_row(
        self,
        when: Any,
        kind: str,
        who: str,
        *,
        note: Optional[str] = None,
        snr: Optional[float] = None,
        rssi: Optional[float] = None,
    ) -> Text:
        """Lay one feed row out in fixed lanes: time, class, node, reception, detail.

        Never wraps — a long relay path ellipsizes at the right edge instead of
        spilling a lone ``dBm`` onto its own line — and the node lane is fixed-width
        so the reception columns align down the feed.
        """
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append(when.astimezone().strftime("%H:%M:%S") + "  ", style="muted")
        row.append(kind.ljust(10), style=_KIND_STYLES.get(kind, "brand"))
        row.append(_fit(who, _FEED_NAME_WIDTH))
        row.append("  ")
        row.append(
            f"{snr:+5.1f} dB" if snr is not None else " " * 8,
            style=snr_style(snr) if snr is not None else "muted",
        )
        row.append(
            f"  {rssi:5.0f} dBm" if rssi is not None else " " * 10, style="muted"
        )
        if note:
            row.append(f"  {note}", style="muted")
        return row

    def _name(self, node: Optional[str]) -> str:
        """A node's friendly name when known, else its raw hash (never ``None``)."""
        if not node:
            return "?"
        named = self._resolve(node)
        return named if named else node


async def open_dashboard(ctx: "AppContext") -> None:
    """Open the live dashboard and run it until dismissed.

    Wires the screen to its feeds: the stored trailing window seeds it, a hub
    subscription streams every new packet in, the device's own statistics are polled
    on a slow cadence (skipped quietly while nothing is connected), and a once-a-second
    ticker keeps rates and ages honest. Everything is torn down when the screen closes.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services import trace_runner
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the dashboard is only available in the menu")
    session = ctx.ui.session

    contacts = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            device = await ctx.device()
            contacts = await device.get_contacts()
    except Exception:  # noqa: BLE001 - the dashboard renders fine without contact names
        contacts = []
    resolve = trace_runner.make_node_resolver(contacts)

    window = ctx.repo.recent_observations(since=utcnow() - ACTIVITY_WINDOW)
    screen = DashboardScreen(
        session=session,
        resolve=resolve,
        window=window,
        activity=ctx.monitor.activity_histogram,
        kind_counts=ctx.monitor.kind_counts,
        hub_active=lambda: ctx.events.active,
    )

    unsubscribe = ctx.events.subscribe(screen.on_event)

    async def poll_stats() -> None:
        """Refresh the radio's own numbers on a slow cadence, while connected."""
        while True:
            if ctx.is_connected:
                try:
                    device = await ctx.device()
                    screen.stats = dict(await device.get_stats() or {})
                    screen.battery = dict(await device.get_battery() or {})
                except Exception:  # noqa: BLE001 - optional reads; keep the last good ones
                    pass
                session.invalidate()
            await asyncio.sleep(_STATS_POLL_S)

    async def tick() -> None:
        """Repaint once a second so rates, ages, and the chart's clock stay honest."""
        while True:
            await asyncio.sleep(_REFRESH_S)
            session.invalidate()

    poller = asyncio.ensure_future(poll_stats())
    ticker = asyncio.ensure_future(tick())
    try:
        await session.run_screen(screen)
    finally:
        unsubscribe()
        for task in (poller, ticker):
            task.cancel()
        for task in (poller, ticker):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - teardown must never surface a poll hiccup
                pass
