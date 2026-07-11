"""The ``nodes`` tool: list this node and the contacts it knows about."""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class NodesTool(Tool):
    """List this node and its known contacts, each with its path-hash prefix."""

    name = "nodes"
    title = "Nodes"
    icon = "👥"
    help = "List this node and known contacts (last heard, packets, type, key)"
    category = "Device"
    order = 12

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render the node/contact list.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing the number of known contacts.
        """
        from ..ui.surface import TuiUi
        from ..ui.widgets import NodesSort, nodes_table

        device = await ctx.device()
        info = await device.get_self_info()
        contacts = await device.get_contacts()

        # The path-hash prefix — the leading slice of a key a forced path addresses — is
        # ``mode + 1`` bytes wide; highlight it in every key. The mode is an optional read,
        # so fall back to no highlighting when the firmware doesn't report it.
        try:
            mode = await device.get_path_hash_mode()
        except Exception:  # noqa: BLE001 - optional read; absence just skips highlighting
            mode = None
        prefix_bytes = (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0

        # Overheard-packet tallies from background monitoring, keyed by node hash; a
        # contact we've never passively overheard simply has no entry.
        counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}

        self_name = str(info.get("name") or "this node")
        self_key = str(info.get("public_key") or "")
        sort = NodesSort.from_name(str(params.get("sort") or "name"))

        # In the menu, hand the list to the interactive screen so the arrows re-sort it live;
        # on the scripted CLI, render the table once in the requested order.
        if isinstance(ctx.ui, TuiUi):
            from ..ui.nodes_screen import open_nodes

            await open_nodes(ctx, self_name, self_key, contacts, prefix_bytes, counts, sort)
        else:
            ctx.ui.show(nodes_table(self_name, self_key, contacts, prefix_bytes, counts, sort))

        return ToolResult(summary={"contacts": len(contacts)})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``nodes`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _nodes(
            sort: str = typer.Option(
                "name", "--sort", "-s", help="Order contacts by: name, heard, packets"
            ),
        ) -> None:
            run_tool_command(self, {"sort": sort})
