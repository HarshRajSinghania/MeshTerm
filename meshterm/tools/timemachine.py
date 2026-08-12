"""The ``timemachine`` tool: explore everything the recorder ever heard.

Opens the Time Machine (see :mod:`meshterm.ui.timemachine_screen`): pick the whole mesh
or any node ever observed and read its history as braille charts — reception volume,
the median-SNR band, the hour-of-day rhythm, packets and nodes per day, first-ever
arrivals — over a switchable 24 h / 7 d / 30 d / all-time window. The passive monitor
has been writing this history since the first session; this is where it pays off.

Menu-only: the explorer is interactive by nature (subjects, windows, scrolling); the
``monitor`` and ``nodes`` subcommands remain the scripted views over the same data.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class TimeMachineTool(Tool):
    """Read the mesh's recorded past: per-node timelines, daily volumes, arrivals."""

    name = "timemachine"
    title = "Time machine"
    icon = "⏳"
    help = "Recorded history — timelines, rhythms, arrivals"
    category = "Watch"
    order = 40  # the past tense of the Dashboard above it

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the explorer; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.timemachine_screen import open_timemachine

        await open_timemachine(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the Time Machine lives inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — the Time Machine is an interactive explorer.

        Args:
            app: The Typer application (untouched).
        """
