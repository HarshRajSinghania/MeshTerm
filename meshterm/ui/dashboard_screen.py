"""The live mesh dashboard: everything going on around this node, on one screen.

The interactive face of the ``dashboard`` tool. One full-screen, always-repainting view
stacks four reads of the mesh, coarsest first:

* **Activity** — a tall braille bar chart of *every* packet the hub hears (adverts,
  telemetry, RX-logged packets, messages, acks), one dot column per minute — braille's
  full horizontal resolution, two minutes per character — stretched across whatever
  width the terminal offers, newest at the right (the app-wide timeline direction)
  with the count scale mirrored on both edges. The header indicator's big sibling,
  drawn from the same :meth:`~meshterm.services.monitor_service.MonitorService`
  buckets. A pulse line beneath it reads the rate, who's been heard, and the busiest
  node of the window.
* **Traffic** — the session's tallies by packet class, each with a proportional bar,
  from the monitor's kind counters.
* **RF health** — the trailing window's reception quality: median SNR (on the trace
  tool's quality bar) and RSSI from stored observations, plus the radio's own live
  numbers — noise floor, last RSSI/SNR, airtime, battery — polled from the device the
  way Device info reads them.
* **Feed** — the latest packets, newest first: time, class (icon + label, icon alone
  on a narrow terminal), node, SNR/RSSI. Seeded from stored history so the screen
  opens full, then streamed live off the event hub. ``↑``/``↓`` walk the feed rows
  and Enter opens the highlighted packet in the shared
  :class:`~meshterm.ui.packet_viewer.PacketViewer` — which then pages through the
  feed itself with the same ``↑``/``↓``, and, for an overheard channel-text packet
  naming a channel we hold the key for, decrypts it.

The screen holds no subscriptions of its own — the opener (:func:`open_dashboard`)
wires the hub subscription, the device-stats poll, and the once-a-second repaint, and
tears them all down when the screen resolves. PgUp/PgDn/Home/End scroll; Esc first
drops the feed highlight, then backs out.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter, deque
from statistics import median
from typing import TYPE_CHECKING, Any, Optional, Sequence

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.events import EventKind, MeshEvent
from ..core.models import NODE_TYPE_REPEATER, Observation, utcnow
from ..persistence.repository import ACTIVITY_WINDOW
from .braillechart import axis_chart, meter, timeline_rows
from .menus import fit_cells
from .widgets import path_text
from .packet_viewer import (
    KIND_STYLES,
    PacketEntry,
    PacketViewer,
    kind_icon,
    node_label,
    payload_class,
)
from .theme import name_style, snr_style
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

#: The order the traffic panel lists packet classes in (heard ones not listed sort last).
_KIND_ORDER = ("advert", "telemetry", "packet", "message", "ack")

#: How many character cells a traffic lane's meter spans (48 half-step levels).
_TRAFFIC_METER_CELLS = 24

#: Terminal width below which the feed drops the textual kind label and keeps only
#: the two-cell icon, buying the name and reception lanes room (≤72-col care).
_FEED_LABEL_MIN_WIDTH = 76

#: The label column every hanging-indent section grid reserves (the ``snr      `` /
#: ``radio    `` lane), so wrapped values align with their own block, never column 0.
_GRID_LABEL_W = 9


#: A channel message carries no sender field on the wire, so senders self-identify by
#: prefixing ``Name: `` (mirrors the chat transcript's own parse). Lifting the name out
#: lets the feed's node lane show *who* sent it rather than the bare channel it came in on.
_SENDER_PREFIX = re.compile(r"^([^\s:][^:]{0,19}):[ \t]+\S")


def _channel_sender(text: Optional[str]) -> Optional[str]:
    """The sender named by a channel message's ``Name: `` prefix, or ``None`` if absent."""
    match = _SENDER_PREFIX.match(text or "")
    if match is None:
        return None
    name = match.group(1).strip()
    return name if name and not name.isdigit() else None


def _span_label(minutes: int) -> str:
    """A compact duration — ``45 min`` under two hours, else ``2.5 h`` / ``3 h``."""
    if minutes < 120:
        return f"{minutes} min"
    text = f"{minutes / 60:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + " h"


