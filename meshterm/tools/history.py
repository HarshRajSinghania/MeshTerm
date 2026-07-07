"""The ``history`` tool: browse logged tool executions from the database."""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..context import AppContext
from .base import Tool, ToolResult, register

_STATUS_STYLE = {"ok": "ok", "error": "err", "running": "warn"}


@register
class HistoryTool(Tool):
    """List recent runs recorded in the database (no device needed)."""

    name = "history"
    help = "Show recent tool executions and their outcomes."
    category = "Data"
    order = 10

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Render the most recent runs as a table.

        Args:
            ctx: Shared application context.
            params: Optional ``limit`` (int) controlling row count.

        Returns:
            A :class:`ToolResult` noting how many runs were shown.
        """
        limit = int(params.get("limit", 20))
        runs = ctx.repo.list_runs(limit=limit)

        table = Table(title=f"Recent runs (last {limit})", border_style="muted")
        table.add_column("#", justify="right", style="muted")
        table.add_column("Tool", style="brand")
        table.add_column("Profile", style="muted")
        table.add_column("Status")
        table.add_column("Started")
        for r in runs:
            style = _STATUS_STYLE.get(r.status, "muted")
            table.add_row(
                str(r.id),
                r.tool,
                r.profile or "-",
                f"[{style}]{r.status}[/{style}]",
                r.started_at.replace("T", " ")[:19],
            )
        ctx.ui.show(table)
        return ToolResult(summary={"shown": len(runs)})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``history`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _history(
            limit: int = typer.Option(20, "--limit", "-n", help="Number of runs to show."),
        ) -> None:
            run_tool_command(self, {"limit": limit})
