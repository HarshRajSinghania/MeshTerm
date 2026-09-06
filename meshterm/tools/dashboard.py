"""The ``dashboard`` tool: the live mesh overview, at the top of the Watch section.

Opens the full-screen dashboard (see :mod:`meshterm.ui.dashboard_screen`): the two-hour
all-packet activity chart with its pulse line, the session's traffic tallies by packet
class, and the window's RF health beside the radio's own live numbers. (The per-packet
stream is the Live feed tool, right below.) It leads the Watch section because it answers
the section's first question — *what's going on out there?* — before any specific tool
is reached for.

Menu-only: the dashboard is inherently live (it rides the event hub and repaints every
second), so there is no scripted one-shot to register — the ``monitor`` and ``contacts``
subcommands cover scripted inspection.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class DashboardTool(Tool):
    """Watch everything going on on the mesh: activity, traffic, RF health."""

    name = "dashboard"
    title = "Dashboard"
    icon = "📊"
    help = "The mesh live — activity, traffic, RF health"
    category = "Watch"
    order = 10  # the section's overview, above the tools it summarizes

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the live dashboard; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``advert`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.dashboard_screen import open_dashboard

        await open_dashboard(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the dashboard lives inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — the dashboard is a live, menu-only screen.

        Args:
            app: The Typer application (untouched).
        """
