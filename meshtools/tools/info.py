"""The ``info`` tool: show the connected companion device's identity and contacts."""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class InfoTool(Tool):
    """Display device identity, radio configuration, and known contacts."""

    name = "info"
    help = "Show device identity, radio config, and contacts."
    category = "Device"
    order = 10

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render its self-info and contacts.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing device name and contact count.
        """
        device = await ctx.device()
        info = await device.get_self_info()
        contacts = await device.get_contacts()

        table = Table(title="Device", border_style="muted", expand=False)
        table.add_column("Field", style="muted")
        table.add_column("Value")
        for key in ("name", "tx_power", "freq", "bw", "sf", "cr"):
            if key in info:
                table.add_row(key, str(info[key]))
        ctx.console.print(table)

        if contacts:
            ctable = Table(title=f"Contacts ({len(contacts)})", border_style="muted")
            ctable.add_column("Name", style="brand")
            ctable.add_column("Key prefix", style="muted")
            for c in contacts:
                ctable.add_row(c.name, c.key_prefix or "[muted]?[/muted]")
            ctx.console.print(ctable)

        return ToolResult(
            summary={"name": info.get("name"), "contacts": len(contacts)},
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``info`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _info() -> None:
            run_tool_command(self, {})
