"""The ``info`` tool: show the connected companion device's identity and radio config."""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class InfoTool(Tool):
    """Display device identity and radio configuration.

    The known-contacts list lives in the separate ``nodes`` tool.
    """

    name = "info"
    help = "Show device identity and radio config."
    category = "Device"
    order = 10

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render its full configuration.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing the device name.
        """
        from ..core.device_config import build_snapshot
        from ..ui.config_editor import config_table

        device = await ctx.device()
        snapshot = await build_snapshot(device)
        custom = await device.get_custom_vars()

        # Render every current setting with a short explanation of each.
        ctx.ui.show(config_table(snapshot, custom))

        return ToolResult(summary={"name": snapshot.get("name")})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``info`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _info() -> None:
            run_tool_command(self, {})
