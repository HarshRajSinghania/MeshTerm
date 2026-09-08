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

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.connection import DeviceCommandError
from ..core.models import NODE_TYPE_LABELS, Contact, LoginResult
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Facts


@register
class RepeaterAdminTool(Tool):
    """Configure a remote repeater/room server: settings, actions, and its raw CLI."""

    name = "repeater-admin"
    title = "Repeater admin"
    icon = "🗼"
    help = "Run a remote repeater — settings and actions"
    category = "Other nodes"
    order = 10  # the remote sibling of the local config tools

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
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
        # Both of these are the *argument* being wrong, and nothing has gone out over
        # the air when either fires — so they are usage errors (2), not device failures
        # (4), whose promise to a caller is that a retry is worth trying. `tx-optimize`
        # already answered the identical question this way for the identical condition.
        if node is None:
            raise typer.BadParameter(f"unknown contact: {name!r}")

        password = params.get("password") or ctx.admin_store.get(node)
        if not password:
            raise typer.BadParameter(
                f"no admin password for {name!r}; pass --password or run the "
                "interactive flow once to store it."
            )
        outcome = await device.admin_login(node, str(password))
        ctx.admin_store.record(node, str(password), outcome)
        if outcome is LoginResult.REFUSED:
            raise DeviceCommandError(
                f"admin login to {name!r} failed (wrong password?). The saved password was cleared."
            )
        if not outcome:
            raise DeviceCommandError(
                f"{name!r} did not answer the admin login — it may be out of reach, "
                "asleep, or busy. Any saved password was kept; try again when it answers."
            )

        command = str(params["command"])
        ctx.remote_store.append_history(node, command)
        reply = await device.send_remote_command(node, command, timeout=10.0)
        return ToolResult(
            report=(_answered(node, command, reply),),
            summary={"node": name, "command": command, "replied": reply is not None},
            # Silence is not failure — the command may well have landed — but there is
            # nothing to report, and a script waiting on output should know which it got.
            exit_code=exitcodes.OK if reply is not None else exitcodes.NO_RESULT,
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
            password: str | None = typer.Option(
                None, "--password", help="Admin password (else remembered)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {"node": node, "command": " ".join(command)}
            if password is not None:
                tool_params["password"] = password
            run_tool_command(self, tool_params)


def _answered(node: Contact, command: str, reply: str | None) -> Facts:
    """What a remote node said back.

    ``reply`` is the node's own text, whole and verbatim — the plain face prints it bare
    (as text and never as markup, so a reply holding a square bracket is a reply holding a
    square bracket), and the document carries it raw, multi-line where the node sent
    several lines.

    It is deliberately **not parsed**. MeshTerm does not know the remote node's CLI
    grammar, and a document that pretended to would be inventing structure a consumer
    would then depend on.
    """
    from ..ui import fields
    from ..ui.fields import NodeRef
    from ..ui.report import BARE, Facts

    return Facts(
        key="remote",
        fields=(
            fields.node("node", lanes=()),
            fields.hidden("command"),
            fields.word("reply", "reply"),
        ),
        values={
            "node": NodeRef(
                name=node.name,
                key=(node.public_key or "").lower() or None,
                hash=(node.key_prefix or "").lower() or None,
                type=NODE_TYPE_LABELS.get(node.node_type),
            ),
            "command": command,
            "reply": reply,
        },
        shape=BARE,
        bare="reply",
    )
