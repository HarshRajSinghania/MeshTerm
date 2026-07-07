"""The live chat screen and its launcher.

This is the interactive, full-screen chat experience: a scrolling transcript with an input
line pinned at the bottom, where messages you send and messages that arrive over the mesh
appear together in real time. Like the device picker and config editor, this module sits in
the UI layer but is allowed to depend on the context and services — it wires the
:class:`~meshtools.services.chat_service.ChatService` send path and the always-on event hub
to a :class:`~meshtools.ui.tui.screen.Screen`.

The screen subscribes to the hub for the duration it is open so inbound messages for the
current conversation append live; :class:`~meshtools.services.chat_service.ChatService`
independently persists every inbound message, so history is complete whether or not the
screen is open.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.events import EventKind, MeshEvent
from ..core.models import ChatMessage, Contact, Conversation, Message
from .theme import snr_style
from .tui.prompt import _LineEditor
from .tui.render import render_lines
from .tui.screen import CANCEL, Screen

if TYPE_CHECKING:
    from ..context import AppContext

#: Fixed page step for PageUp/PageDown while scrolling the transcript (the screen isn't
#: told the viewport height, so a constant keeps paging predictable).
_PAGE = 10

#: How many past messages to load into the transcript when a conversation opens.
_HISTORY_LIMIT = 200

#: Palette of distinct, dark-theme-friendly colors cycled through to give each channel
#: sender its own hue (red is reserved for errors, so it's excluded). ``you`` and unknown
#: senders are styled separately.
_SENDER_COLORS = (
    "bold #f472b6",  # pink
    "bold #60a5fa",  # blue
    "bold #34d399",  # green
    "bold #a78bfa",  # violet
    "bold #fb923c",  # orange
    "bold #22d3ee",  # cyan
    "bold #a3e635",  # lime
    "bold #e879f9",  # fuchsia
)

#: Matches the ``Name: message`` convention channel senders use to identify themselves
#: (the protocol carries no sender field). The name is 1–20 non-colon characters and must
#: be followed by ``": "`` — conservative enough to leave ``http://…`` and ``note:x`` alone.
_SENDER_PREFIX = re.compile(r"^([^\s:][^:]{0,19}):[ \t]+(.*)$", re.DOTALL)


def _split_channel_sender(text: str) -> tuple[Optional[str], str]:
    """Split a channel message into ``(sender_name, body)`` when it carries a name prefix.

    Channel messages have no sender field on the wire, so senders identify themselves by
    prefixing the text with ``Name: ``. Lifting that name out lets the transcript show it
    as a colored header and keep the body clean.

    Args:
        text: The raw channel message text.

    Returns:
        ``(name, body)`` when a plausible ``Name: `` prefix is present, else ``(None, text)``.
    """
    match = _SENDER_PREFIX.match(text)
    if match is None:
        return None, text
    name, body = match.group(1).strip(), match.group(2)
    if not name or name.isdigit() or body.startswith("//"):  # reject URLs / timestamps
        return None, text
    return name, body


class ChatScreen(Screen):
    """A live conversation: a scrolling transcript above a pinned input line.

    The transcript auto-sticks to the newest message (and snaps back to the bottom whenever
    you type or send). Scrolling up with the arrows / PageUp detaches from the bottom to
    read history; End re-attaches. Enter sends the current line; Esc leaves the chat.
    """

    floating = False

    def __init__(
        self,
        conversation: Conversation,
        messages: list[ChatMessage],
        *,
        send: Callable[[str], Awaitable[Optional[ChatMessage]]],
        names: dict[str, str],
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
    ) -> None:
        """Build the chat screen.

        Args:
            conversation: The thread being shown (its label titles the screen).
            messages: The initial transcript (history), oldest-first.
            send: Async callable that sends a line and returns the recorded outbound
                message (or ``None`` if nothing was sent).
            names: Map of contact key prefix to friendly name, for labeling inbound
                direct messages.
            session: The running :class:`~meshtools.ui.tui.session.TuiSession`, used to
                request repaints when messages arrive or a send completes.
        """
        super().__init__()
        self.title = conversation.label
        self._is_channel = conversation.is_channel
        self._messages = list(messages)
        self._send = send
        self._names = names
        self._session = session
        self._editor = _LineEditor()
        self._sending = False
        self._status = ""
        self._stick = True  # keep the newest message in view until the user scrolls up
        # Channel-only reply selection: index of the highlighted message (or None when the
        # compose line is focused), plus the body line it rendered on so the frame keeps it
        # in view. Direct chats keep the flat, free-scrolling view and never select.
        self._selected: Optional[int] = None
        self._selected_line: Optional[int] = None

    @property
    def footer_hint(self) -> str:
        """Key hint, reflecting whether a message is picked for reply (channels only)."""
        if not self._is_channel:
            return "Enter send · ↑↓/PgUp scroll · End latest · Esc back"
        if self._selected is not None:
            return "Enter reply (@mention) · ↑↓ pick · End/Esc cancel"
        return "Enter send · ↑ pick a message to reply · Esc back"

    # --- live updates --------------------------------------------------------

    def append(self, message: ChatMessage) -> None:
        """Append an inbound message to the transcript and repaint."""
        self._messages.append(message)
        # Don't yank the view to the tail while the user is picking a message to reply to.
        if self._selected is None:
            self._stick = True
        self._session.invalidate()

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the transcript, a divider, the input line, and any status."""
        self._selected_line = None
        if not self._messages:
            lines = render_lines(Text("No messages yet — say hello!", style="muted"), width)
        elif self._is_channel:
            # Channels carry many senders, so group consecutive messages from one sender
            # under a colored header (Discord/Slack style) rather than the flat one-line
            # format used for the two-party direct view. Rendering per-message also lets us
            # record where the picked reply target lands (see _selected_line / cursor_line).
            if self._selected is not None:
                self._selected = max(0, min(self._selected, len(self._messages) - 1))
            lines = self._render_channel(width)
        else:
            lines = render_lines(Group(*(self._format(m) for m in self._messages)), width)

        footer_parts: list[RenderableType] = [
            Text("─" * width, style="muted"),
            self._editor.render(),
        ]
        if self._is_channel and self._selected is not None:
            footer_parts.append(self._reply_banner())
        if self._status:
            footer_parts.append(Text(self._status, style="muted"))
        lines += render_lines(Group(*footer_parts), width)

        # Sticking to the bottom: hand the frame an over-large offset so it clamps the view
        # to the final lines (input + latest messages). Scrolling up clears the stick.
        if self._stick:
            self.scroll = len(lines)
        return lines

    def cursor_line(self) -> Optional[int]:
        """Keep the picked reply target in view; otherwise free scroll (managed by stick)."""
        return self._selected_line

    def _format(self, message: ChatMessage) -> Text:
        """Render one transcript message as ``HH:MM  who  text`` with quality cues."""
        stamp = message.created_at.astimezone().strftime("%H:%M")
        if message.outbound:
            who, style = "you", "accent"
        elif message.is_channel:
            who, style = (message.peer_name or "·"), "brand"
        else:
            who, style = (self._name(message.peer) or message.peer or "?"), "brand"

        line = Text()
        line.append(f"{stamp} ", style="muted")
        line.append(f"{who:>10.10} ", style=style)
        line.append(message.text)
        if message.outbound and message.acked is False:
            line.append("  ⚠ no ack", style="warn")
        if message.snr is not None:
            line.append(f"  {message.snr:+.0f} dB", style=snr_style(message.snr))
        return line

    def _name(self, peer: Optional[str]) -> Optional[str]:
        """Resolve a sender key prefix to a contact name (exact, then prefix match)."""
        if not peer:
            return None
        needle = peer.lower()
        if needle in self._names:
            return self._names[needle]
        for prefix, name in self._names.items():
            if prefix.startswith(needle) or needle.startswith(prefix):
                return name
        return None

    # --- channel (grouped) rendering -----------------------------------------

    def _render_channel(self, width: int) -> list[str]:
        """Render the channel transcript to ANSI lines, tracking the picked message's row.

        Consecutive messages from the same sender on the same day share one colored header,
        with each message body indented below. A muted divider marks each new day, and a
        blank line separates distinct sender groups. Rendering message-by-message (rather
        than as one Group) lets us record the body line of the selected reply target in
        :attr:`_selected_line` so the frame can scroll it into view.
        """
        lines: list[str] = []
        prev_sender: Optional[str] = None
        prev_day = None
        for idx, message in enumerate(self._messages):
            stamp = message.created_at.astimezone()
            day = stamp.date()
            sender, body = self._sender_and_body(message)
            new_day = day != prev_day
            if new_day:
                if lines:
                    lines += render_lines(Text(""), width)
                lines += render_lines(
                    Text(f"── {stamp:%a} {stamp:%b} {stamp.day} ──", style="muted"), width
                )
            if new_day or sender != prev_sender:
                if not new_day and lines:
                    lines += render_lines(Text(""), width)  # gap between sender groups
                lines += render_lines(self._group_header(sender), width)
            selected = idx == self._selected
            if selected:
                self._selected_line = len(lines)
            lines += render_lines(self._body_line(body, message, selected=selected), width)
            prev_sender, prev_day = sender, day
        return lines

    def _sender_and_body(self, message: ChatMessage) -> tuple[str, str]:
        """Return the display sender and cleaned body for a channel message."""
        if message.outbound:
            return "you", message.text
        name, body = _split_channel_sender(message.text)
        return (name or "·"), body

    def _group_header(self, sender: str) -> Text:
        """Build the sender header that starts a group, colored per sender."""
        return Text(sender, style=self._sender_style(sender))

    def _body_line(self, body: str, message: ChatMessage, *, selected: bool = False) -> Text:
        """Build one indented message line, each stamped with its own time.

        The per-message timestamp lives here (not on the group header) so every message
        shows the time it was actually sent, even when several are grouped under one
        sender — otherwise a run of same-sender messages would appear to share one time.
        When ``selected``, the line is marked as the reply target (matching the select
        screen's ``❯`` pointer and brand highlight).
        """
        stamp = message.created_at.astimezone()
        line = Text()
        line.append("❯ " if selected else "  ", style="brand" if selected else None)
        line.append(f"{stamp:%H:%M}  ", style="brand" if selected else "muted")
        line.append(body, style="brand" if selected else None)
        if message.outbound and message.acked is False:
            line.append("  ⚠ no ack", style="warn")
        if message.snr is not None:
            line.append(f"  {message.snr:+.0f} dB", style=snr_style(message.snr))
        return line

    def _reply_banner(self) -> Text:
        """One-line cue shown above the input when a message is picked to reply to."""
        sender, _ = self._sender_and_body(self._messages[self._selected])
        who = sender if sender not in ("·", "you") else "this message"
        return Text(
            f"↩ Enter to reply to {who} with an @mention · End to cancel", style="accent"
        )

    def _sender_style(self, sender: str) -> str:
        """Pick a stable color for a channel sender (accent for us, muted for unknown)."""
        if sender == "you":
            return "accent"
        if sender == "·":
            return "muted"
        return _SENDER_COLORS[sum(map(ord, sender)) % len(_SENDER_COLORS)]

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Dispatch a key: channels navigate a reply selection; direct chats free-scroll."""
        if self._is_channel:
            self._handle_channel(action, data)
        else:
            self._handle_flat(action, data)

    def _handle_flat(self, action: str, data: str = "") -> None:
        """Direct-chat input: send on Enter, scroll the transcript, edit, or leave on Esc."""
        if action == "enter":
            self._submit()
        elif action == "escape":
            self.resolve(CANCEL)
        elif action == "up":
            self._stick = False
            self.scroll = max(0, self.scroll - 1)
        elif action == "pageup":
            self._stick = False
            self.scroll = max(0, self.scroll - _PAGE)
        elif action == "down":
            self.scroll += 1
        elif action == "pagedown":
            self.scroll += _PAGE
        elif action == "home":
            self._stick = False
            self.scroll = 0
        elif action == "end":
            self._stick = True
        else:
            if self._editor.edit(action, data):
                self._stick = True  # typing snaps back to the live tail

    def _handle_channel(self, action: str, data: str = "") -> None:
        """Channel input: arrows pick a message to reply to; Enter sends or starts a reply.

        With no message picked the view behaves like the compose line (Enter sends). Moving
        up enters the selection from the newest message; moving down past the newest (or
        End) returns focus to the compose line and clears the selection. Enter on a picked
        message primes the input with an ``@mention`` instead of sending.
        """
        if action == "enter":
            if self._selected is not None:
                self._begin_reply()
            else:
                self._submit()
        elif action == "escape":
            if self._selected is not None:
                self._clear_selection()  # first Esc deselects; next leaves the chat
                self._session.invalidate()
            else:
                self.resolve(CANCEL)
        elif action == "up":
            self._move_selection(-1)
        elif action == "pageup":
            self._move_selection(-_PAGE)
        elif action == "down":
            self._move_selection(1)
        elif action == "pagedown":
            self._move_selection(_PAGE)
        elif action == "home":
            if self._messages:
                self._selected = 0
                self._stick = False
        elif action == "end":
            self._clear_selection()
            self._stick = True  # jump back to the live tail / compose line
        else:
            if self._editor.edit(action, data):
                # Touching the compose line returns focus there — nothing stays selected.
                self._clear_selection()
                self._stick = True

    def _move_selection(self, delta: int) -> None:
        """Move the reply selection by ``delta`` messages (negative = toward older).

        Entering from the compose line only happens moving up (``delta < 0``); moving past
        the newest message drops the selection and re-sticks to the tail.
        """
        if not self._messages:
            return
        last = len(self._messages) - 1
        if self._selected is None:
            if delta < 0:
                self._selected = last
                self._stick = False
            return
        target = self._selected + delta
        if target > last:
            self._clear_selection()
            self._stick = True
        else:
            self._selected = max(0, target)
            self._stick = False

    def _clear_selection(self) -> None:
        """Drop any reply selection (focus returns to the compose line)."""
        self._selected = None
        self._selected_line = None

    def _begin_reply(self) -> None:
        """Prime the compose line with an ``@mention`` of the picked message's sender."""
        message = self._messages[self._selected]
        sender, _ = self._sender_and_body(message)
        mention = f"@[{sender}] " if sender and sender not in ("you", "·") else "@"
        self._editor = _LineEditor(mention + self._editor.text)
        self._clear_selection()
        self._stick = True
        self._session.invalidate()

    def _submit(self) -> None:
        """Send the current input line as a message (scheduled off the key handler)."""
        text = self._editor.text.strip()
        if not text or self._sending:
            return
        self._editor = _LineEditor()
        self._sending = True
        self._status = "sending…"
        self._stick = True

        async def _run() -> None:
            try:
                message = await self._send(text)
                if message is not None:
                    self._messages.append(message)
                self._status = ""
            except Exception as exc:  # noqa: BLE001 - report inline, keep the chat alive
                self._status = f"send failed: {exc}"
            finally:
                self._sending = False
                self._stick = True
                self._session.invalidate()

        asyncio.ensure_future(_run())


