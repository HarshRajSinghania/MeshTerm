"""The live chat screen and its launcher.

This is the interactive, full-screen chat experience: a scrolling transcript with an input
line pinned at the bottom, where messages you send and messages that arrive over the mesh
appear together in real time. Like the device picker and config editor, this module sits in
the UI layer but is allowed to depend on the context and services — it wires the
:class:`~meshterm.services.chat_service.ChatService` send path and the always-on event hub
to a :class:`~meshterm.ui.tui.screen.Screen`.

The screen subscribes to the hub for the duration it is open so inbound messages for the
current conversation append live; :class:`~meshterm.services.chat_service.ChatService`
independently persists every inbound message, so history is complete whether or not the
screen is open.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.channels import split_channel_sender
from ..core.events import EventKind, MeshEvent
from ..core.models import ChatMessage, Contact, Conversation, Message, utcnow
from .theme import name_style, snr_style
from .tui.prompt import _LineEditor
from .tui.render import render_hanging, render_lines, right_aligned_tail
from .tui.screen import CANCEL, Screen
from .tui.spinner import Spinner

if TYPE_CHECKING:
    from ..context import AppContext

#: How many past messages to load into the transcript when a conversation opens.
_HISTORY_LIMIT = 200

#: Matches an ``@[Name]`` mention token, as the reply flow primes into the compose line (see
#: :meth:`ChatScreen._begin_reply`). The transcript renders each as a bare ``@Name`` colored
#: in that sender's hue instead of showing the literal brackets. Name is 1–20 non-``]`` chars.
_MENTION = re.compile(r"@\[([^\]]{1,20})\]")


#: Delivery-state marks for a *resolved* outbound direct message, shown at the end of its
#: line: acknowledged, or transmitted-but-unacknowledged (retryable via ^R). The app-wide
#: ``✓``/``✗`` status marks in their ok/err styles — not the ✅/❌ emoji, which belong to
#: the packet-class icon lane. While the ack is still pending the line shows an animated
#: spinner instead (see :meth:`ChatScreen._delivery_glyph`).
_DELIVERED = ("✓", "ok")
_FAILED = ("✗", "err")

#: Seconds between spinner frames on a message that is still awaiting its ack.
_SPINNER_INTERVAL = 0.12

#: How many UTF-8 bytes a single outgoing message may carry, by conversation kind. MeshCore's
#: LoRa payload caps a direct message at 150 bytes and an (unscoped) channel broadcast at 130;
#: over the limit the companion would silently drop the packet, so we block the send instead
#: and show the running byte budget in the compose bar.
_DM_BYTE_LIMIT = 150
_CHANNEL_BYTE_LIMIT = 130

#: Byte-counter thresholds (bytes *remaining*) at which its color escalates, plus the two
#: mid-band hues. The theme's ``warn``/``err`` sit too close together (an amber that reads
#: orange, then red), so the counter names a truer yellow and orange directly to keep the
#: green→yellow→orange→red fuel gauge visibly stepped.
_BYTES_TIGHT, _BYTES_LOW = 20, 10
_BYTES_YELLOW = "bold #fde047"
_BYTES_ORANGE = "bold #ff9500"


def _sender_hue(sender: str) -> str:
    """The stable per-sender colour a name is drawn in, keyed on the name's characters.

    The app-wide name palette (:func:`~meshterm.ui.theme.name_style`), shared by the live
    transcript (sender headers, ``@mentions``), the conversation list (a contact's colour
    dot, a channel preview's inline sender), the dashboard feed, and the packet viewer, so
    a person reads the same colour everywhere. ``you`` and unknown (``·``) senders are
    handled by the caller.
    """
    return name_style(sender)


#: The sender-prefix parser, shared app-wide from the protocol layer (the transcript,
#: the conversation picker, the dashboard feed, and the message-paths matcher must all
#: split ``Name: body`` identically). Kept under its old private name for the callers
#: that import it from here.
_split_channel_sender = split_channel_sender


class ChatScreen(Screen):
    """A live conversation: a scrolling transcript above a pinned input line.

    The transcript auto-sticks to the newest message (and snaps back to the bottom
    whenever you type or send). ↑ picks a message — the pick walks with ↑↓/PgUp/PgDn
    and carries the view with it; ^End (or Esc) returns focus to the compose line.
    Enter sends the current line, or acts on a picked message: in a channel it primes
    a reply ``@mention``, in a direct chat it opens the message's delivery paths. ^P
    opens the paths of the picked (or latest) message in either kind. Esc leaves the
    chat once nothing is picked.
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
        resend: Optional[Callable[[ChatMessage], Awaitable[ChatMessage]]] = None,
        paths: Optional[Callable[[ChatMessage], Awaitable[None]]] = None,
    ) -> None:
        """Build the chat screen.

        Args:
            conversation: The thread being shown (its label titles the screen).
            messages: The initial transcript (history), oldest-first.
            send: Async callable that sends a line and returns the recorded outbound
                message (or ``None`` if nothing was sent).
            names: Map of contact key prefix to friendly name, for labeling inbound
                direct messages.
            session: The running :class:`~meshterm.ui.tui.session.TuiSession`, used to
                request repaints when messages arrive or a send completes.
            resend: Async callable that re-attempts delivery of an unacknowledged direct
                message, updating it in place (direct chats only; ``None`` for channels).
            paths: Async callable that presents the delivery paths of one message (the
                ^P view); ``None`` leaves the affordance quietly inert.
        """
        super().__init__()
        self.title = conversation.label
        self._is_channel = conversation.is_channel
        self._messages = list(messages)
        self._send = send
        self._resend = resend
        self._paths = paths
        self._names = names
        self._session = session
        self._editor = _LineEditor()
        self._sending = False
        # Cycled while a direct message is in flight, so its trailing glyph spins (rather than
        # a static hourglass) until the ack resolves. Shared across messages: only one send or
        # retry is ever in flight at a time (both gated by ``_sending``).
        self._spinner = Spinner()
        self._status = ""
        self._stick = True  # keep the newest message in view until the user scrolls up
        self._paths_open = False  # one paths dialog at a time
        # The pick: index of the highlighted message (or None when the compose line is
        # focused), plus the body line it rendered on so the frame keeps it in view.
        self._selected: Optional[int] = None
        self._selected_line: Optional[int] = None

    @property
    def footer_hint(self) -> str:
        """Key hint, reflecting whether a message is picked and what Enter does to it."""
        if self._selected is not None:
            if self._is_channel:
                return "Enter reply (@mention) · ^P paths · ↑↓ pick · ^End/Esc cancel"
            return "Enter paths · ↑↓ pick · ^End/Esc cancel"
        if not self._is_channel and any(
            m.outbound and m.acked is False for m in self._messages
        ):
            return "Enter send · ↑ pick a message · ^R retry failed · ^P paths · Esc back"
        return "Enter send · ↑ pick a message · ^P paths · Esc back"

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
        else:
            # Both direct and channel threads use the same grouped, Discord/Slack-style
            # transcript: consecutive messages from one sender share a colored header, with
            # day dividers between them. Rendering per-message also lets us record where the
            # picked reply target lands (channels only; see _selected_line / cursor_line).
            if self._selected is not None:
                self._selected = max(0, min(self._selected, len(self._messages) - 1))
            lines = self._render_grouped(width)

        limit = self._byte_limit()
        # The byte budget is pinned to the right edge of the input's *last* line — so a
        # compose that wraps onto a second line keeps the counter in the bottom-right
        # corner rather than letting it trail the cursor down the wrap. Only a last line
        # already full to the edge pushes it onto a right-aligned line of its own.
        input_line = self._editor.render(overflow_at=self._overflow_at(limit))
        counter = self._byte_counter(limit)
        compose = right_aligned_tail(input_line, counter, width)
        footer_parts: list[RenderableType] = [
            Text("─" * width, style="muted"),
            compose,
        ]
        if self._selected is not None:
            footer_parts.append(self._pick_banner())
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

    # --- outgoing byte budget ------------------------------------------------

    def _byte_limit(self) -> int:
        """The UTF-8 byte ceiling for a message in this conversation (channel vs direct)."""
        return _CHANNEL_BYTE_LIMIT if self._is_channel else _DM_BYTE_LIMIT

    def _used_bytes(self) -> int:
        """UTF-8 byte length of the current compose buffer — what counts against the limit."""
        return len(self._editor.text.encode("utf-8"))

    def _overflow_at(self, limit: int) -> Optional[int]:
        """Index of the first compose character whose bytes spill past ``limit``, else ``None``.

        Walking by character (not byte) keeps multibyte input intact: an emoji or accented
        letter is entirely under or entirely over the line, never split mid-sequence.
        """
        total = 0
        for i, ch in enumerate(self._editor.text):
            total += len(ch.encode("utf-8"))
            if total > limit:
                return i
        return None

    def _byte_counter(self, limit: int) -> Text:
        """The inline ``used/limit`` budget; only ``used`` is colored by how much is left.

        Green with room to spare, yellow within :data:`_BYTES_TIGHT` bytes, orange within
        :data:`_BYTES_LOW`, and red once the limit is met or exceeded — so the number reads as
        a fuel gauge while the ``/limit`` suffix stays muted (it never changes).
        """
        used = self._used_bytes()
        counter = Text()
        counter.append(str(used), style=self._byte_style(limit - used))
        counter.append(f"/{limit}", style="muted")
        return counter

    @staticmethod
    def _byte_style(remaining: int) -> str:
        """Map bytes remaining to the counter's escalating color (green→yellow→orange→red)."""
        if remaining <= 0:
            return "err"  # at or over the limit — the send is blocked
        if remaining <= _BYTES_LOW:
            return _BYTES_ORANGE
        if remaining <= _BYTES_TIGHT:
            return _BYTES_YELLOW
        return "ok"

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

    # --- grouped rendering ---------------------------------------------------

    def _render_grouped(self, width: int) -> list[str]:
        """Render the transcript to ANSI lines, tracking the picked message's row.

        Serves both direct and channel threads. Consecutive messages from the same sender on
        the same day share one colored header, with each message body indented below. A muted
        divider marks each new day, and a blank line separates distinct sender groups.
        Rendering message-by-message (rather than as one Group) lets us record the body line
        of the selected reply target in :attr:`_selected_line` so the frame can scroll it into
        view (channels only; direct threads never select).
        """
        lines: list[str] = []
        # Record each day divider as a sticky-header candidate, so the divider governing the
        # topmost visible message is re-pinned to the top row once it scrolls off — the same
        # base Screen.sticky_header the conversation picker uses for its section headings.
        self._sticky_headers = []
        prev_group: Optional[tuple[bool, str]] = None
        prev_day = None
        for idx, message in enumerate(self._messages):
            stamp = message.created_at.astimezone()
            day = stamp.date()
            sender, body = self._sender_and_body(message)
            # Group by (are-we-the-sender, display name), not the name alone, so our own
            # messages never merge with a remote sender who happens to be named the same.
            group = (message.outbound, sender)
            new_day = day != prev_day
            if new_day:
                if lines:
                    lines += render_lines(Text(""), width)
                divider = render_lines(
                    Text(f"── {stamp:%a} {stamp:%b} {stamp.day} ──", style="muted"), width
                )
                self._sticky_headers.append((len(lines), divider[0]))
                lines += divider
            if new_day or group != prev_group:
                if not new_day and lines:
                    lines += render_lines(Text(""), width)  # gap between sender groups
                header = self._group_header(sender, is_self=message.outbound)
                lines += render_lines(header, width)
            selected = idx == self._selected
            if selected:
                self._selected_line = len(lines)
            lines += self._body_lines(body, message, width, selected=selected)
            prev_group, prev_day = group, day
        return lines

    def _sender_and_body(self, message: ChatMessage) -> tuple[str, str]:
        """Return the display sender and cleaned body for a message.

        Our own messages are ``you``. Inbound channel messages carry a ``Name: `` prefix we
        lift into the sender (falling back to ``·`` when absent); inbound direct messages take
        the sender from the resolved contact name (or the raw key, or ``?``).
        """
        if message.outbound:
            return "you", message.text
        if message.is_channel:
            name, body = _split_channel_sender(message.text)
            return (name or "·"), body
        return (self._name(message.peer) or message.peer or "?"), message.text

    def _group_header(self, sender: str, *, is_self: bool = False) -> Text:
        """Build the sender header that starts a group, colored per sender."""
        return Text(sender, style=self._sender_style(sender, is_self=is_self))

    def _body_lines(
        self, body: str, message: ChatMessage, width: int, *, selected: bool = False
    ) -> list[str]:
        """Render one message to ANSI lines, stamped with its own time and hanging-indented.

        The per-message timestamp lives here (not on the group header) so every message
        shows the time it was actually sent, even when several are grouped under one
        sender — otherwise a run of same-sender messages would appear to share one time.
        A long body wraps with a hanging indent so continuation lines align under the body
        rather than under the timestamp gutter. When ``selected``, the line is marked as the
        reply target (matching the select screen's ``❯`` pointer and brand highlight).
        """
        stamp = message.created_at.astimezone()
        prefix = Text()
        prefix.append("❯ " if selected else "  ", style="brand" if selected else None)
        prefix.append(f"{stamp:%H:%M}  ", style="brand" if selected else "muted")
        body_text = self._body_text(body, message, selected=selected)
        return render_hanging(prefix, body_text, width, indent=prefix.cell_len)

    def _body_text(self, body: str, message: ChatMessage, *, selected: bool) -> Text:
        """Build the styled body of a message: mentions colored, then any trailing glyphs."""
        text = self._render_mentions(body, selected=selected)
        if message.outbound:
            # Direct messages track per-message delivery (spin → ✅/❌, retryable); channel
            # broadcasts have no ack, so only flag one that failed to leave the companion.
            if message.is_channel:
                if message.acked is False:
                    text.append("  ⚠ no ack", style="warn")
            else:
                text.append("  ")
                text.append_text(self._delivery_glyph(message.acked))
        if message.snr is not None:
            text.append(f"  {message.snr:+.0f} dB", style=snr_style(message.snr))
        return text

    def _render_mentions(self, body: str, *, selected: bool) -> Text:
        """Render body text, rewriting each ``@[Name]`` token to a ``@Name`` in its hue.

        The reply flow primes the compose line with an ``@[Name]`` token (see
        :meth:`_begin_reply`); here it reads back as a bare ``@Name`` colored in that
        sender's stable hue, so a mention is visually tied to the person it names. Text
        around the mentions keeps the line's base style (brand when the message is the
        picked reply target, otherwise unstyled).
        """
        base = "brand" if selected else None
        text = Text()
        pos = 0
        for match in _MENTION.finditer(body):
            if match.start() > pos:
                text.append(body[pos : match.start()], style=base)
            name = match.group(1)
            text.append(f"@{name}", style=self._sender_style(name))
            pos = match.end()
        if pos < len(body):
            text.append(body[pos:], style=base)
        return text

    def _delivery_glyph(self, acked: Optional[bool]) -> Text:
        """Map an outbound direct message's ``acked`` state to its trailing mark.

        A message still awaiting its ack (``acked is None``) shows the current spinner frame,
        animated by :meth:`_spin_while` for as long as the send is in flight; a resolved one
        shows the delivered ``✓`` (ok) or the unacknowledged ``✗`` (err).
        """
        if acked is None:
            return self._spinner.text()
        glyph, style = _DELIVERED if acked else _FAILED
        return Text(glyph, style=style)

    def _pick_banner(self) -> Text:
        """One-line cue shown above the input while a message is picked.

        Channels lead with the reply affordance (Enter's job there); direct chats with
        the paths view (their Enter). Both mention what the pick is for, so the state
        never reads as a mystery highlight.
        """
        message = self._messages[self._selected]
        sender, _ = self._sender_and_body(message)
        who = "this message" if message.outbound or sender == "·" else sender
        if self._is_channel:
            return Text(
                f"↩ Enter to reply to {who} with an @mention · End to cancel",
                style="accent",
            )
        return Text(
            "Enter to see the paths this message took · End to cancel", style="accent"
        )

    def _sender_style(self, sender: str, *, is_self: bool = False) -> str:
        """Pick a stable color for a channel sender.

        Our own messages are white — keyed on ``is_self`` (the message being outbound), not
        on the ``"you"`` label, so a remote sender who happens to be named ``you`` still gets
        a hue from the palette rather than masquerading as us. ``·`` (unknown) is muted; every
        other sender gets a stable hue derived from its name.
        """
        if is_self:
            return "you"  # white, out of the per-sender hue range — always easy to spot
        if sender == "·":
            return "muted"
        return _sender_hue(sender)

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Dispatch a key: pick and act on messages, edit the compose line, or leave.

        Both chat kinds share one model. With no message picked, Enter sends and typing
        edits the compose line. ↑ picks the newest message; the pick then walks with
        ↑↓, PgUp/PgDn (a screenful), Ctrl+Home (the very first message), and
        Ctrl+PgUp/PgDn (day dividers), carrying the view with it. Enter on a picked
        message primes a reply ``@mention`` in a channel and opens the delivery paths
        in a direct chat; ^P opens the paths of the picked (or latest) message in
        either kind. ^End (or moving past the newest) returns to the compose line;
        Esc peels the pick first, the screen second.
        """
        if action == "enter":
            if self._selected is not None:
                if self._is_channel:
                    self._begin_reply()
                else:
                    self._open_paths(self._selected)
            else:
                self._submit()
        elif action == "paths":
            target = self._selected if self._selected is not None else len(self._messages) - 1
            self._open_paths(target)
        elif action == "retry":
            if not self._is_channel:
                self._retry()
        elif action == "escape":
            if self._selected is not None:
                self._clear_selection()  # first Esc unpicks; next leaves the chat
                self._session.invalidate()
            else:
                self.resolve(CANCEL)
        elif action == "up":
            self._move_selection(-1)
        elif action == "pageup":
            self._move_selection(-self._page_step)
        elif action == "down":
            self._move_selection(1)
        elif action == "pagedown":
            self._move_selection(self._page_step)
        elif action == "ctrl_home":
            if self._messages:
                self._selected = 0
                self._stick = False
        elif action == "ctrl_pageup":
            self._select_section(-1)
        elif action == "ctrl_pagedown":
            self._select_section(1)
        elif action == "ctrl_end":
            self._clear_selection()
            self._stick = True  # jump back to the live tail / compose line
        else:
            if self._editor.edit(action, data):
                # Touching the compose line returns focus there — nothing stays picked.
                self._status = ""  # trimming clears the "too long" notice
                self._clear_selection()
                self._stick = True

    def _open_paths(self, index: Optional[int]) -> None:
        """Float the delivery-paths view for the message at ``index`` (one at a time)."""
        if index is None or not self._messages or self._paths is None or self._paths_open:
            return
        message = self._messages[max(0, min(index, len(self._messages) - 1))]
        self._paths_open = True

        async def run() -> None:
            try:
                await self._paths(message)
            finally:
                self._paths_open = False
                self._session.invalidate()

        asyncio.ensure_future(run())

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

    def _select_section(self, direction: int) -> None:
        """Jump the reply selection to the first message of the previous/next day.

        The selection-space analogue of the transcript's Ctrl+PageUp/PageDown day jump: down
        moves to the first message of the following day (dropping to the tail when there's no
        later day); up moves to the first message of the current day, or the previous day's
        when already atop one.
        """
        if not self._messages:
            return
        starts = self._day_start_indices()
        current = self._selected if self._selected is not None else len(self._messages) - 1
        if direction > 0:
            target = next((s for s in starts if s > current), None)
            if target is None:
                self._clear_selection()
                self._stick = True
                return
        else:
            governing = max((s for s in starts if s <= current), default=0)
            target = (
                governing
                if governing < current
                else max((s for s in starts if s < current), default=0)
            )
        self._selected = target
        self._stick = False

    def _day_start_indices(self) -> list[int]:
        """Message indices that begin a new local-day group — matching the transcript dividers."""
        starts: list[int] = []
        prev_day = None
        for i, message in enumerate(self._messages):
            day = message.created_at.astimezone().date()
            if day != prev_day:
                starts.append(i)
                prev_day = day
        return starts

    def _clear_selection(self) -> None:
        """Drop any reply selection (focus returns to the compose line)."""
        self._selected = None
        self._selected_line = None

    def _begin_reply(self) -> None:
        """Prime the compose line with an ``@mention`` of the picked message's sender."""
        message = self._messages[self._selected]
        sender, _ = self._sender_and_body(message)
        named = not message.outbound and sender != "·"
        mention = f"@[{sender}] " if named else "@"
        self._editor = _LineEditor(mention + self._editor.text)
        self._clear_selection()
        self._stick = True
        self._session.invalidate()

    def _submit(self) -> None:
        """Send the current input line as a message (scheduled off the key handler).

        Refuses an over-budget line: the buffer is kept intact (so the user can trim it) and
        the overage is reported, matching the red overflow the compose bar already shows.
        """
        text = self._editor.text.strip()
        if not text or self._sending:
            return
        limit = self._byte_limit()
        over = self._used_bytes() - limit
        if over > 0:
            self._status = f"Too long by {over} byte{'s' if over != 1 else ''} — trim to send."
            self._session.invalidate()
            return
        self._editor = _LineEditor()
        self._sending = True
        self._stick = True
        if self._is_channel:
            self._status = "sending…"
            self._session.invalidate()
            asyncio.ensure_future(self._send_channel(text))
        else:
            # Direct chats show an optimistic bubble whose trailing mark tracks delivery:
            # a spinner now, then ✓/✗ once the ack resolves (or times out). acked=None
            # ⇒ still spinning.
            pending = ChatMessage(text=text, outbound=True, created_at=utcnow())
            self._messages.append(pending)
            self._status = ""
            self._session.invalidate()
            asyncio.ensure_future(self._send_direct(pending))

    async def _send_channel(self, text: str) -> None:
        """Broadcast a channel message and append it once the companion accepts it."""
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

    async def _spin_while(self, coro: Awaitable[Any]) -> Any:
        """Await ``coro`` while animating the delivery spinner on the in-flight message.

        A background timer advances the shared spinner and repaints every
        :data:`_SPINNER_INTERVAL` seconds, so a pending message's trailing glyph spins until
        the ack resolves. The timer is always cancelled (and awaited, so it can't outlive the
        send as a stray pending task) before returning. The spinner is cosmetic, so any hiccup
        in the animation is swallowed rather than allowed to break the send.
        """
        self._spinner.reset()

        async def animate() -> None:
            while True:
                await asyncio.sleep(_SPINNER_INTERVAL)
                self._spinner.tick()
                self._session.invalidate()

        ticker = asyncio.ensure_future(animate())
        try:
            return await coro
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a send
                pass

    async def _send_direct(self, pending: ChatMessage) -> None:
        """Await delivery of the optimistic ``pending`` bubble, swapping in the stored row.

        On success the pending bubble is replaced by the recorded message (carrying its
        resolved ``acked`` state and row id, so a ❌ can later be retried in place). A hard
        failure — the companion rejecting the send outright — drops the bubble and reports
        the error inline, matching how a failed send has always surfaced.
        """
        try:
            message = await self._spin_while(self._send(pending.text))
        except Exception as exc:  # noqa: BLE001 - report inline, keep the chat alive
            self._discard(pending)
            self._status = f"send failed: {exc}"
        else:
            if message is not None:
                self._swap(pending, message)
            else:
                self._discard(pending)
            self._status = ""
        finally:
            self._sending = False
            self._stick = True
            self._session.invalidate()

    def _retry(self) -> None:
        """Re-attempt delivery of the most recent unacknowledged direct message (Ctrl-R)."""
        if self._sending or self._resend is None:
            return
        target = next(
            (m for m in reversed(self._messages) if m.outbound and m.acked is False),
            None,
        )
        if target is None:
            return
        self._sending = True
        target.acked = None  # back to ⏳ while the retry is in flight
        self._status = "retrying…"
        self._stick = True
        self._session.invalidate()
        asyncio.ensure_future(self._resend_message(target))

    async def _resend_message(self, message: ChatMessage) -> None:
        """Drive a retry to completion, refreshing the message's delivery state in place."""
        assert self._resend is not None
        try:
            await self._spin_while(self._resend(message))
        except Exception as exc:  # noqa: BLE001 - report inline, keep the chat alive
            message.acked = False
            self._status = f"retry failed: {exc}"
        else:
            self._status = "" if message.acked else "still no ack — ^R to retry"
        finally:
            self._sending = False
            self._stick = True
            self._session.invalidate()

    def _swap(self, old: ChatMessage, new: ChatMessage) -> None:
        """Replace an optimistic bubble with its recorded message, in place."""
        try:
            self._messages[self._messages.index(old)] = new
        except ValueError:  # pragma: no cover - the bubble is always still present
            self._messages.append(new)

    def _discard(self, message: ChatMessage) -> None:
        """Drop an optimistic bubble that never became a real message."""
        try:
            self._messages.remove(message)
        except ValueError:  # pragma: no cover - defensive
            pass


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

    resend: Optional[Callable[[ChatMessage], Awaitable[ChatMessage]]] = None
    if not conversation.is_channel:

        async def resend(message: ChatMessage) -> ChatMessage:
            assert conversation.contact is not None
            return await ctx.chat.resend_direct(conversation.contact, message)

    paths = await _make_paths_presenter(ctx, conversation, device)
    screen = ChatScreen(
        conversation,
        history,
        send=send,
        names=names,
        session=session,
        resend=resend,
        paths=paths,
    )
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


