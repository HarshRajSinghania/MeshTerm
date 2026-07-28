"""The Live feed: every packet as it arrives, full-screen and newest first.

The interactive face of the ``livefeed`` tool — the dashboard's old feed panel,
promoted to a first-class screen. One always-repainting list streams the latest
packets, newest first: time, class (icon + label, icon alone on a narrow terminal),
node, SNR/RSSI, and the trailing detail (a relayed frame's ``via`` chain, a message's
conversation). Seeded from stored history so the screen opens full, then streamed
live off the event hub. ``↑``/``↓`` walk the rows, PgUp/PgDn/Home/End page and jump
them, ``←``/``→`` scroll the highlighted row sideways when it runs past the right edge
(a long relay path read to its end without opening anything), and Enter opens the
highlighted packet in the shared
:class:`~meshterm.ui.packet_viewer.PacketViewer` — which then pages through the feed
itself with the same ``↑``/``↓``, and, for an overheard channel-text packet naming a
channel we hold the key for, decrypts it.

The screen holds no subscriptions of its own — the opener (:func:`open_livefeed`)
wires the hub subscription and the once-a-second repaint, and tears them down when
the screen resolves. The newest packet is highlighted from the moment the screen
opens; Esc backs out directly.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any, Optional, Sequence

from rich.text import Text

from ..core.channels import split_channel_sender
from ..core.events import EventKind, MeshEvent
from ..core.models import Observation, utcnow
from ..persistence.repository import OBSERVATION_WINDOW
from .menus import fit_cells
from .packet_viewer import (
    KIND_STYLES,
    PacketEntry,
    PacketViewer,
    kind_icon,
    node_label,
    payload_class,
)
from .theme import name_style, snr_style
from .tui.render import crop_cells, render_to_ansi
from .tui.screen import ListWindow, Screen
from .pathline import path_line
from .widgets import NameKeyResolver, TypeOf

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between full repaints while the feed is open (keeps the live-light and any
#: age-sensitive chrome honest even between hub events).
_REFRESH_S = 1.0

#: How many feed rows are kept (the feed windows within the screen, so this is
#: history depth, not layout).
_FEED_CAP = 100

#: The feed's fixed node-name lane width; longer names ellipsize so the columns hold.
_FEED_NAME_WIDTH = 18

#: Terminal width below which the feed drops the textual kind label and keeps only
#: the two-cell icon, buying the name and reception lanes room (≤72-col care).
_FEED_LABEL_MIN_WIDTH = 76

#: Cells one ←/→ press shifts the highlighted row by — the app-wide select list's own
#: step (:attr:`~meshterm.ui.tui.select.SelectScreen._HSCROLL_STEP`), so a row here
#: scrolls at the rate a row anywhere else does.
_HSCROLL_STEP = 8

#: Moves that abandon the highlighted row's horizontal scroll: each row scrolls on its
#: own, exactly as an ``hscroll`` select list's rows do — landing on a new packet always
#: starts it at its own beginning.
_HSHIFT_RESET = frozenset({
    "up", "down", "pageup", "pagedown", "space", "home", "ctrl_home", "end", "ctrl_end",
})


def _channel_sender(text: Optional[str]) -> Optional[str]:
    """The sender named by a channel message's ``Name: `` prefix, or ``None`` if absent.

    The shared protocol-layer parse (see
    :func:`~meshterm.core.channels.split_channel_sender`), so the feed's node lane names
    exactly the sender the chat transcript would.
    """
    name, _body = split_channel_sender(text or "")
    return name


class LiveFeedScreen(Screen):
    """The full-screen packet stream. Renders state; the opener feeds it."""

    floating = False

    def __init__(
        self,
        *,
        session: Any,
        resolve: Any,
        seed: list[Observation],
        hub_active: Any,
        prefix_bytes: int = 0,
        self_name: Optional[str] = None,
        channels: Sequence[tuple[str, bytes]] = (),
        type_of: Optional[TypeOf] = None,
        key_of: Optional[NameKeyResolver] = None,
    ) -> None:
        """Create the feed over its data feeds.

        Args:
            session: The running TUI session (for repaints and the packet viewer).
            resolve: Maps a node hash to a friendly contact name when known.
            seed: Stored observations to open with (oldest first, as the repository
                returns them); the newest :data:`_FEED_CAP` become the opening feed.
            hub_active: Zero-arg callable: whether the event hub is pumping.
            prefix_bytes: The hash width to light in the packet viewer's keys.
            self_name: Our own node's name, drawn white wherever it appears.
            channels: The device's configured channels, as ``(name, secret)`` pairs,
                handed to each opened :class:`~meshterm.ui.packet_viewer.PacketViewer`
                so it can attempt to decrypt an overheard channel-text packet.
            type_of: Maps a relay hash to its node type, handed to the packet viewer so a
                relayed packet's route graph marks a repeater ``▲`` (etc.) over a dot.
            key_of: Maps a sender's display name back to its node's key (see
                :func:`~meshterm.services.trace_runner.make_name_key_resolver`), so a
                channel sender the contacts or the recorder know takes its key-derived
                hue; an unresolvable name stays muted.
        """
        super().__init__()
        self.title = "Live feed"
        self._session = session
        self._resolve = resolve
        self._hub_active = hub_active
        self._prefix_bytes = prefix_bytes
        self._self_name = self_name
        self._channels = channels
        self._type_of = type_of
        self._key_of: NameKeyResolver = key_of or (lambda name: None)
        #: The feed: latest events of every class as data, newest first — rendered
        #: fresh each paint (rows adapt to width) and handed whole to the viewer.
        self._feed: deque[PacketEntry] = deque(maxlen=_FEED_CAP)
        for obs in list(seed)[-_FEED_CAP:][::-1]:
            self._feed.append(PacketEntry.from_observation(obs))
        #: The highlighted feed row. Starts on the newest packet (``None`` only when
        #: the feed is empty) so a lone Esc always backs straight out of the screen.
        self._selected: Optional[int] = 0 if self._feed else None
        #: The feed's window within the fixed screen (its rows scroll under the heading).
        self._feed_window = ListWindow()
        #: The highlighted row's horizontal scroll (cells shifted in, ``←/→``) and how
        #: far it can shift — measured against the width of the last paint, so a resize
        #: can only ever clamp an in-progress scroll back into range.
        self._hshift = 0
        self._hmax = 0

    # --- live feed -----------------------------------------------------------------

    def on_event(self, event: MeshEvent) -> None:
        """Fold one hub event into the feed and repaint."""
        entry: Optional[PacketEntry] = None
        obs = event.observation
        if obs is not None:
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
        self._session.invalidate()

    # --- input -----------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The key hint, gaining ``←→ scroll line`` exactly while it would do something.

        A row that already fits scrolls nowhere, so the atom stays hidden on it — the
        same "advertise a key only where it acts" rule the select list's own ``hscroll``
        hint follows.
        """
        base = "↑↓ PgUp/PgDn Home/End move · Enter open · Esc back"
        if self._selected is not None and self._hmax > 0:
            return "↑↓ PgUp/PgDn Home/End move · ←→ scroll line · Enter open · Esc back"
        return base

    def handle(self, action: str, data: str = "") -> None:
        """Walk the feed, open the highlighted packet, scroll, or dismiss."""
        if action in _HSHIFT_RESET:
            self._hshift = 0  # moving off a row abandons its scroll
        if action == "up":
            self._move_selection(-1)
        elif action == "down":
            self._move_selection(1)
        elif action == "enter":
            self._open_packet()
        elif action == "pageup":
            # With a feed row highlighted the page keys walk the selection (a
            # windowful at a time), so the highlight travels with the window; with
            # none, they slide the feed window itself under the pinned heading.
            if self._selected is not None:
                self._select_index(self._selected - self._feed_window.page)
            else:
                self._feed_window.top -= self._feed_window.page
                self._session.invalidate()
        elif action in ("pagedown", "space"):
            if self._selected is not None:
                self._select_index(self._selected + self._feed_window.page)
            else:
                self._feed_window.top += self._feed_window.page
                self._session.invalidate()
        elif action in ("home", "ctrl_home"):
            # With the feed highlight active, Home jumps to the newest packet;
            # otherwise it slides the window to the feed's newest end (End mirrors).
            if self._selected is not None:
                self._select_index(0)
            else:
                self._feed_window.top = 0
                self._session.invalidate()
        elif action in ("end", "ctrl_end"):
            if self._selected is not None:
                self._select_index(len(self._feed) - 1)
            else:
                self._feed_window.to_end()
                self._session.invalidate()
        elif action == "left":
            self._scroll_line(-_HSCROLL_STEP)
        elif action == "right":
            self._scroll_line(_HSCROLL_STEP)
        elif action == "escape":
            self.resolve(None)

    def _scroll_line(self, delta: int) -> None:
        """Shift the highlighted row sideways (clamped to its own tail) and repaint."""
        shift = max(0, min(self._hshift + delta, self._hmax))
        if shift != self._hshift:
            self._hshift = shift
            self._session.invalidate()

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
            channels=self._channels, type_of=self._type_of, key_of=self._key_of,
            # The live feed itself (newest first), so the viewer keeps up with packets
            # that arrive while it is open instead of freezing at this snapshot.
            source=lambda: list(self._feed),
        )
        asyncio.ensure_future(self._session.run_screen(viewer))

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """The pinned heading, then the feed's window filling the viewport."""
        if self._selected is not None and self._feed:
            self._selected = min(self._selected, len(self._feed) - 1)
        else:
            self._hmax = 0  # nothing highlighted scrolls, so nothing advertises ←→
        lines = [render_to_ansi(self._heading(), width, no_wrap=True)]
        win = max(1, self._scroll_viewport - len(lines))
        lines.extend(self._feed_lines(width, win))
        self._scroll_total = max(1, len(lines))
        return lines

    def _heading(self) -> Text:
        """The feed's pinned status line, with a live-light for the hub."""
        heading = Text("newest first · Enter opens a packet  ", style="muted")
        if self._hub_active():
            heading.append("● live", style="ok")
        else:
            heading.append("○ waiting for a device", style="muted")
        return heading

    def _feed_lines(self, width: int, win: int) -> list[str]:
        """The feed's windowed rows: newest first, ``↑/↓ n more`` at the edges."""
        if not self._feed:
            return [render_to_ansi(Text("nothing heard yet", style="muted"), width)]
        show_label = width >= _FEED_LABEL_MIN_WIDTH
        entries = list(self._feed)
        top, count = self._feed_window.fit(len(entries), win, self._selected)
        out: list[str] = []
        if top > 0:
            out.append(render_to_ansi(ListWindow.marker(top, "above"), width))
        for i in range(top, top + count):
            row = self._feed_row(entries[i], i == self._selected, show_label, width)
            out.append(render_to_ansi(row, width, no_wrap=True))
        below = len(entries) - top - count
        if below > 0:
            out.append(render_to_ansi(ListWindow.marker(below, "below"), width))
        return out

    def _feed_row(
        self, entry: PacketEntry, selected: bool, show_label: bool, width: int
    ) -> Text:
        """Lay one feed row out in fixed lanes: time, class, node, reception, detail.

        The class lane leads with its two-cell icon; the textual label beside it is
        dropped wholesale on a narrow terminal (``show_label``), keeping the lanes
        aligned either way. The node name takes the app-wide palette hue (our own
        node white, a bare hash muted). Never wraps — the detail lane gets whatever
        width the fixed lanes leave (``width``).

        A row that runs past the right edge is read by scrolling it, not by growing it.
        An unhighlighted row simply elides its relay path's middle hops, keeping both
        ends of the route in view; the *highlighted* row instead slides under ``←→``
        (:attr:`_hshift`), carrying its detail whole so it can be read to the end — the
        app-wide h-scroll convention, with the ``▸`` pointer lane pinned so the cursor
        never scrolls away from the row it marks. The shift bound is measured here,
        against the current width, so a resize can only clamp it back into range.
        """
        row = Text(no_wrap=True, overflow="ellipsis")
        row.append("▸ " if selected else "  ", style="accent")
        avail = max(1, width - 2)

        body = Text(no_wrap=True, overflow="ellipsis")
        body.append(entry.when.astimezone().strftime("%H:%M:%S") + "  ", style="muted")
        body.append(kind_icon(entry.kind) + " ")
        if show_label:
            body.append(entry.kind.ljust(10), style=KIND_STYLES.get(entry.kind, "brand"))
        label, style = node_label(entry, self._resolve, self._self_name)
        if label == "?":
            label, style = self._feed_subject(entry)  # no node identity: name what we can
        body.append(fit_cells(label, _FEED_NAME_WIDTH), style=style)
        body.append("  ")
        body.append(
            f"{entry.snr:+5.1f} dB" if entry.snr is not None else " " * 8,
            style=snr_style(entry.snr) if entry.snr is not None else "muted",
        )
        body.append(
            f"  {entry.rssi:5.0f} dBm" if entry.rssi is not None else " " * 10,
            style="muted",
        )
        note = self._feed_note(
            entry, None if selected else max(1, avail - body.cell_len - 2)
        )
        if note is not None:
            body.append("  ")
            body.append_text(note)

        if selected:
            self._hmax = max(0, body.cell_len - avail)
            self._hshift = min(self._hshift, self._hmax)
            if self._hshift:
                body = crop_cells(body, self._hshift, avail)
        row.append_text(body)
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
                # A resolvable sender takes its key-derived hue; a stranger stays
                # muted — colour is reserved for keyed identities.
                return sender, ("you" if ours else name_style(sender, self._key_of(sender)))
            return "channel", "muted"
        if entry.kind == "packet":
            cls = payload_class(entry.raw)
            if cls:
                return cls, "muted"
        if entry.where:
            return entry.where, "muted"  # an ack's code, or any other stray context
        return "—", "muted"

    def _feed_note(self, entry: PacketEntry, budget: Optional[int]) -> Optional[Text]:
        """The row's trailing detail: a packet's relay path, a message's conversation.

        The path renders through THE path line. Given a ``budget`` — the cells the row's
        fixed lanes leave — a chain too long for the lane elides its *middle* hops behind
        ``⋯``, so the origin and the last relay (the ends a right-edge cut would amputate)
        always survive. ``None`` asks for the chain whole instead: that is the highlighted
        row, which scrolls sideways rather than eliding, so nothing may be dropped before
        ``←→`` has had the chance to reveal it.

        Args:
            entry: The packet the row describes.
            budget: Cells the detail may occupy, or ``None`` for the full-length line.

        Returns:
            The detail to append, or ``None`` when the class carries none.
        """
        if entry.kind == "packet" and entry.path is not None:
            note = Text("via ", style="muted")
            line = path_line(
                entry.path.split(","),
                self._resolve,
                prefix_bytes=self._prefix_bytes,
                self_name=self._self_name,
            )
            note.append_text(
                line.text() if budget is None else line.ellipsized(max(1, budget - 4))
            )
            return note
        if entry.kind == "message" and entry.where:
            return Text(entry.where, style="muted")
        return None


