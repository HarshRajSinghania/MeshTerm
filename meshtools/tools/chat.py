"""The ``chat`` tool: channel and direct messaging over the mesh.

Interactively it opens a conversation picker and then a live, full-screen chat (see
:mod:`meshtools.ui.chat`) where sent and received messages stream together. On the CLI it
exposes ``send``, ``history``, and ``list`` subcommands for scripted use. Channels are only
*listed* here for picking; creating and editing channel slots lives in the ``channels`` tool.

Every inbound message is recorded to history by the always-on
:class:`~meshtools.services.chat_service.ChatService`, and outbound messages are recorded on
send, so ``history`` reflects the full transcript regardless of which path produced it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import typer
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.connection import Device
from ..core.events import EventKind, MeshEvent
from ..core.models import ChatMessage, Contact, Conversation
from ..ui.tui import Choice, Separator
from .base import Tool, ToolResult, register

#: How many channel slots to probe when listing channels to chat on.
_MAX_CHANNELS = 8

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
            The chosen :class:`~meshtools.core.models.Conversation`, or ``None`` if
            cancelled.
        """
        device = await ctx.device()
        channels = await _read_channels(device)
        contacts = await device.get_contacts()
        lasts = ctx.repo.last_chat_messages()

        items: list = [Separator("── Channels ──")]
        for conversation in channels:
            items.append(Choice(title=_title(ctx, conversation, lasts), value=conversation))

        items.append(Separator("── Direct ──"))
        if contacts:
            for contact in contacts:
                conversation = Conversation(label=contact.name, is_channel=False, contact=contact)
                items.append(Choice(title=_title(ctx, conversation, lasts), value=conversation))
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
        state = "[ok]delivered[/ok]" if message.acked else "[warn]no ack[/warn]"
        ctx.ui.note(f"[ok]✓[/ok] sent to [brand]{contact.name}[/brand] — {state}")
        return ToolResult(summary={"to": contact.name, "acked": bool(message.acked)})

    async def _cli_history(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print a conversation's stored transcript."""
        limit = int(params.get("limit") or _HISTORY_LIMIT)
        channel = params.get("channel")
        to = params.get("to")
        if channel is not None:
            messages = ctx.repo.recent_chat_messages(
                is_channel=True, channel_idx=int(channel), limit=limit
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
    for name in ("meshcore", "meshtools.core.connection"):
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
        One :class:`~meshtools.core.models.Conversation` per configured channel (at least
        channel 0).
    """
    conversations: list[Conversation] = []
    for idx in range(_MAX_CHANNELS):
        try:
            channel = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - firmware may not support channel reads
            break
        if channel:
            name = str(channel.get("channel_name") or idx)
            conversations.append(
                Conversation(label=name, is_channel=True, channel_idx=idx)
            )
    if not any(c.channel_idx == 0 for c in conversations):
        conversations.insert(0, Conversation(label="Public", is_channel=True, channel_idx=0))
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


def _title(ctx: AppContext, conversation: Conversation, lasts: dict) -> str:
    """Build a picker row title: label, an unread marker, and a last-message snippet.

    Choice titles render as plain text, so markup is avoided; the unread count shows as a
    ``●`` badge and the most recent message as a short trailing preview.

    Args:
        ctx: Shared application context (for the unread count).
        conversation: The conversation the row represents.
        lasts: Map of conversation key to its most recent message.

    Returns:
        A single-line plain-text title.
    """
    parts = [conversation.label]
    unread = ctx.chat.unread(conversation.key)
    if unread:
        parts.append(f"  ● {unread}")
    last: Optional[ChatMessage] = lasts.get(conversation.key)
    if last is not None:
        who = "you: " if last.outbound else ""
        parts.append(f"   · {who}{last.text[:32]}")
    return "".join(parts)


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
