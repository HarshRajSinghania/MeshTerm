"""The ``chat`` tool: channel and direct messaging over the mesh.

Interactively it opens a conversation picker and then a live, full-screen chat (see
:mod:`meshterm.ui.chat`) where sent and received messages stream together. On the CLI it
exposes ``send``, ``history``, and ``list`` subcommands for scripted use. Channels are only
*listed* here for picking; creating and editing channel slots lives in the ``channels`` tool.

Every inbound message is recorded to history by the always-on
:class:`~meshterm.services.chat_service.ChatService`, and outbound messages are recorded on
send, so ``history`` reflects the full transcript regardless of which path produced it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import typer
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.channels import (
    CHANNEL_SLOT_PROBE_CAP,
    DEFAULT_PUBLIC_SECRET,
    channel_identity,
)
from ..core.connection import Device
from ..core.events import EventKind, MeshEvent
from ..core.models import (
    NODE_TYPE_CHAT,
    ChatMessage,
    Contact,
    Conversation,
    is_direct_messageable,
)
from ..services.trace_runner import NameKeyResolver, make_name_key_resolver
from ..ui.chat import _MENTION, _split_channel_sender
from ..ui.menus import Lane, column_header, fit_cells, section_heading
from ..ui.theme import name_style
from ..ui.tui import Choice, DeleteRequest, Separator
from ..ui.widgets import _NODE_GLYPHS, _age_seconds, _format_age, channel_glyph
from .base import Tool, ToolResult, register

if TYPE_CHECKING:
    from ..ui.channels import ChannelSlot

#: How many recent messages ``chat history`` prints by default.
_HISTORY_LIMIT = 50


@register
class ChatTool(Tool):
    """Send and receive channel and direct messages, with a live interactive chat."""

    name = "chat"
    title = "Chat"
    icon = "💬"
    help = "Channel and direct messaging, with history"
    category = "Message"
    order = 10

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Nothing to gather here — the conversation picker lives inside :meth:`run`.

        The picker has to *stay pushed* while a chat runs, so backing out of a thread lands
        on the very list it was opened from — same cursor, same typed filter. A prompt
        gathered here would resolve, and pop, before the tool ran.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"live": True}`` — the menu's marker for the interactive path.
        """
        return {"live": True}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open a live chat (menu) or perform a scripted messaging action (CLI).

        Args:
            ctx: Shared application context.
            params: Either ``conversation`` (menu) or a ``cli_action`` with its arguments.

        Returns:
            A :class:`ToolResult` summarizing what happened.
        """
        action = params.get("cli_action")
        if action is not None:
            return await self._run_cli(ctx, action, params)
        return await self._run_live(ctx)

    # -- interactive picker -----------------------------------------------------

    async def _run_live(self, ctx: AppContext) -> ToolResult:
        """Keep the conversation picker pushed and open chats above it until it is left.

        One screen for the whole visit, so backing out of a thread lands on the row it was
        opened from with the typed filter still narrowing the list. The picker used to be
        rebuilt from scratch each round and the cursor put back by a ``default=`` restore,
        which recovers the cursor alone — and only while the row it names still exists.

        The rows *are* data: an exchange moves its thread up the recency order, and deleting
        a history hollows its dot and demotes the row to the alphabetical tail. So they are
        re-read and swapped in place after each chat and each delete, which follows the
        highlighted thread wherever it moved to.

        Args:
            ctx: Shared application context.

        Returns:
            A :class:`ToolResult` counting the conversations opened and the messages the
            last one showed.
        """
        from ..ui.chat import open_chat
        from ..ui.tui import SelectScreen
        from ..ui.tui.screen import CANCEL

        picker = SelectScreen(
            "Chat — pick a conversation",
            await self._picker_items(ctx),
            delete_hint="Del delete history",
        )
        opened = 0
        shown = 0
        async with ctx.ui.session.stay(picker) as visit:
            while True:
                choice = await visit.result()
                if isinstance(choice, DeleteRequest):
                    await self._delete_history(ctx, choice.value)
                elif choice is CANCEL or choice is None:  # Esc — out to the menu
                    return ToolResult(
                        summary={"conversations": opened, "messages": shown}
                    )
                else:
                    shown = await open_chat(ctx, choice)
                    opened += 1
                picker.replace_items(await self._picker_items(ctx))

    async def _picker_items(self, ctx: AppContext) -> list:
        """Build the picker's rows: the pinned lane names, the Channels group, then Direct.

        Read fresh every time the list is built or swapped, so a thread that just gained
        messages sits where its recency puts it. Cheap enough to redo after each chat: the
        two device reads go through the session cache and the rest is stored history.

        Args:
            ctx: Shared application context.

        Returns:
            The :class:`~meshterm.ui.tui.select.Choice` / ``Separator`` rows, in display
            order.
        """
        # Through the session cache: this picker runs on every Chat open, and its two
        # reads — the channel-slot probe and the contacts table — are the two slowest
        # round-trips on a companion. Reading them from the device each time is what made
        # opening Chat stall for seconds (the cached chat screen behind it never got the
        # chance to help). The cache holds channels until the channel editor writes a
        # slot and refreshes contacts in the background (see
        # :class:`~meshterm.services.device_state.DeviceState`).
        channels = _channels_from_slots(await ctx.devstate.channel_slots())
        contacts = await ctx.devstate.contacts()
        # Only companion nodes are listed — we don't DM repeaters, rooms, or sensors;
        # a contact whose type was never advertised gets the benefit of the doubt (the
        # app-wide DM rule, see is_direct_messageable).
        companions = [c for c in contacts if is_direct_messageable(c.node_type)]
        # A stable snapshot orders the rows (so the list doesn't reshuffle under the
        # cursor), while a self-refreshing view feeds each row's live preview
        # (see _LiveLasts).
        lasts = ctx.repo.last_chat_messages()
        live = _LiveLasts(ctx, seed=lasts)
        # Names in previews/mentions resolve back to keys for their hue (the app-wide
        # colour rule); a name no contact or stored advert carries stays muted.
        key_of = make_name_key_resolver(contacts, ctx.repo.node_names())

        # The lane names pin for the whole picker (they mean the same in both groups),
        # so scrolling into Direct keeps them overhead with that group's heading under
        # them, instead of the header vanishing one row in — see Screen.sticky_rows.
        items: list = [
            Separator(_picker_header, pinned=True),
            section_heading("📡 Channels"),
        ]
        for conversation in channels:
            items.append(
                Choice(
                    title=_row_title(ctx, conversation, live, key_of),
                    value=conversation,
                )
            )

        items.append(section_heading("👤 Direct"))
        if companions:
            # List contacts by recency — those with messages first, newest exchange at
            # the top — then the never-contacted ones alphabetically (see _recency_key).
            direct = [
                Conversation(label=c.name, is_channel=False, contact=c)
                for c in companions
            ]
            direct.sort(key=lambda conv: _recency_key(conv, lasts))
            for conversation in direct:
                items.append(
                    Choice(
                        title=_row_title(ctx, conversation, live, key_of),
                        value=conversation,
                        # Del offers to delete this thread's stored history —
                        # only where there is history to delete.
                        deletable=lasts.get(conversation.key) is not None,
                    )
                )
        else:
            items.append(Separator("  no contacts yet — receive an advert first"))

        return items

    @staticmethod
    async def _delete_history(ctx: AppContext, conversation: Conversation) -> None:
        """Confirm and delete one direct conversation's stored history.

        Deleting history is irreversible data loss, so the confirm wears the reserved
        red (``destructive``): Cancel on the left, the committing Delete on the right.
        On confirm the peer's messages are removed from the database and the thread's
        unread count is cleared; the contact itself (a device-side record) is untouched.
        """
        if not await ctx.ui.dialog(
            f"Delete the chat history with {conversation.label}? Every stored "
            "message in this conversation is removed.",
            [("Cancel", False), ("Delete", True)],
            title="Delete history",
            default=1,
            destructive=True,
        ):
            return
        ctx.repo.delete_chat_history(conversation.peer)
        ctx.chat.clear_unread(conversation.key)

    # -- CLI --------------------------------------------------------------------

    async def _run_cli(
        self, ctx: AppContext, action: str, params: dict[str, Any]
    ) -> ToolResult:
        """Dispatch a scripted CLI action.

        Args:
            ctx: Shared application context.
            action: One of ``send``, ``history``, ``list``.
            params: The action's arguments.

        Returns:
            A :class:`ToolResult` for the action.
        """
        if action == "send":
            return await self._cli_send(ctx, params)
        if action == "history":
            return await self._cli_history(ctx, params)
        if action == "listen":
            return await self._cli_listen(ctx, params)
        return await self._cli_list(ctx)

    async def _cli_send(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Send a channel or direct message and report the outcome."""
        device = await ctx.device()
        text = str(params["text"])
        channel = params.get("channel")
        if channel is not None:
            await ctx.chat.send_channel(int(channel), text, label=f"#{channel}")
            ctx.ui.note(f"[ok]✓[/ok] sent to channel [brand]{channel}[/brand]")
            return ToolResult(summary={"channel": channel, "sent": True})

        contact = _resolve_contact(await device.get_contacts(), str(params["to"]))
        message = await ctx.chat.send_direct(contact, text)
        state = "[ok]✓ delivered[/ok]" if message.acked else "[warn]? no ack[/warn]"
        ctx.ui.note(f"[ok]✓[/ok] sent to [brand]{contact.name}[/brand] — {state}")
        return ToolResult(summary={"to": contact.name, "acked": bool(message.acked)})

    async def _cli_history(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print a conversation's stored transcript."""
        limit = int(params.get("limit") or _HISTORY_LIMIT)
        channel = params.get("channel")
        to = params.get("to")
        if channel is not None:
            channel_id = await ctx.chat.channel_id_for(int(channel))
            messages = ctx.repo.recent_chat_messages(
                is_channel=True, channel_id=channel_id, limit=limit
            )
            label = f"#{channel}"
        else:
            device = await ctx.device()
            contact = _resolve_contact(await device.get_contacts(), str(to))
            messages = ctx.repo.recent_chat_messages(
                is_channel=False,
                peer=contact.key_prefix or contact.public_key[:12],
                limit=limit,
            )
            label = contact.name

        if not messages:
            ctx.ui.note(f"[muted]no messages with {label} yet[/muted]")
        else:
            ctx.ui.show(_history_table(label, messages))
        return ToolResult(summary={"conversation": label, "messages": len(messages)})

    async def _cli_listen(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Tail inbound messages live in the console (and record them to history).

        A plain-console receiver, outside the full-screen menu: it starts the always-on
        listener and prints each message as it arrives. Doubles as a diagnostic — with the
        pump's debug logging enabled (``--debug`` on the CLI) it shows whether messages are
        being pulled from the companion at all.

        Args:
            ctx: Shared application context.
            params: ``seconds`` — how long to listen (``0``/``None`` = until interrupted).

        Returns:
            A :class:`ToolResult` with how many messages were seen.
        """
        seconds = params.get("seconds") or 0
        await ctx.device()
        await ctx.chat.start()  # begins recording inbound to history too
        ctx.console.print(
            "[muted]listening for messages"
            f"{f' for {seconds}s' if seconds else ' — press Ctrl-C to stop'}…[/muted]"
        )
        seen = 0

        def on_message(event: MeshEvent) -> None:
            nonlocal seen
            message = event.message
            if message is None:
                return
            seen += 1
            stamp = message.received_at.astimezone().strftime("%H:%M:%S")
            if message.is_channel:
                who = f"#{message.channel}"
            else:
                who = message.sender or "?"
            snr = f" [muted]({message.snr:+.0f} dB)[/muted]" if message.snr is not None else ""
            ctx.console.print(f"[muted]{stamp}[/muted] [accent]{who}[/accent]: {message.text}{snr}")

        unsubscribe = ctx.events.subscribe(on_message, EventKind.MESSAGE)
        try:
            if seconds:
                await asyncio.sleep(seconds)
            else:
                await asyncio.Event().wait()  # until Ctrl-C / cancellation
        except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
            pass
        finally:
            unsubscribe()
        return ToolResult(
            summary={"messages": seen},
            message=f"[muted]stopped — heard {seen} message{'' if seen == 1 else 's'}[/muted]",
        )

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """List channels, contacts, and their most recent message."""
        device = await ctx.device()
        channels = await _read_channels(device)
        contacts = await device.get_contacts()
        lasts = ctx.repo.last_chat_messages()

        table = Table(title="Conversations", border_style="muted", expand=False)
        table.add_column("CONVERSATION")
        table.add_column("UNREAD", justify="right")
        table.add_column("LAST MESSAGE")
        rows = [*channels] + [
            Conversation(label=c.name, is_channel=False, contact=c) for c in contacts
        ]
        for conversation in rows:
            last = lasts.get(conversation.key)
            snippet = ""
            if last is not None:
                who = "you: " if last.outbound else ""
                snippet = f"{who}{last.text[:40]}"
            unread = ctx.chat.unread(conversation.key)
            table.add_row(conversation.label, str(unread) if unread else "·", snippet)
        ctx.ui.show(table)
        return ToolResult(summary={"conversations": len(rows)})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``chat`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        chat_app = typer.Typer(help=self.help, no_args_is_help=True, rich_markup_mode="rich")

        @chat_app.command("send", help="Send a message to a contact or channel")
        def _send_cmd(
            text: str = typer.Argument(..., help="The message body"),
            to: str | None = typer.Option(None, "--to", help="Contact name or key prefix"),
            channel: int | None = typer.Option(None, "--channel", help="Channel slot index"),
        ) -> None:
            if (to is None) == (channel is None):
                raise typer.BadParameter("Pass exactly one of --to / --channel.")
            run_tool_command(
                self, {"cli_action": "send", "to": to, "channel": channel, "text": text}
            )

        @chat_app.command("history", help="Show a conversation's stored history")
        def _history_cmd(
            to: str | None = typer.Option(None, "--to", help="Contact name or key prefix"),
            channel: int | None = typer.Option(None, "--channel", help="Channel slot index"),
            limit: int = typer.Option(_HISTORY_LIMIT, "--limit", help="Max messages to show"),
        ) -> None:
            if (to is None) == (channel is None):
                raise typer.BadParameter("Pass exactly one of --to / --channel.")
            run_tool_command(
                self,
                {"cli_action": "history", "to": to, "channel": channel, "limit": limit},
            )

        @chat_app.command("list", help="List channels, contacts, and recent messages")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @chat_app.command("listen", help="Tail inbound messages live in the console")
        def _listen_cmd(
            seconds: int = typer.Option(
                0, "--seconds", "-s", help="How long to listen (0 = until Ctrl-C)"
            ),
            debug: bool = typer.Option(
                False, "--debug", help="Log the message-pull activity (diagnostic)"
            ),
        ) -> None:
            if debug:
                _enable_receive_debug()
            run_tool_command(self, {"cli_action": "listen", "seconds": seconds})

        app.add_typer(chat_app, name=self.name)


# -- helpers ------------------------------------------------------------------


def _enable_receive_debug() -> None:
    """Route the message-pull and library debug logs to the console for diagnosis.

    Raises the ``meshcore`` and connection-layer loggers to DEBUG and attaches a stderr
    handler, so ``chat listen --debug`` shows whether inbound messages are being pulled
    from the companion (the receive path) at all.
    """
    import logging
    import sys

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    for name in ("meshcore", "meshterm.core.connection"):
        log = logging.getLogger(name)
        log.setLevel(logging.DEBUG)
        log.addHandler(handler)


def _channels_from_slots(slots: list[ChannelSlot]) -> list[Conversation]:
    """Turn cached channel slots into channel conversations, always offering Public (slot 0).

    The picker reads its channels through the session cache
    (:meth:`~meshterm.services.device_state.DeviceState.channel_slots`) so it reuses the one
    slow slot probe instead of re-walking every slot on each Chat open; this maps that cached
    :class:`~meshterm.ui.channels.ChannelSlot` list onto the picker's
    :class:`~meshterm.core.models.Conversation` rows. Channel 0 (the default public channel) is
    synthesised when the firmware reports no slot for it, so there is always somewhere to chat —
    the same guarantee :func:`_read_channels` (the CLI path) makes.

    Args:
        slots: The configured channel slots, from the session cache.

    Returns:
        One :class:`~meshterm.core.models.Conversation` per slot, with Public prepended when
        slot 0 is absent.
    """
    conversations = [
        Conversation(
            label=slot.name,
            is_channel=True,
            channel_idx=slot.idx,
            channel_id=channel_identity(slot.name, slot.secret),
            secret=slot.secret,
        )
        for slot in slots
    ]
    if not any(c.channel_idx == 0 for c in conversations):
        conversations.insert(
            0,
            Conversation(
                label="Public",
                is_channel=True,
                channel_idx=0,
                channel_id="slot:0",
                secret=DEFAULT_PUBLIC_SECRET,
            ),
        )
    return conversations


async def _read_channels(device: Device) -> list[Conversation]:
    """Probe channel slots and return them as channel conversations.

    Channel 0 (the default public channel) is always offered even when the firmware reports
    no configured slots, so there is always somewhere to chat.

    Args:
        device: The connected device to probe.

    Returns:
        One :class:`~meshterm.core.models.Conversation` per configured channel (at least
        channel 0).
    """
    conversations: list[Conversation] = []
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            channel = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - firmware may not support channel reads
            break
        if channel:
            name = str(channel.get("channel_name") or idx)
            secret = bytes(channel.get("channel_secret") or b"\x00" * 16)
            conversations.append(
                Conversation(
                    label=name,
                    is_channel=True,
                    channel_idx=idx,
                    channel_id=channel_identity(name, secret),
                    secret=secret,
                )
            )
    if not any(c.channel_idx == 0 for c in conversations):
        conversations.insert(
            0,
            Conversation(
                label="Public",
                is_channel=True,
                channel_idx=0,
                channel_id="slot:0",
                secret=DEFAULT_PUBLIC_SECRET,
            ),
        )
    return conversations


def _resolve_contact(contacts: list[Contact], needle: str) -> Contact:
    """Find a contact by exact name (case-insensitive) or key-prefix match.

    Args:
        contacts: The known contacts.
        needle: A contact name or public-key prefix.

    Returns:
        The matching contact.

    Raises:
        typer.BadParameter: If no contact matches ``needle``.
    """
    folded = needle.casefold()
    for contact in contacts:
        pub = (contact.public_key or "").lower().removeprefix("0x")
        if contact.name.casefold() == folded or pub.startswith(folded):
            return contact
    raise typer.BadParameter(f"no contact matches {needle!r}")


def _recency_key(conversation: Conversation, lasts: dict) -> tuple:
    """Sort key ordering conversations by recency, then name.

    Conversations that have been chatted with sort first, most-recent exchange at the top;
    those never chatted with sort after them, alphabetically by label. The leading ``0``/``1``
    keeps the two groups apart so their differently-typed tie-breakers never compare.

    Args:
        conversation: The conversation to rank.
        lasts: Map of conversation key to its most recent message.

    Returns:
        A tuple usable as a ``sorted`` key.
    """
    last: ChatMessage | None = lasts.get(conversation.key)
    if last is not None:
        return (0, -last.created_at.timestamp())
    return (1, conversation.label.casefold())


class _LiveLasts:
    """A self-refreshing view of each conversation's most recent message.

    The picker snapshots :meth:`~meshterm.persistence.repository.Repository.last_chat_messages`
    once to *order* the rows (so the list never reshuffles under the cursor), but the row
    previews read through this so a message arriving while the list is open updates the
    sender/text preview on the next repaint. It re-queries the repository at most a few times a
    second (bounded by ``ttl``) rather than once per row per repaint, so a wide list stays cheap.
    """

    def __init__(self, ctx: AppContext, *, seed: dict, ttl: float = 0.5) -> None:
        """Bind to a context, seeding the cache with the snapshot already loaded at open."""
        self._ctx = ctx
        self._ttl = ttl
        self._cache = seed
        self._at = time.monotonic()

    def get(self, key: str) -> ChatMessage | None:
        """Return the latest message for ``key``, refreshing the cache once its TTL lapses."""
        now = time.monotonic()
        if now - self._at >= self._ttl:
            try:
                self._cache = self._ctx.repo.last_chat_messages()
            except Exception:  # noqa: BLE001 - keep the last good snapshot on a read error
                pass
            self._at = now
        return self._cache.get(key)


#: Column width (display cells) the conversation label is padded/ellipsized to, so the unread
#: badge and message preview line up in fixed lanes down the picker.
_LABEL_WIDTH = 22
#: Width of the unread-badge lane between the label and the age (fits ``● 999``).
_BADGE_WIDTH = 5
#: Width of the relative-age lane between the badge and the preview (right-aligned; fits ``now``
#: and two-digit spans like ``59m`` / ``23h``), so every row's message text starts in one column.
_AGE_WIDTH = 3
#: Longest message preview shown before it is ellipsized.
_PREVIEW_WIDTH = 40


def _picker_header(width: int) -> str:
    """Column headers over the picker's fixed lanes (see :func:`_title` for the layout).

    The five-cell indent covers the select screen's pointer column (2 cells, drawn on choice
    rows but not separators) plus the marker lane (3 cells), so each header lands exactly
    over its column. UNREAD borrows its lane's trailing gap — the badge lane itself is one
    cell too narrow for the word — which still leaves a space before the age column.

    Resolved against the render width (the header row is pinned, so it must stay one row):
    on a terminal too narrow for the whole line, ``LAST MESSAGE`` gives its cells back a
    word at a time rather than the line wrapping or losing the label (see
    :func:`~meshterm.ui.menus.column_header`).
    """
    return column_header(
        [
            Lane("CONVERSATION", _LABEL_WIDTH + 2),
            Lane("UNREAD", _BADGE_WIDTH + 2),
            Lane("AGE", _AGE_WIDTH + 2),
            Lane(("LAST MESSAGE", "LAST MSG", "LAST")),
        ],
        width,
        indent=5,
    )


def _row_title(
    ctx: AppContext,
    conversation: Conversation,
    lasts: _LiveLasts,
    key_of: NameKeyResolver,
) -> Callable[[], str | Text]:
    """Return a picker-row title *callable* the select screen re-renders on each repaint.

    Both the unread badge and the last-message preview are read live, so a message arriving
    while the picker sits open updates that row's ``●`` count *and* its sender/text preview on
    the next repaint (the session already repaints ~1×/s for the header).

    Args:
        ctx: Shared application context (for the live unread count).
        conversation: The conversation the row represents.
        lasts: The self-refreshing latest-message view feeding the preview.
        key_of: Maps a sender name back to its node's key, for the preview hues.

    Returns:
        A zero-argument callable producing the current row title.
    """
    return lambda: _title(ctx, conversation, lasts, key_of)


def _title(
    ctx: AppContext,
    conversation: Conversation,
    lasts: _LiveLasts,
    key_of: NameKeyResolver,
) -> str | Text:
    """Build a picker row as fixed-width, colour-coded lanes.

    Alignment carries the readability — marker, label, unread badge, relative age, and preview
    each sit in their own lane, so every row's message text starts in the same column. Colour is
    purposeful: a direct contact's name takes its key-derived palette hue (a channel label stays
    base), the leading dot is the standard companion pink with its shape marking history, the
    unread ``●`` badge is red, the age is muted, and the preview mutes its body while lighting
    sender names and ``@mentions`` in their key-derived hue — the same colours the live
    transcript uses. The row is always a Rich :class:`~rich.text.Text` so those spans survive
    under the select screen's row highlight.

    Args:
        ctx: Shared application context (for the live unread count).
        conversation: The conversation the row represents.
        lasts: The self-refreshing latest-message view.
        key_of: Maps a preview sender/mention name back to its node's key.

    Returns:
        The row title as a styled :class:`~rich.text.Text`.
    """
    unread = ctx.chat.unread(conversation.key)
    last = lasts.get(conversation.key)
    text = Text(no_wrap=True, overflow="ellipsis")
    _append_marker(text, conversation, last)
    label_style = ""
    if not conversation.is_channel and conversation.contact is not None:
        contact = conversation.contact
        label_style = name_style(
            conversation.label, contact.public_key or contact.key_prefix
        )
    text.append(fit_cells(conversation.label, _LABEL_WIDTH), style=label_style or None)
    text.append("  ")
    # Unread badge lane (_BADGE_WIDTH cells): a red ● with the count in warn, or blank filler so
    # the following lanes still line up on rows with nothing unread.
    if unread:
        text.append("●", style="err")
        text.append(f" {unread}".ljust(_BADGE_WIDTH - 1), style="warn")
    else:
        text.append(" " * _BADGE_WIDTH)
    # Relative-age lane (right-aligned) sits between the badge and the message text, so the ages
    # stack in one tidy column and the previews all start at the same place.
    age = _ago(last.created_at) if last is not None else ""
    text.append("  ")
    text.append(f"{age:>{_AGE_WIDTH}}", style="muted")
    text.append("  ")
    if last is not None:
        text.append_text(_preview_text(last, key_of))
    return text


#: The standard companion pink — the shared plain-node ``●`` colour — the contact dot's
#: hue: the shape (filled/hollow) marks history, the colour marks "a companion", and the
#: name beside it carries the person's own key-derived hue.
_COMPANION_DOT_STYLE = _NODE_GLYPHS[NODE_TYPE_CHAT][1]


def _append_marker(text: Text, conversation: Conversation, last: ChatMessage | None) -> None:
    """Prepend the row's leading marker (3 display cells) — a channel glyph or a contact dot.

    A channel keeps its openness marker (＃ / 🌐 / 🔒). A contact gets a small circle in the
    standard companion pink — filled (``●``) once we've exchanged messages, a hollow ring
    (``○``) before any — so the hollow-vs-filled shape marks whether there's history while the
    name itself carries the person's key-derived hue. The contact dot is padded to the same
    width as a channel's double-cell glyph so the labels line up across both sections.
    """
    if conversation.is_channel:
        text.append(f"{channel_glyph(conversation.label, conversation.secret)} ")
    else:
        dot = "●" if last is not None else "○"
        text.append(dot, style=_COMPANION_DOT_STYLE)
        text.append("  ")


def _preview_text(last: ChatMessage, key_of: NameKeyResolver) -> Text:
    """A muted last-message preview with sender names and ``@mentions`` lit in their hue.

    Mirrors the live transcript: our own messages get a ``you:`` prefix, an inbound channel
    message's inline ``Name:`` sender is coloured in its key-derived hue (muted when no known
    node carries the name), and every ``@[Name]`` mention reads as a bare ``@Name`` the same
    way — so the list and the chat speak the same colour language. The result is clipped to
    :data:`_PREVIEW_WIDTH` cells.
    """
    body_raw = last.text.replace("\n", " ")
    text = Text()
    if last.outbound:
        text.append("you: ", style="accent")
        _append_body(text, body_raw, key_of)
    elif last.is_channel:
        name, body = _split_channel_sender(body_raw)
        if name is not None:
            text.append(name, style=name_style(name, key_of(name)))
            text.append(": ", style="muted")
            _append_body(text, body, key_of)
        else:
            _append_body(text, body_raw, key_of)
    else:
        _append_body(text, body_raw, key_of)
    text.truncate(_PREVIEW_WIDTH, overflow="ellipsis")
    return text


def _append_body(text: Text, body: str, key_of: NameKeyResolver) -> None:
    """Append ``body`` to ``text``, muted, with each ``@[Name]`` mention drawn in the
    mentioned node's key-derived hue (muted when the name resolves to no known node).
    """
    pos = 0
    for match in _MENTION.finditer(body):
        if match.start() > pos:
            text.append(body[pos : match.start()], style="muted")
        name = match.group(1)
        text.append(f"@{name}", style=name_style(name, key_of(name)))
        pos = match.end()
    if pos < len(body):
        text.append(body[pos:], style="muted")


def _ago(when: Any) -> str:
    """The column age for a message time, through THE grammar (``_format_age``).

    One deliberate difference from the widget: a missing or naive timestamp (a stray one
    from the wire) reads as a *blank* lane rather than ``never`` — the picker wants an
    empty cell there, not a word.
    """
    if getattr(when, "tzinfo", None) is None:
        return ""
    return _format_age(_age_seconds(when))


def _history_table(label: str, messages: list[ChatMessage]) -> Table:
    """Render a conversation's stored messages as a table.

    Args:
        label: The conversation's display name.
        messages: The messages to show, oldest-first.

    Returns:
        A Rich :class:`Table` of time, sender, and text.
    """
    table = Table(title=f"History — {label}", border_style="muted", expand=False)
    table.add_column("TIME")
    table.add_column("FROM")
    table.add_column("MESSAGE")
    for message in messages:
        stamp = message.created_at.astimezone().strftime("%m-%d %H:%M")
        if message.outbound:
            who = Text("you", style="accent")
        else:
            who = Text(message.peer_name or message.peer or "?", style="brand")
        table.add_row(stamp, who, message.text)
    return table
