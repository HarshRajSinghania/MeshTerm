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
from typing import Any, Callable, Optional, Union

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
from ..core.models import ChatMessage, Contact, Conversation
from ..ui.tui import Choice, Separator
from ..ui.widgets import channel_glyph
from .base import Tool, ToolResult, register

#: How many recent messages ``chat history`` prints by default.
_HISTORY_LIMIT = 50


@register
class ChatTool(Tool):
    """Send and receive channel and direct messages, with a live interactive chat."""

    name = "chat"
    help = "Channel and direct messaging — live chat, with stored history."
    category = "Messaging"
    order = 10

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Show the conversation picker and return the chosen thread to open.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"conversation": Conversation}`` for the chosen thread, or ``None`` if the
            user backed out.
        """
        conversation = await self._pick_conversation(ctx)
        if conversation is None:
            return None
        return {"conversation": conversation}

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

        conversation: Optional[Conversation] = params.get("conversation")
        if conversation is None:  # pragma: no cover - prompt_params returns None to cancel
            return ToolResult(summary={})

        from ..ui.chat import open_chat

        # Loop picker ↔ chat so backing out of a conversation (Esc) steps back to the
        # picker rather than all the way to the main menu; Esc from the picker ends the
        # loop and returns to the menu. The just-closed conversation is pre-selected in
        # the picker so the cursor lands where the user left.
        opened = 0
        shown = 0
        while conversation is not None:
            shown = await open_chat(ctx, conversation)
            opened += 1
            conversation = await self._pick_conversation(ctx, default_key=conversation.key)
        return ToolResult(summary={"conversations": opened, "messages": shown})

    # -- interactive picker -----------------------------------------------------

    async def _pick_conversation(
        self, ctx: AppContext, *, default_key: Optional[str] = None
    ) -> Optional[Conversation]:
        """Let the user pick a channel or contact to chat with.

        Args:
            ctx: Shared application context.
            default_key: Conversation key to pre-highlight (e.g. the one just backed out
                of), so the cursor lands there rather than at the top.

        Returns:
            The chosen :class:`~meshterm.core.models.Conversation`, or ``None`` if
            cancelled.
        """
        device = await ctx.device()
        channels = await _read_channels(device)
        contacts = await device.get_contacts()
        # A stable snapshot orders the rows (so the list doesn't reshuffle under the cursor),
        # while a self-refreshing view feeds each row's live preview (see _LiveLasts).
        lasts = ctx.repo.last_chat_messages()
        live = _LiveLasts(ctx, seed=lasts)

        items: list = [Separator("── Channels ──")]
        for conversation in channels:
            items.append(Choice(title=_row_title(ctx, conversation, live), value=conversation))

        items.append(Separator("── Direct ──"))
        if contacts:
            # List contacts by recency — those with messages first, newest exchange at the
            # top — then the never-contacted ones alphabetically (see _recency_key).
            direct = [
                Conversation(label=c.name, is_channel=False, contact=c) for c in contacts
            ]
            direct.sort(key=lambda conv: _recency_key(conv, lasts))
            for conversation in direct:
                items.append(Choice(title=_row_title(ctx, conversation, live), value=conversation))
        else:
            items.append(Separator("  (no contacts yet — receive an advert first)"))

        items.append(Separator(" "))
        items.append(Choice(title="Back", value="__back__"))

        default = next(
            (
                it.value
                for it in items
                if isinstance(it, Choice)
                and isinstance(it.value, Conversation)
                and it.value.key == default_key
            ),
            None,
        )
        choice = await ctx.ui.select(
            "Chat — pick a conversation", items, default=default
        )
        if choice in (None, "__back__"):
            return None
        return choice

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
        state = "[ok]✅ delivered[/ok]" if message.acked else "[warn]❌ no ack[/warn]"
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
            message=f"[muted]stopped — saw {seen} message(s)[/muted]",
        )

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """List channels, contacts, and their most recent message."""
        device = await ctx.device()
        channels = await _read_channels(device)
        contacts = await device.get_contacts()
        lasts = ctx.repo.last_chat_messages()

        table = Table(title="Conversations", border_style="muted", expand=False)
        table.add_column("Conversation")
        table.add_column("Unread", justify="right")
        table.add_column("Last message")
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

        @chat_app.command("send", help="Send a message to a contact or channel.")
        def _send_cmd(
            text: str = typer.Argument(..., help="The message body."),
            to: Optional[str] = typer.Option(None, "--to", help="Contact name or key prefix."),
            channel: Optional[int] = typer.Option(None, "--channel", help="Channel slot index."),
        ) -> None:
            if (to is None) == (channel is None):
                raise typer.BadParameter("Pass exactly one of --to / --channel.")
            run_tool_command(
                self, {"cli_action": "send", "to": to, "channel": channel, "text": text}
            )

        @chat_app.command("history", help="Show a conversation's stored history.")
        def _history_cmd(
            to: Optional[str] = typer.Option(None, "--to", help="Contact name or key prefix."),
            channel: Optional[int] = typer.Option(None, "--channel", help="Channel slot index."),
            limit: int = typer.Option(_HISTORY_LIMIT, "--limit", help="Max messages to show."),
        ) -> None:
            if (to is None) == (channel is None):
                raise typer.BadParameter("Pass exactly one of --to / --channel.")
            run_tool_command(
                self,
                {"cli_action": "history", "to": to, "channel": channel, "limit": limit},
            )

        @chat_app.command("list", help="List channels, contacts, and recent messages.")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @chat_app.command("listen", help="Tail inbound messages live in the console.")
        def _listen_cmd(
            seconds: int = typer.Option(
                0, "--seconds", "-s", help="How long to listen (0 = until Ctrl-C)."
            ),
            debug: bool = typer.Option(
                False, "--debug", help="Log the message-pull activity (diagnostic)."
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
    last: Optional[ChatMessage] = lasts.get(conversation.key)
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

    def get(self, key: str) -> Optional[ChatMessage]:
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
#: Width of the unread-badge lane between the label and the preview (fits ``● 999``).
_BADGE_WIDTH = 5
#: Longest message preview shown before it is ellipsized.
_PREVIEW_WIDTH = 42


def _row_title(
    ctx: AppContext, conversation: Conversation, lasts: "_LiveLasts"
) -> Callable[[], Union[str, Text]]:
    """Return a picker-row title *callable* the select screen re-renders on each repaint.

    Both the unread badge and the last-message preview are read live, so a message arriving
    while the picker sits open updates that row's ``●`` count *and* its sender/text preview on
    the next repaint (the session already repaints ~1×/s for the header).

    Args:
        ctx: Shared application context (for the live unread count).
        conversation: The conversation the row represents.
        lasts: The self-refreshing latest-message view feeding the preview.

    Returns:
        A zero-argument callable producing the current row title.
    """
    return lambda: _title(ctx, conversation, lasts)


def _title(
    ctx: AppContext, conversation: Conversation, lasts: "_LiveLasts"
) -> Union[str, Text]:
    """Build a picker row as fixed-width columns: label, unread badge, message preview.

    Alignment carries the readability: the label is padded to a fixed lane, the unread ``●``
    badge sits in its own lane, and the preview lines up across every row regardless of how
    long the names or counts are. When there is unread, the row is returned as a Rich
    :class:`~rich.text.Text` so the ``●`` glyph alone can be tinted red; otherwise a plain
    string suffices.

    Args:
        ctx: Shared application context (for the live unread count).
        conversation: The conversation the row represents.
        lasts: The self-refreshing latest-message view.

    Returns:
        The row title — a plain ``str``, or a ``Text`` with a red ``●`` when unread.
    """
    unread = ctx.chat.unread(conversation.key)
    badge = f"● {unread}" if unread else ""
    last = lasts.get(conversation.key)
    preview = _preview(last) if last is not None else ""
    # A channel leads with its openness marker (＃ / 🌐 / 🔒); a direct chat with a person glyph
    # — 💬 once we've exchanged messages, 👤 for a contact we haven't talked to yet. Every glyph
    # is one double-width cell, so the label lane still lines up across rows.
    if conversation.is_channel:
        glyph = channel_glyph(conversation.label, conversation.secret)
    else:
        glyph = "💬" if last is not None else "👤"
    line = f"{glyph} {_fit(conversation.label, _LABEL_WIDTH)}  {badge:<{_BADGE_WIDTH}}  {preview}".rstrip()
    if not unread:
        return line
    text = Text(line)
    dot = line.index("●")  # only the badge carries this glyph; colour just it red
    text.stylize("err", dot, dot + 1)
    return text


def _preview(last: ChatMessage, *, width: int = _PREVIEW_WIDTH) -> str:
    """One-line preview of the latest message, ``you:``-prefixed when we sent it.

    Only our own outbound messages get an author prefix. Inbound channel messages already
    carry the sender's node name inline in their text (the firmware embeds it), and an inbound
    direct message's author is the row's own label — so neither needs one added here.

    Args:
        last: The most recent message in the conversation.
        width: The column budget before the preview is ellipsized.

    Returns:
        A single-line, length-bounded preview string.
    """
    who = "you: " if last.outbound else ""
    body = f"{who}{last.text}".replace("\n", " ")
    return body[: width - 1] + "…" if len(body) > width else body


def _fit(text: str, width: int) -> str:
    """Left-justify ``text`` to ``width`` columns, ellipsizing anything that would overflow."""
    if len(text) > width:
        return text[: width - 1] + "…"
    return text.ljust(width)


def _history_table(label: str, messages: list[ChatMessage]) -> Table:
    """Render a conversation's stored messages as a table.

    Args:
        label: The conversation's display name.
        messages: The messages to show, oldest-first.

    Returns:
        A Rich :class:`Table` of time, sender, and text.
    """
    table = Table(title=f"History — {label}", border_style="muted", expand=False)
    table.add_column("Time")
    table.add_column("From")
    table.add_column("Message")
    for message in messages:
        stamp = message.created_at.astimezone().strftime("%m-%d %H:%M")
        if message.outbound:
            who = Text("you", style="accent")
        else:
            who = Text(message.peer_name or message.peer or "?", style="brand")
        table.add_row(stamp, who, message.text)
    return table