async def open_livefeed(ctx: "AppContext") -> None:
    """Open the live feed and run it until dismissed.

    Wires the screen to its feeds: stored recent observations seed it, a hub
    subscription streams every new packet in, and a once-a-second ticker keeps the
    live-light honest. Everything is torn down when the screen closes.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services import trace_runner
    from .surface import TuiUi
    from .timemachine_screen import _routing_prefix_bytes

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the live feed is only available in the menu")
    session = ctx.ui.session

    contacts = []
    self_name: Optional[str] = None
    channels: list[tuple[str, bytes]] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            # Through the session cache: contacts and the channel-slot probe are the two
            # slowest reads on a screen open, so reading them once (not per feed open)
            # is what keeps navigation snappy over Bluetooth.
            contacts = await ctx.devstate.contacts()
            self_name = (await ctx.devstate.self_info()).get("name") or None
            channels = [(s.name, s.secret) for s in await ctx.devstate.channel_slots()]
    except Exception:  # noqa: BLE001 - the feed renders fine without contact names
        contacts = []
    # Contacts first, every name the recorder ever overheard as the fallback — the
    # app-wide rule that a node we can name never renders as a bare hash.
    resolve = trace_runner.make_node_resolver(contacts, ctx.repo.node_names())
    type_of = trace_runner.make_node_type_resolver(contacts)
    key_of = trace_runner.make_name_key_resolver(contacts, ctx.repo.node_names())
    prefix_bytes = await _routing_prefix_bytes(ctx)

    seed = ctx.repo.recent_observations(since=utcnow() - OBSERVATION_WINDOW)
    screen = LiveFeedScreen(
        session=session,
        resolve=resolve,
        seed=seed,
        hub_active=lambda: ctx.events.active,
        prefix_bytes=prefix_bytes,
        self_name=self_name,
        channels=channels,
        type_of=type_of,
        key_of=key_of,
    )

    unsubscribe = ctx.events.subscribe(screen.on_event)

    async def tick() -> None:
        """Repaint once a second so the live-light and clock-sensitive rows stay honest."""
        while True:
            await asyncio.sleep(_REFRESH_S)
            session.invalidate()

    ticker = asyncio.ensure_future(tick())
    try:
        await session.run_screen(screen)
    finally:
        unsubscribe()
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - teardown must never surface a tick hiccup
            pass
