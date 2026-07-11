"""The ``atlas`` tool: the mesh's observed shape, drawn as a live link graph.

Opens the full-screen Mesh Atlas (see :mod:`meshterm.ui.atlas_screen`): every link the
evidence graph holds — trace walks, firmware routes, overheard relay chains, repeater
neighbour tables — drawn as braille edges coloured by SNR around our own node, with
node-by-node walking and per-link detail. The geographic map answers *where* the mesh
is; the atlas answers *how it hangs together*.

Menu-only: the atlas is an interactive reading of stored evidence (and transmits
nothing), so there is no scripted one-shot to register — ``trace``-family commands
already print path evidence in scripted runs.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class AtlasTool(Tool):
    """See how the mesh hangs together: the observed link graph, walkable."""

    name = "atlas"
    title = "Mesh atlas"
    icon = "🕸️"
    help = "Draw the mesh's observed topology — links, SNR, hop rings"
    category = "Mesh"
    order = 32  # beside Map: the logical shape next to the geographic one

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the atlas; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.atlas_screen import open_atlas

        await open_atlas(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the atlas lives inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — the atlas is an interactive, menu-only screen.

        Args:
            app: The Typer application (untouched).
        """
