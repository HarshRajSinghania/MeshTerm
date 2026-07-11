"""The ``courier`` tool: store-and-forward messaging for nodes that aren't there yet.

Interactively it opens the outbox screen (see :mod:`meshterm.ui.courier_screen`):
queued messages with their live state, the queueing flow (recipient → message →
when), forced sends, and the delivered/given-up history. Delivery runs in the
background for the whole session (see :mod:`meshterm.services.courier`): a queued
message goes out when its node is next heard — or at its scheduled time — as a normal
direct message with acknowledgement tracking and polite exponential backoff, and the
outcome lights the header's Watchtower badge.

On the CLI it stays a scriptable one-shot: ``courier <node> <text…> [--at HH:MM]``
queues the message (reading the contact list to address it — nothing is transmitted)
and the next interactive session's courier delivers it.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from ..core.connection import DeviceCommandError
from .base import Tool, ToolResult, register


@register
class CourierTool(Tool):
    """Queue messages for offline nodes; they go out when the node is next heard."""

    name = "courier"
    title = "Courier"
    icon = "📨"
    help = "Store-and-forward outbox — deliver when the node is next heard"
    category = "Mesh"
    order = 12  # right after Chat: the same conversation, minus the waiting around

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the outbox screen; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.courier_screen import open_courier

        await open_courier(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Queue one message from the CLI (transmits nothing).

        Args:
            ctx: Shared application context.
            params: ``node`` (contact name), ``text`` (the message body), and an
                optional local ``at`` clock time (``HH:MM``, next occurrence).

        Returns:
            A :class:`ToolResult` describing the queued entry.
        """
        from ..ui.courier_screen import parse_clock
        from ..ui.watchtower_screen import contact_watch_key

        device = await ctx.device()
        contacts = await device.get_contacts()
        name = str(params["node"])
        needle = name.casefold()
        contact = next((c for c in contacts if c.name.casefold() == needle), None)
        if contact is None:
            raise DeviceCommandError(f"unknown contact: {name!r}")
        key = contact_watch_key(contact)
        if key is None:
            raise DeviceCommandError(f"{name!r} has no usable key to address")

        not_before = None
        if params.get("at"):
            not_before = parse_clock(str(params["at"]))
            if not_before is None:
                raise DeviceCommandError(
                    f"--at wants a clock time like 07:00, not {params['at']!r}"
                )
        message = ctx.courier_store.queue(
            key, contact.name, str(params["text"]), not_before=not_before
        )
        when = (
            f"at {not_before.astimezone().strftime('%b %d %H:%M')}"
            if not_before is not None
            else "when the node is next heard"
        )
        ctx.ui.note(
            f"[ok]queued[/ok] #{message.ident} for [brand]{contact.name}[/brand] — "
            f"delivers {when} (an interactive session's courier does the sending)"
        )
        return ToolResult(
            summary={"queued": message.ident, "node": contact.name, "at": params.get("at")}
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``courier`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _courier(
            node: str = typer.Argument(..., help="The recipient contact's name"),
            text: list[str] = typer.Argument(..., help="The message to queue"),
            at: Optional[str] = typer.Option(
                None, "--at", help="Hold until this local time (HH:MM, next occurrence)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {"node": node, "text": " ".join(text)}
            if at is not None:
                tool_params["at"] = at
            run_tool_command(self, tool_params)
