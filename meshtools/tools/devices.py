"""The ``devices`` tool: enumerate serial companion devices and (in the menu) pick one.

Discovery never opens the radio — it only lists what is attached. On the CLI this is a
read-only inventory; selection there happens by passing ``--port`` to a command, which is
remembered after it connects. In the interactive menu this tool additionally offers the
device picker to change the session's active device.
"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..context import AppContext
from ..core.discovery import discover_devices
from .base import Tool, ToolResult, register

#: "LoRa?" table cell per discovery confidence tier: a bare serial bridge is only a "maybe",
#: since the same chip shows up on plenty of non-LoRa hardware.
_LORA_CELL: dict[str, str] = {
    "board": "[ok]yes[/ok]",
    "bridge": "[warn]maybe[/warn]",
    "unknown": "[muted]—[/muted]",
}


@register
class DevicesTool(Tool):
    """List attached serial devices, flag likely LoRa hardware, mark the default."""

    name = "devices"
    help = "List attached serial devices and pick the companion to use."
    category = "Device"
    order = 5

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any]:
        """In the menu, offer to change the session's active device.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"select": True}`` when the user opted to re-pick a device, else ``{}``.
        """
        if ctx.mock:
            return {}
        change = await ctx.ui.confirm("Change the active companion device?", default=False)
        if not change:
            return {}

        from ..ui.device_picker import prompt_device

        devices = discover_devices()
        chosen = await prompt_device(ctx.ui, devices, ctx.device_store.load())
        if chosen is not None:
            ctx.selected_device = chosen
            ctx.port_override = chosen.port
            ctx.ui.note(
                f"[ok]●[/ok] active device set to [accent]{chosen.label}[/accent] "
                "[muted](remembered once it connects)[/muted]"
            )
        return {"select": True}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Enumerate serial devices and render them as a table.

        Args:
            ctx: Shared application context.
            params: Unused beyond the menu's selection bookkeeping.

        Returns:
            A :class:`ToolResult` summarizing how many devices were found.
        """
        devices = discover_devices()
        remembered = ctx.device_store.load()
        active = ctx.selected_device
        active_port = ctx.port_override or (active.port if active else None)

        if ctx.json_output:
            import json

            payload = [
                {
                    "port": d.port,
                    "label": d.label,
                    "vendor": d.vendor_label,
                    "likely_lora": d.is_likely_lora,
                    "confidence": d.confidence,
                    "serial_number": d.serial_number,
                    "stable_id": d.stable_id,
                    "remembered": remembered is not None and remembered.matches(d),
                    "active": d.port == active_port,
                }
                for d in devices
            ]
            ctx.console.print_json(json.dumps(payload))
            return ToolResult(summary={"count": len(devices)})

        if not devices:
            ctx.ui.note(
                "[warn]No serial devices detected.[/warn] "
                "Connect a companion device, or use [accent]--mock[/accent] for the simulator."
            )
            return ToolResult(summary={"count": 0})

        table = Table(title="Serial devices", border_style="muted", expand=False)
        table.add_column("", style="ok", no_wrap=True)  # active/remembered markers
        table.add_column("Port", style="brand")
        table.add_column("Device")
        table.add_column("Vendor", style="muted")
        table.add_column("LoRa?", justify="center")
        table.add_column("Serial", style="muted")
        for d in devices:
            is_remembered = remembered is not None and remembered.matches(d)
            is_active = d.port == active_port
            marker = ("●" if is_active else "") + ("★" if is_remembered else "")
            device_name = d.product or d.description or "[muted]?[/muted]"
            if is_remembered and remembered.node_name:  # the mesh name learned on connect
                device_name = f"{remembered.node_name}  [muted]({device_name})[/muted]"
            table.add_row(
                marker,
                d.port,
                device_name,
                d.vendor_label or "[muted]?[/muted]",
                _LORA_CELL[d.confidence],
                d.serial_number or "[muted]—[/muted]",
            )
        ctx.ui.show(table)
        ctx.ui.note(
            "[muted]● active   ★ remembered default. "
            "Pass --port <PORT> to select on the CLI.[/muted]"
        )
        return ToolResult(summary={"count": len(devices)})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``devices`` subcommand (inventory only; no connection).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _devices() -> None:
            run_tool_command(self, {})
