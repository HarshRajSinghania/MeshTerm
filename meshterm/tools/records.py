"""The Trophy case tool: the record-setting walks the trace tools have turned up.

The menu face opens the browser (:mod:`meshterm.ui.records_screen`): every discipline's
records — longest distance, farthest node, most nodes (with and without revisits),
weakest surviving link, biggest loop — each under its own description, opened for their
full stats, re-walked in Trace path, or deleted. Records are *earned* on the trace
screens, where every walk that comes home is scored and offered to the boards (see
:mod:`meshterm.services.records`); this tool never transmits.

The CLI face prints the stored boards: ``meshterm records`` reads the tables so a script
(or a curious shell) can see the standings without a device attached.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..services.records import CATEGORIES, CATEGORY_BY_ID
from .base import Tool, ToolResult, register


@register
class TrophyCaseTool(Tool):
    """The record-setting mesh walks the trace tools scored, browsed or listed."""

    name = "records"
    title = "Trophy case"
    icon = "🏆"
    help = "Record-setting walks, scored from every trace"
    category = "Explore"
    order = 40  # what the two walks above it score into

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the browser; returning ``None`` completes the invocation with no run row.

        The screen reads stored records and transmits nothing (and logs no rows of its
        own), so it lives in :meth:`prompt_params` and needs no logged run — the mesh walk /
        dashboard pattern.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.records_screen import open_records

        await open_records(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print the stored boards — only reachable from a scripted call (never transmits).

        Args:
            ctx: Shared application context.
            params: ``category``/``width`` filters and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the record count.
        """
        return self._show_records(
            ctx,
            category=params.get("category"),
            width=params.get("width"),
        )

    def _show_records(
        self, ctx: AppContext, *, category: str | None, width: int | None
    ) -> ToolResult:
        """Print the stored record boards (no device, no transmissions).

        Args:
            ctx: Shared application context.
            category: Only this category id, or ``None`` for every discipline.
            width: Only this hash width, or ``None`` for every width.

        Returns:
            A :class:`ToolResult` with the record count.
        """
        wanted = [CATEGORY_BY_ID[category]] if category in CATEGORY_BY_ID else CATEGORIES
        total = 0
        for cat in wanted:
            rows = ctx.repo.discoveries(cat.id, width_bytes=width)
            if not rows:
                continue
            rows.sort(key=lambda r: (r.width_bytes, r.score if cat.ascending else -r.score))
            table = Table(
                title=f"{cat.icon} {cat.title} — {cat.description}",
                title_justify="left",
            )
            table.add_column("width", justify="right")
            table.add_column("score", justify="right")
            table.add_column("route")
            table.add_column("recorded")
            table.add_column("version")
            for record in rows:
                score = cat.format_score(record.score)
                if cat.id == "long_haul" and not record.stats.get("km_complete", True):
                    score = "≥ " + score
                table.add_row(
                    f"{record.width_bytes} B",
                    score,
                    record.spec,
                    record.discovered_at.astimezone().strftime("%b %d %Y %H:%M"),
                    record.app_version,
                )
                total += 1
            ctx.ui.show(table)
        if total == 0:
            ctx.ui.show(
                Text("no records yet — every trace that comes home is scored here", style="muted")
            )
        return ToolResult(
            summary={"records": total},
            message=f"{total} record{'s' if total != 1 else ''} stored",
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``records`` subcommand (read-only record listing).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help="List the record-setting walks (reads the database; never transmits)",
        )
        def _records(
            category: str | None = typer.Option(
                None,
                "--category",
                "-c",
                help="Only this discipline (long_haul, far_point, long_leg, "
                "grand_tour, clean_trail, thin_thread, big_loop)",
            ),
            width: int | None = typer.Option(
                None, "--width", "-w", help="Only records at this hash width (1, 2, or 4)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {}
            if category:
                tool_params["category"] = category
            if width:
                tool_params["width"] = width
            run_tool_command(self, tool_params)