class DashboardScreen(Screen):
    """The live mesh overview. Renders state and scrolls; the opener feeds it."""

    floating = False
    footer_hint = "↑↓ packets · Enter open · PgUp/PgDn/Home/End scroll · Esc back"

    def __init__(
        self,
        *,
        session: Any,
        resolve: Any,
        window: list[Observation],
        activity: Any,
        activity_flags: Any,
        kind_counts: Any,
        hub_active: Any,
        prefix_bytes: int = 0,
        self_name: Optional[str] = None,
        channels: Sequence[tuple[str, bytes]] = (),
    ) -> None:
        """Create the dashboard over its data feeds.

        Args:
            session: The running TUI session (for repaints and the packet viewer).
            resolve: Maps a node hash to a friendly contact name when known.
            window: The stored observations seeding the trailing window (oldest first).
            activity: Zero-arg callable returning the monitor's all-packet histogram.
            activity_flags: Zero-arg callable returning the histogram's per-bucket
                this-session flags (seeded history draws grey, live traffic green).
            kind_counts: Zero-arg callable returning the monitor's kind tallies.
            hub_active: Zero-arg callable: whether the event hub is pumping.
            prefix_bytes: Path-hash width to light in the packet viewer's hashes.
            self_name: Our own node's name, drawn white wherever it appears.
            channels: The device's configured channels, as ``(name, secret)`` pairs,
                handed to each opened :class:`~meshterm.ui.packet_viewer.PacketViewer`
                so it can attempt to decrypt an overheard channel-text packet.
        """
        super().__init__()
        self.title = "Dashboard — mesh overview"
        self._session = session
        self._resolve = resolve
        self._activity = activity
        self._activity_flags = activity_flags
        self._kind_counts = kind_counts
        self._hub_active = hub_active
        self._prefix_bytes = prefix_bytes
        self._self_name = self_name
        self._channels = channels
        #: The trailing window of observations (stored seed + live), oldest first.
        self._window: deque[Observation] = deque(window, maxlen=4000)
        #: The feed: latest events of every class as data, newest first — rendered
        #: fresh each paint (rows adapt to width) and handed whole to the viewer.
        self._feed: deque[PacketEntry] = deque(maxlen=_FEED_CAP)
        for obs in list(self._window)[-_FEED_CAP:][::-1]:
            self._feed.append(PacketEntry.from_observation(obs))
        #: The highlighted feed row (``None`` = nothing selected, view scrolls free).
        self._selected: Optional[int] = None
        #: Body lines the last render produced before the feed rows began.
        self._feed_first_line = 0
        #: The device's own numbers, refreshed by the opener's poll (Device info's
        #: dynamic rows): ``stats`` from get_stats, ``battery`` from get_battery.
        self.stats: dict = {}
        self.battery: dict = {}

    # --- live feed -----------------------------------------------------------------

    def on_event(self, event: MeshEvent) -> None:
        """Fold one hub event into the window and the feed, and repaint."""
        entry: Optional[PacketEntry] = None
        obs = event.observation
        if obs is not None:
            self._window.append(obs)
            entry = PacketEntry.from_observation(obs)
        elif event.kind == EventKind.MESSAGE and event.message is not None:
            msg = event.message
            entry = PacketEntry(
                when=utcnow(), kind="message", node=msg.sender, snr=msg.snr,
                where=f"ch {msg.channel}" if msg.is_channel else "direct",
                text=msg.text, raw=msg.raw,
            )
        elif event.kind == EventKind.ACK and event.ack is not None:
            entry = PacketEntry(
                when=utcnow(), kind="ack",
                where=event.ack.code or "delivery confirmed",
            )
        if entry is not None:
            self._feed.appendleft(entry)
            # Keep the highlight on the same packet as new rows push it down.
            if self._selected is not None:
                self._selected = min(self._selected + 1, len(self._feed) - 1)
        self._prune()
        self._session.invalidate()

    def _prune(self) -> None:
        """Drop window observations that aged past the trailing window."""
        cutoff = utcnow() - ACTIVITY_WINDOW
        while self._window and self._window[0].observed_at < cutoff:
            self._window.popleft()

    # --- input -----------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Walk the feed, open the highlighted packet, scroll, or dismiss."""
        if action == "up":
            self._move_selection(-1)
        elif action == "down":
            self._move_selection(1)
        elif action == "enter":
            self._open_packet()
        elif action == "pageup":
            # With a feed row highlighted the page keys walk the selection (a screenful
            # at a time), so the highlight travels with the view instead of the view
            # scrolling out from under a pinned selection; with none, they free-scroll.
            if self._selected is not None:
                self._select_index(self._selected - self._page_step)
            else:
                self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            if self._selected is not None:
                self._select_index(self._selected + self._page_step)
            else:
                self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            # With the feed highlight active, Home jumps to the newest packet;
            # otherwise it keeps its plain scroll-to-top meaning (End mirrors it).
            if self._selected is not None:
                self._select_index(0)
            else:
                self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            if self._selected is not None:
                self._select_index(len(self._feed) - 1)
            else:
                self.scroll_to_bottom()
        elif action == "escape":
            if self._selected is not None:
                self._selected = None  # first Esc peels the highlight, second leaves
                self._session.invalidate()
            else:
                self.resolve(None)

    def _move_selection(self, delta: int) -> None:
        """Move the feed highlight (the first press lands on the newest packet)."""
        if not self._feed:
            return
        if self._selected is None:
            self._select_index(0)
        else:
            self._select_index(self._selected + delta)

    def _select_index(self, index: int) -> None:
        """Highlight one feed row (clamped) and repaint."""
        self._selected = max(0, min(index, len(self._feed) - 1))
        self._session.invalidate()

    def _open_packet(self) -> None:
        """Float the packet viewer over the highlighted feed row.

        Paging inside the viewer walks the feed highlight in step (via
        ``on_navigate``), so closing it lands back on the packet last viewed.
        """
        if self._selected is None or not self._feed:
            return

        def follow(entry: PacketEntry) -> None:
            # The feed may have grown since the snapshot; find the entry itself.
            for i, candidate in enumerate(self._feed):
                if candidate is entry:
                    self._select_index(i)
                    return

        viewer = PacketViewer(
            list(self._feed), self._selected,
            resolve=self._resolve, prefix_bytes=self._prefix_bytes,
            self_name=self._self_name, on_navigate=follow,
            channels=self._channels,
            # The live feed itself (newest first), so the viewer keeps up with packets
            # that arrive while it is open instead of freezing at this snapshot.
            source=lambda: list(self._feed),
        )
        asyncio.ensure_future(self._session.run_screen(viewer))

    def cursor_line(self) -> Optional[int]:
        """Keep the highlighted feed row in view (free scrolling when nothing is)."""
        if self._selected is None or not self._feed:
            return None
        return self._feed_first_line + self._selected

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the four stacked sections for the current state."""
        self._prune()
        if self._selected is not None and self._feed:
            self._selected = min(self._selected, len(self._feed) - 1)
        sections: list[RenderableType] = [
            *self._activity_section(width),
            Text(),
            *self._traffic_section(),
            Text(),
            *self._rf_section(),
            Text(),
            *self._feed_section(width),
        ]
        lines = render_lines(Group(*sections), width)
        self._scroll_total = max(1, len(lines))
        # Feed rows are the body's tail, one line each (they never wrap), so the
        # highlight's body line is a plain offset from the end.
        self._feed_first_line = len(lines) - len(self._feed)
        return lines

    # -- activity --

    def _activity_section(self, width: int) -> list[RenderableType]:
        """The all-packet chart — newest minute at the right — plus the pulse line.

        One dot column per minute, two per character cell, stretched across every
        cell the terminal offers between the two scale gutters; a wider terminal
        simply shows more history. Time runs oldest→now left to right (every
        MeshTerm timeline's direction), and the scale is mirrored on both edges so
        the counts are readable from either end of a wide chart.
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
        # Buckets seeded from a previous session's stored history draw grey; only
        # what this session heard itself pulses green.
        flags = (tuple(self._activity_flags()) + (True,) * minutes)[:minutes]
        styles = ["ok" if live else "muted" for live in reversed(flags)]
        chart_rows = timeline_rows(
            list(reversed(shown)), rows=_CHART_ROWS, column_styles=styles
        )

        def caption_at(frac: float) -> str:
            if frac >= 1.0:
                return "now"
            return "−" + _span_label(round(minutes * (1 - frac)))

        out: list[RenderableType] = [
            heading,
            *axis_chart(chart_rows, peak, chars, caption_at, label_w=label_w),
        ]
        out.append(Text())
        out.append(self._pulse_grid(shown, width))
        return out

    def _grid(self, rows: list[tuple[str, Text]]) -> Table:
        """Labelled rows with a hanging indent: values wrap within their own block.

        The app-wide alignment rule — a wrapped item's continuation lines align with
        the item, never with the line start — done as a two-column frameless grid:
        the label lane is fixed at :data:`_GRID_LABEL_W` cells, the value column
        soaks up the rest and folds inside itself.
        """
        grid = Table(
            box=None, show_header=False, show_edge=False, pad_edge=False,
            padding=(0, 0), expand=False,
        )
        grid.add_column(width=_GRID_LABEL_W, no_wrap=True)
        grid.add_column(overflow="fold")
        for label, value in rows:
            grid.add_row(Text(label, style="muted"), value)
        return grid

    def _pulse_grid(self, shown: list[int], width: int) -> Table:
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
            line = Text()
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
        if len(line.plain) + _GRID_LABEL_W > width:
            line = compose(heard=False)
        rows = [("pulse", line)]
        busiest = self._busiest()
        if busiest is not None:
            name, count = busiest
            value = Text(name, style="brand")
            value.append(
                f"  {count} packet{'s' if count != 1 else ''} in the window",
                style="muted",
            )
            rows.append(("busiest", value))
        return self._grid(rows)

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
        """Tallies by packet class, each with a proportional braille meter."""
        heading = Text("Traffic", style="accent")
        heading.append("  ·  by packet class · stored history + live", style="muted")
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
            # The shared braille meter, full-height with no track: the lane resolves
            # 48 levels across its 24 cells (two half-steps per cell).
            row.append_text(
                meter(
                    count / peak, _TRAFFIC_METER_CELLS,
                    style=KIND_STYLES.get(kind, "brand"),
                )
            )
            rows.append(row)
        return rows

    # -- rf health --

    def _rf_section(self) -> list[RenderableType]:
        """The window's reception quality plus the radio's own live numbers.

        Rendered as a labelled grid so an item that wraps on a narrow terminal
        aligns its continuation with its own block, not the line start.
        """
        heading = Text("RF health", style="accent")
        heading.append("  ·  reception over 2 h · radio live", style="muted")
        rows: list[tuple[str, Text]] = []

        snrs = [o.snr for o in self._window if o.snr is not None and o.kind != "packet"]
        rssis = [o.rssi for o in self._window if o.rssi is not None and o.kind != "packet"]
        if snrs:
            med = median(snrs)
            line = Text(f"{med:+.1f} dB median  ", style=snr_style(med))
            line.append_text(snr_bar(med))
            lo, hi = min(snrs), max(snrs)
            line.append(f"  worst {lo:+.1f} · best {hi:+.1f}", style="muted")
            rows.append(("snr", line))
        if rssis:
            line = Text(f"{median(rssis):.0f} dBm median")
            line.append(f"  weakest {min(rssis):.0f} · strongest {max(rssis):.0f}",
                        style="muted")
            rows.append(("rssi", line))

        stats = self.stats
        if stats.get("noise_floor") is not None:
            line = Text(f"{stats['noise_floor']} dBm noise floor")
            if stats.get("last_rssi") is not None:
                line.append(f" · last RSSI {stats['last_rssi']} dBm", style="muted")
            if stats.get("last_snr") is not None:
                line.append(f" · last SNR {stats['last_snr']:+.1f} dB", style="muted")
            rows.append(("radio", line))
        extras = Text()
        label = "airtime"
        if stats.get("tx_air_secs") is not None:
            extras.append(f"TX {stats['tx_air_secs']} s · RX {stats.get('rx_air_secs', 0)} s")
        level = self.battery.get("level")
        if level:
            if extras.plain:
                extras.append("  ·  ", style="muted")
            else:
                label = "battery"
            extras.append(f"{int(level) / 1000:.2f} V")
        if extras.plain:
            rows.append((label, extras))

        if not rows:
            return [heading, Text("no receptions in the window yet", style="muted")]
        return [heading, self._grid(rows)]

    # -- feed --

    def _feed_section(self, width: int) -> list[RenderableType]:
        """The latest packets, newest first, with a live-light in the heading."""
        heading = Text("Feed", style="accent")
        heading.append("  ·  newest first · Enter opens a packet  ", style="muted")
        if self._hub_active():
            heading.append("● live", style="ok")
        else:
            heading.append("○ waiting for a device", style="muted")
        rows: list[RenderableType] = [heading]
        if not self._feed:
            rows.append(Text("nothing heard yet", style="muted"))
        show_label = width >= _FEED_LABEL_MIN_WIDTH
        for i, entry in enumerate(self._feed):
            rows.append(self._feed_row(entry, i == self._selected, show_label))
        return rows

    def _feed_row(self, entry: PacketEntry, selected: bool, show_label: bool) -> Text:
        """Lay one feed row out in fixed lanes: time, class, node, reception, detail.

        The class lane leads with its two-cell icon; the textual label beside it is
        dropped wholesale on a narrow terminal (``show_label``), keeping the lanes
        aligned either way. The node name takes the app-wide palette hue (our own
        node white, a bare hash muted). Never wraps — a long relay path ellipsizes
        at the right edge instead of spilling a lone ``dBm`` onto its own line.
        """
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append("▸ " if selected else "  ", style="accent")
        row.append(entry.when.astimezone().strftime("%H:%M:%S") + "  ", style="muted")
        row.append(kind_icon(entry.kind) + " ")
        if show_label:
            row.append(entry.kind.ljust(10), style=KIND_STYLES.get(entry.kind, "brand"))
        label, style = node_label(entry, self._resolve, self._self_name)
        if label == "?":
            label, style = self._feed_subject(entry)  # no node identity: name what we can
        row.append(fit_cells(label, _FEED_NAME_WIDTH), style=style)
        row.append("  ")
        row.append(
            f"{entry.snr:+5.1f} dB" if entry.snr is not None else " " * 8,
            style=snr_style(entry.snr) if entry.snr is not None else "muted",
        )
        row.append(
            f"  {entry.rssi:5.0f} dBm" if entry.rssi is not None else " " * 10,
            style="muted",
        )
        note = self._feed_note(entry)
        if note is not None:
            row.append("  ")
            row.append_text(note)
        return row

    def _feed_subject(self, entry: PacketEntry) -> tuple[str, str]:
        """Name the node lane when an entry carries no resolvable node identity.

        The fallback the vast majority of rows hit — a relayed ``packet`` naming no
        origin, or a channel message with no sender field. Rather than a useless ``?``
        (or the bare ``ch 3`` that only repeats the note), it reads the most identifying
        thing the entry does carry: the sender a channel message named itself with, or
        what *kind* of frame a relayed packet is (``channel text`` / ``trace`` / …). An
        ack falls back to its code; nothing else, to a dash.
        """
        if entry.kind == "message":
            sender = _channel_sender(entry.text)
            if sender:
                ours = self._self_name and sender == self._self_name
                return sender, ("you" if ours else name_style(sender))
            return "channel", "muted"
        if entry.kind == "packet":
            cls = payload_class(entry.raw)
            if cls:
                return cls, "muted"
        if entry.where:
            return entry.where, "muted"  # an ack's code, or any other stray context
        return "—", "muted"

    def _feed_note(self, entry: PacketEntry) -> Optional[Text]:
        """The row's trailing detail: a packet's relay path, a message's conversation.

        The path renders through the shared compact path widget, so a relayed frame's
        ``via`` chain reads the same here as in the packet viewer and the chat paths.
        """
        if entry.kind == "packet" and entry.path is not None:
            note = Text("via ", style="muted")
            note.append_text(
                path_text(
                    entry.path.split(","),
                    self._resolve,
                    prefix_bytes=self._prefix_bytes,
                    self_name=self._self_name,
                )
            )
            return note
        if entry.kind == "message" and entry.where:
            return Text(entry.where, style="muted")
        return None

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
    from .channels import read_channel_slots
    from .surface import TuiUi
    from .timemachine_screen import _routing_prefix_bytes

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the dashboard is only available in the menu")
    session = ctx.ui.session

    contacts = []
    self_name: Optional[str] = None
    channels: list[tuple[str, bytes]] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            device = await ctx.device()
            contacts = await device.get_contacts()
            self_name = (await device.get_self_info()).get("name") or None
            channels = [(s.name, s.secret) for s in await read_channel_slots(device)]
    except Exception:  # noqa: BLE001 - the dashboard renders fine without contact names
        contacts = []
    # Contacts first, every name the recorder ever overheard as the fallback — the
    # app-wide rule that a node we can name never renders as a bare hash.
    resolve = trace_runner.make_node_resolver(contacts, ctx.repo.node_names())
    prefix_bytes = await _routing_prefix_bytes(ctx)

    window = ctx.repo.recent_observations(since=utcnow() - ACTIVITY_WINDOW)
    screen = DashboardScreen(
        session=session,
        resolve=resolve,
        window=window,
        activity=ctx.monitor.activity_histogram,
        activity_flags=ctx.monitor.activity_session_flags,
        kind_counts=ctx.monitor.kind_counts,
        hub_active=lambda: ctx.events.active,
        prefix_bytes=prefix_bytes,
        self_name=self_name,
        channels=channels,
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
