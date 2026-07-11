"""The ``watchtower`` tool: a passive sentinel over the nodes you care about.

Opens the Watchtower screen (see :mod:`meshterm.ui.watchtower_screen`): the alert log
with acknowledge/clear, the watchlist, and per-node rules. The rules themselves run in
the background for the whole session (see :mod:`meshterm.services.watchtower`) — a
silence alarm when a starred node goes quiet, an SNR-sag warning when its receptions
degrade, a recovery note when it returns, and a mesh-wide heads-up when a never-before-
seen node appears. Unacknowledged alerts show as the ``▲ n`` badge in the header, so
nothing here needs to be open to be on duty.

Menu-only: the sentinel is inherently a live, session-long service; scripted runs can
read the same history through ``monitor``/``nodes``.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class WatchtowerTool(Tool):
    """Watch starred nodes: silence alarms, SNR sag, new-node sightings."""

    name = "watchtower"
    title = "Watchtower"
    icon = "🚨"
    help = "Alerts for watched nodes — silence, SNR sag, new arrivals"
    category = "Mesh"
    order = 35  # after the views: the section's always-on alerting eye

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the Watchtower screen; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.watchtower_screen import open_watchtower

        await open_watchtower(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the Watchtower lives inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — the Watchtower is a live, menu-only feature.

        Args:
            app: The Typer application (untouched).
        """