async def open_chat(ctx: "AppContext", conversation: Conversation) -> int:
    """Open the live chat screen for ``conversation`` and run it until dismissed.

    Ensures listening and message recording are active, loads the stored transcript,
    subscribes to the hub so inbound messages for this thread append live, and pushes the
    screen. The subscription and the active-conversation marker are always cleaned up on
    exit.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        conversation: The channel or contact conversation to open.

    Returns:
        The number of messages shown in the transcript when the screen closed.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("live chat is only available in the interactive menu")
    session = ctx.ui.session

    device = await ctx.device()
    try:
        await ctx.chat.start()  # begin recording inbound if it wasn't already
    except Exception:  # noqa: BLE001 - the hub may already be running; recording is best-effort
        pass

    history = ctx.repo.recent_chat_messages(
        is_channel=conversation.is_channel,
        channel_id=conversation.channel_id,
        peer=conversation.peer,
        limit=_HISTORY_LIMIT,
    )
    names = _contact_names(await device.get_contacts())

    async def send(text: str) -> Optional[ChatMessage]:
        if conversation.is_channel:
            assert conversation.channel_idx is not None
            return await ctx.chat.send_channel(
                conversation.channel_idx, text, label=conversation.label
            )
        assert conversation.contact is not None
        return await ctx.chat.send_direct(conversation.contact, text)

    screen = ChatScreen(conversation, history, send=send, names=names, session=session)
    ctx.chat.set_active(conversation.key)

    def on_event(event: MeshEvent) -> None:
        message = event.message
        if message is not None and _belongs(message, conversation):
            peer_name = names.get((message.sender or "").lower())
            screen.append(
                ChatMessage.from_message(
                    message, peer_name=peer_name, channel_id=conversation.channel_id
                )
            )

    unsubscribe = ctx.events.subscribe(on_event, EventKind.MESSAGE)
    try:
        await session.run_screen(screen)
    finally:
        unsubscribe()
        ctx.chat.set_active(None)
    return len(screen._messages)


def _contact_names(contacts: list[Contact]) -> dict[str, str]:
    """Build a key-prefix → name map for labeling inbound direct messages."""
    names: dict[str, str] = {}
    for contact in contacts:
        for key in (contact.key_prefix, contact.public_key[:12]):
            if key:
                names[key.lower()] = contact.name
    return names


def _belongs(message: Message, conversation: Conversation) -> bool:
    """Whether an inbound message belongs to the open conversation.

    Args:
        message: The received message.
        conversation: The conversation currently shown.

    Returns:
        ``True`` if the message should append to this transcript.
    """
    if conversation.is_channel:
        return message.is_channel and message.channel == conversation.channel_idx
    if message.is_channel:
        return False
    sender = (message.sender or "").lower()
    peer = (conversation.peer or "").lower()
    if not sender or not peer:
        return False
    return sender.startswith(peer) or peer.startswith(sender)
