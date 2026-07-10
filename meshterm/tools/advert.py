"""The ``advert`` tool: announce this node to the mesh, straight from the main menu.

Opens the Send advert flow (see :func:`meshterm.ui.config_editor.send_advert`) — a
zero-hop or flood advertisement, or this node's shareable contact card as a QR code.
The same flow also lives inside the Device actions screen; it gets its own menu entry
because sending an advert is the everyday operation in that bucket, promoted to the top
level so it sits one keystroke (and one type-to-filter match) away instead of two
screens deep.

Menu-only: the scripted equivalents are ``config advert`` and ``config share``, so no
duplicate subcommand is registered here (the same pattern as ``device-actions``).
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class SendAdvertTool(Tool):
    """Send a zero-hop or flood advert, or share this node's contact card."""

    name = "advert"
    title = "Send advert"
    help = "Announce this node to the mesh — zero-hop, flood, or share as QR"
    category = "Device"
    order = 10  # right after Device actions, before Device info

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the interactive advert flow; there are never parameters to collect.

        The flow presents its own result (the sent-advert note or the contact-card
        view) before returning, so by the time the user backs out there is nothing
        left for :meth:`run` to do — returning ``None`` tells the menu the invocation
        is complete (the same pattern as the Device actions screen).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.config_editor import send_advert

        await send_advert(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the advert is sent inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — ``config advert`` / ``config share`` cover scripting.

        Args:
            app: The Typer application (untouched).
        """
