"""The ``device-actions`` tool: immediate operations on the companion device itself.

Opens the Device actions screen (see :func:`meshterm.ui.config_editor.device_actions`) —
clock sync, backup/restore, the identity key, reboot, and factory reset. Where the
sibling ``config`` tool *stages* value changes for review, everything here acts on the
box the moment it is confirmed, so the two concerns get their own menu entries and
screens. Sending an advert is not here: that everyday operation lives in its own
main-menu entry (the sibling ``advert`` tool — see :mod:`meshterm.tools.advert`).

Menu-only: every action already has a first-class scripted equivalent under the ``config``
CLI group (``config backup``, ``config restore``, ``config reboot``, ...), so no duplicate
subcommand is registered here.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class DeviceActionsTool(Tool):
    """Run immediate device operations: clock, backup, identity key, reboot, reset."""

    name = "device-actions"
    title = "Device actions"
    icon = "🔨"
    help = "Clock, backup, identity key, reboot, reset"
    category = "This node"
    order = 30

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the interactive actions screen; there are never parameters to collect.

        Each action executes and presents its result inside the screen itself, so by the
        time the user backs out there is nothing left for :meth:`run` to do — returning
        ``None`` tells the menu the invocation is complete (the same pattern as backing
        out of the config editor with nothing staged).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.config_editor import device_actions

        await device_actions(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the actions execute inside :meth:`prompt_params`.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            An empty :class:`ToolResult` (only reachable from a scripted call).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — the scripted equivalents live under ``config``.

        Args:
            app: The Typer application (untouched).
        """
