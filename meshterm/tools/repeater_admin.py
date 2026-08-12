"""The ``repeater-admin`` tool: set up remote repeaters and room servers over the mesh.

Interactively it opens the repeater-admin flow (see :mod:`meshterm.ui.repeater_admin`):
pick a node (credentialed ones lead the list), log in with a remembered or prompted
password, and land in a device-configuration-style editor speaking the node's text CLI —
including the repeater-only knobs the local editor never had (TX delay, Direct TX delay,
airtime factor, advert intervals) — plus one-shot actions (advert, clock sync, password,
reboot) and the readline-style remote command line.

On the CLI it stays a scriptable one-shot: ``repeater-admin <node> <command…>`` logs in
(remembered password, or ``--password``) and prints the node's reply — one transmission
per invocation, the trace tool's rule.
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from ..context import AppContext
from ..core.connection import DeviceCommandError
from .base import Tool, ToolResult, register


@register
class RepeaterAdminTool(Tool):
    """Configure a remote repeater/room server: settings, actions, and its raw CLI."""

    name = "repeater-admin"
    title = "Repeater admin"
    icon = "🗼"
    help = "Run a remote repeater — settings and actions"
    category = "Other nodes"
    order = 10  # the remote sibling of the local config tools

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Run the interactive flow; there are never parameters to collect.

        The flow presents everything itself (the same pattern as ``device-actions``),
        so returning ``None`` tells the menu the invocation is complete.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.repeater_admin import open_repeater_admin

        await open_repeater_admin(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Send one CLI command to a remote node and print its reply (scripted path).

        Args:
            ctx: Shared application context.
            params: ``node`` (contact name), ``command`` (the CLI text), and an
                optional ``password`` overriding the remembered one.

        Returns:
            A :class:`ToolResult` carrying the node's reply (or its absence).
        """
        device = await ctx.device()
        contacts = await device.get_contacts()
        name = str(params["node"])
        node = next((c for c in contacts if c.name == name), None)
        if node is None:
            raise DeviceCommandError(f"unknown contact: {name!r}")

        password = params.get("password") or ctx.admin_store.get(node)
        if not password:
            raise DeviceCommandError(
                f"no admin password for {name!r}; pass --password or run the "
                "interactive flow once to store it."
            )
        if not await device.admin_login(node, str(password)):
            ctx.admin_store.forget(node)
            raise DeviceCommandError(
                f"admin login to {name!r} failed (wrong password?). "
                "The saved password was cleared."
            )
        ctx.admin_store.remember(node, str(password))

        command = str(params["command"])
        ctx.remote_store.append_history(node, command)
        reply = await device.send_remote_command(node, command, timeout=10.0)
        if reply is None:
            ctx.ui.note("[warn]no reply — the command may still have landed[/warn]")
        else:
            ctx.ui.note(reply)
        return ToolResult(
            summary={"node": name, "command": command, "replied": reply is not None}
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``repeater-admin`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _repeater_admin(
            node: str = typer.Argument(..., help="The remote contact's name"),
            command: list[str] = typer.Argument(..., help="The CLI command to send"),
            password: Optional[str] = typer.Option(
                None, "--password", help="Admin password (else remembered)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {"node": node, "command": " ".join(command)}
            if password is not None:
                tool_params["password"] = password
            run_tool_command(self, tool_params)