# --- message paths (the ^P view) -----------------------------------------------------


async def _make_paths_presenter(
    ctx: "AppContext",
    conversation: Conversation,
    device,  # noqa: ANN001 - core Device; typed at the source
) -> Callable[[ChatMessage], Awaitable[None]]:
    """Build the async presenter behind the chat's ^P delivery-paths view.

    Gathers what the presenter needs once per chat open: a hop-name resolver over the
    contacts plus every name the recorder ever overheard (the app-wide rule that a
    nameable node never shows as a bare hash), our own node's name for the white
    ``you``, the routing prefix width for the hash highlights, and — for a channel —
    its secret, read from the device when the conversation didn't carry one (a picker
    conversation knows its identity but not always its key).

    Args:
        ctx: The shared application context.
        conversation: The conversation the chat screen is opening.
        device: The connected device (already awaited by the caller).

    Returns:
        An async callable presenting one message's paths in a floating window.
    """
    from ..services import trace_runner
    from ..services.message_paths import (
        channel_arrivals,
        direct_frames_near,
        distinct_paths,
    )
    from .message_paths_screen import MessagePathsScreen
    from .timemachine_screen import _routing_prefix_bytes

    session = ctx.ui.session
    resolve = trace_runner.make_node_resolver(
        await device.get_contacts(), ctx.repo.node_names()
    )
    prefix_bytes = await _routing_prefix_bytes(ctx)
    self_name: Optional[str] = None
    try:
        self_name = str((await device.get_self_info()).get("name") or "") or None
    except Exception:  # noqa: BLE001 - a nameless self just skips the white highlight
        self_name = None

    secret = conversation.secret
    if conversation.is_channel and not secret and conversation.channel_idx is not None:
        try:
            payload = await device.get_channel(conversation.channel_idx)
            raw = (payload or {}).get("channel_secret")
            secret = bytes(raw) if raw else None
        except Exception:  # noqa: BLE001 - no key, no decrypt; the view says so honestly
            secret = None

    async def present(message: ChatMessage) -> None:
        if conversation.is_channel and secret:
            arrivals = channel_arrivals(
                ctx.repo, message, channel_name=conversation.label, secret=secret
            )
            matched = True
            summary = (
                f"heard {len(arrivals)} time{'s' if len(arrivals) != 1 else ''}"
                f" · {distinct_paths(arrivals)} distinct "
                f"path{'s' if distinct_paths(arrivals) != 1 else ''}"
                if arrivals else "no copies in the packet log"
            )
        elif conversation.is_channel:
            await session.scroll(
                Text(
                    "This channel's key isn't at hand, so overheard frames can't be "
                    "matched to the message.",
                    style="muted",
                ),
                title="Message paths",
            )
            return
        else:
            arrivals = direct_frames_near(ctx.repo, message)
            matched = False
            summary = "direct frames are encrypted — matched by time alone (±90 s)"
        # The graph's left endpoint: who the message set out from. Our own sends are
        # us; an inbound channel message names its sender on the wire; a direct chat's
        # origin is the conversation's peer (billed as time-matched by the summary).
        if message.outbound:
            source = self_name
        elif conversation.is_channel:
            source, _body = _split_channel_sender(message.text)
        else:
            source = conversation.label
        await session.run_screen(
            MessagePathsScreen(
                message, arrivals, matched=matched, resolve=resolve,
                prefix_bytes=prefix_bytes, self_name=self_name, summary=summary,
                source=source or None,
            )
        )

    return present
