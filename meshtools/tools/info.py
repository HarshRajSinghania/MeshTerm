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
        """Query the device and render its full configuration and contacts.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing device name and contact count.
        """
        from ..core.device_config import build_snapshot
        from ..ui.config_editor import render_config

        device = await ctx.device()
        snapshot = await build_snapshot(device)
        custom = await device.get_custom_vars()
        contacts = await device.get_contacts()

        # Render every current setting with a short explanation of each.
        render_config(ctx.console, snapshot, custom)

        if contacts:
            ctable = Table(title=f"Contacts ({len(contacts)})", border_style="muted")
            ctable.add_column("Name", style="brand")
            ctable.add_column("Key prefix", style="muted")
            for c in contacts:
                ctable.add_row(c.name, c.key_prefix or "[muted]?[/muted]")
            ctx.console.print(ctable)

        return ToolResult(
            summary={"name": snapshot.get("name"), "contacts": len(contacts)},
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
