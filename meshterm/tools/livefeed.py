"""The ``livefeed`` tool: every packet as it arrives, right under the dashboard.

Opens the full-screen live feed (see :mod:`meshterm.ui.livefeed_screen`): the
streaming packet list that used to be the dashboard's bottom panel, promoted to its
own menu entry — newest first, every class, Enter opening any row in the shared
packet viewer. It sits directly under the dashboard because the two split one
question between them: the dashboard summarizes *what's going on out there*, the
feed shows every individual packet behind those numbers.

Menu-only: the feed is inherently live (it rides the event hub and repaints every
second), so there is no scripted one-shot to register — the ``monitor`` subcommand
covers scripted packet-watching.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class LiveFeedTool(Tool):
    """Watch every packet stream in, newest first, and open any in the viewer."""

    name = "livefeed"
    title = "Live feed"
    icon = "📰"
    help = "Every packet as it arrives, newest first"
    category = "Watch"
    order = 20  # right under the dashboard it was promoted out of

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the live feed; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.livefeed_screen import open_livefeed

        await open_livefeed(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the feed lives inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — the feed is a live, menu-only screen.

        Args:
            app: The Typer application (untouched).
        """
