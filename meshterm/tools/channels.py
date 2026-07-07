"""The ``channels`` tool: create, join, and manage mesh channels.

Interactively it opens a full-screen channel manager (see :mod:`meshterm.ui.channels`) — a
first-class, phone-app-style experience for creating private channels, adding public ``#``
channels, joining with a key, importing a scanned ``meshcore://`` link, and sharing any
channel as a QR code. On the CLI it exposes ``list``, ``add``, ``join``, ``import``, and
``share`` subcommands for scripted use.

Channels are also *listed* by the ``chat`` tool for picking a conversation; this tool owns
everything to do with configuring the slots themselves.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.channels import (
    derive_secret,
    normalize_secret,
    parse_share_url,
    random_secret,
    share_url,
)
from .base import Tool, ToolResult, register


@register
class ChannelsTool(Tool):
    """Create, join, share, and manage the device's mesh channels."""

    name = "channels"
    help = "Create, join, and share mesh channels (with QR codes)."
    category = "Messaging"
    order = 20

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open the channel manager (menu) or run a scripted action (CLI).

        Args:
            ctx: Shared application context.
            params: A ``cli_action`` with its arguments (CLI), or empty for the menu.

        Returns:
            A :class:`ToolResult` summarizing what happened.
        """
        action = params.get("cli_action")
        if action is not None:
            return await self._run_cli(ctx, action, params)

        from ..ui.channels import manage_channels

        changes = await manage_channels(ctx)
        message = (
            f"[ok]✓[/ok] applied [brand]{changes}[/brand] channel change(s)"
            if changes
            else None
        )
        return ToolResult(summary={"changes": changes}, message=message)

    # -- CLI --------------------------------------------------------------------

    async def _run_cli(
        self, ctx: AppContext, action: str, params: dict[str, Any]
    ) -> ToolResult:
        """Dispatch a scripted CLI action."""
        if action == "add":
            return await self._cli_add(ctx, params)
        if action == "join":
            return await self._cli_join(ctx, params)
        if action == "import":
            return await self._cli_import(ctx, params)
        if action == "share":
            return await self._cli_share(ctx, params)
        return await self._cli_list(ctx)

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """List the configured channel slots."""
        from ..ui.channels import _read_slots

        device = await ctx.device()
        slots = await _read_slots(device)
        if not slots:
            ctx.ui.note("[muted]no channels configured[/muted]")
            return ToolResult(summary={"channels": 0})
        table = Table(title="Channels", border_style="muted", expand=False)
        table.add_column("Slot", justify="right")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Hash")
        for slot in slots:
            table.add_row(
                str(slot.idx),
                Text(slot.name, style="brand"),
                "public" if slot.is_public else "private",
                slot.hash,
            )
        ctx.ui.show(table)
        return ToolResult(summary={"channels": len(slots)})

    async def _cli_add(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Add a public, private-random, or explicitly-keyed channel on a slot."""
        device = await ctx.device()
        idx = int(params["index"])
        name = str(params["name"])
        if name.startswith("#"):  # public: firmware derives the key from the name
            await device.set_channel(idx, name, None)
            secret = derive_secret(name)
        else:
            secret = (
                normalize_secret(params["secret"]) if params.get("secret") else random_secret()
            )
            await device.set_channel(idx, name, secret)
        ctx.ui.note(f"[ok]✓[/ok] channel [brand]{name}[/brand] set on slot {idx}")
        self._print_share(ctx, name, secret)
        return ToolResult(summary={"index": idx, "name": name})

    async def _cli_join(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Join a channel from a name and an explicit key."""
        device = await ctx.device()
        idx = int(params["index"])
        name = str(params["name"])
        secret = normalize_secret(str(params["secret"]))
        await device.set_channel(idx, name, secret)
        ctx.ui.note(f"[ok]✓[/ok] joined [brand]{name}[/brand] on slot {idx}")
        return ToolResult(summary={"index": idx, "name": name})

    async def _cli_import(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Import a channel from a ``meshcore://channel/add`` link."""
        parsed = parse_share_url(str(params["url"]))
        if parsed is None:
            raise typer.BadParameter("not a valid meshcore:// channel link")
        name, secret = parsed
        device = await ctx.device()
        idx = int(params["index"])
        await device.set_channel(idx, name, secret)
        ctx.ui.note(f"[ok]✓[/ok] imported [brand]{name}[/brand] on slot {idx}")
        return ToolResult(summary={"index": idx, "name": name})

    async def _cli_share(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print a channel's share link and QR code."""
        from ..ui.channels import _read_slots

        device = await ctx.device()
        idx = int(params["index"])
        slot = next((s for s in await _read_slots(device) if s.idx == idx), None)
        if slot is None:
            ctx.ui.note(f"[warn]slot {idx} is empty[/warn]")
            return ToolResult(summary={"index": idx, "shared": False})
        self._print_share(ctx, slot.name, slot.secret)
        return ToolResult(summary={"index": idx, "shared": True})

    @staticmethod
    def _print_share(ctx: AppContext, name: str, secret: bytes) -> None:
        """Render a channel's QR code and share URL into the output surface."""
        from ..ui.qr import qr_text

        url = share_url(name, secret)
        ctx.ui.show(Group(qr_text(url), Text(""), Text(url, style="accent")))

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``channels`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        channels_app = typer.Typer(
            help=self.help, no_args_is_help=True, rich_markup_mode="rich"
        )

        @channels_app.command("list", help="List the configured channel slots.")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @channels_app.command("add", help="Add a channel (# name = public; else private).")
        def _add_cmd(
            index: int = typer.Argument(..., help="Channel slot index."),
            name: str = typer.Argument(..., help="Channel name (leading # = public)."),
            secret: Optional[str] = typer.Option(
                None, "--secret", help="32-hex-char key (private only; random if omitted)."
            ),
        ) -> None:
            run_tool_command(
                self, {"cli_action": "add", "index": index, "name": name, "secret": secret}
            )

        @channels_app.command("join", help="Join a channel with its name and key.")
        def _join_cmd(
            index: int = typer.Argument(..., help="Channel slot index."),
            name: str = typer.Argument(..., help="Channel name."),
            secret: str = typer.Argument(..., help="32-hex-char (16-byte) key."),
        ) -> None:
            run_tool_command(
                self, {"cli_action": "join", "index": index, "name": name, "secret": secret}
            )

        @channels_app.command("import", help="Import a meshcore:// channel link.")
        def _import_cmd(
            index: int = typer.Argument(..., help="Channel slot index."),
            url: str = typer.Argument(..., help="A meshcore://channel/add link."),
        ) -> None:
            run_tool_command(self, {"cli_action": "import", "index": index, "url": url})

        @channels_app.command("share", help="Print a channel's share link and QR code.")
        def _share_cmd(
            index: int = typer.Argument(..., help="Channel slot index."),
        ) -> None:
            run_tool_command(self, {"cli_action": "share", "index": index})

        app.add_typer(channels_app, name=self.name)
