"""The ``walk`` tool: the mesh's observed shape, explored one node at a time.

Opens the full-screen Mesh walk (see :mod:`meshterm.ui.walk_screen`): a walkable
browser over the evidence graph — trace walks, firmware routes, overheard relay
chains, repeater neighbour tables. One node holds the focus, its neighbourhood draws
as SNR-coloured braille edges on a small clean canvas, and its links list beneath as
rows with quality bars and evidence; Enter walks the graph, ⌫ backtracks along the
breadcrumb trail, and typing finds any node (islands included). The geographic map
answers *where* the mesh is; the walk answers *how it hangs together*.

Menu-only: the walk is an interactive reading of stored evidence (and transmits
nothing), so there is no scripted one-shot to register — ``trace``-family commands
already print path evidence in scripted runs.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class WalkTool(Tool):
    """See how the mesh hangs together: the observed link graph, walkable."""

    name = "walk"
    title = "Mesh walk"
    # A single-codepoint emoji (like every other tool icon), not a VS16 sequence: the
    # emoji-width calibration strips VS16 on terminals that render it narrow, which would
    # measure a 🕸️ one cell short of how it paints and drift this row's help text right of
    # the column the others align to. The wireframe globe reads as the logical topology,
    # the geographic map's 🌍 counterpart.
    icon = "🌐"
    help = "Walk the mesh's observed topology — links, SNR, evidence"
    category = "Mesh"
    order = 32  # beside Map: the logical shape next to the geographic one

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the walk; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.walk_screen import open_walk

        await open_walk(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the walk lives inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — the walk is an interactive, menu-only screen.

        Args:
            app: The Typer application (untouched).
        """
