"""The ``devices`` tool: enumerate attached serial and in-range Bluetooth companions.

Discovery never opens the radio — it only lists what is attached or advertising. This is a
CLI-only, read-only inventory (``menu_visible = False``): by the time the interactive menu is
up a device is already selected, so the listing has no job there — it belongs on the command
line as a startup-time "which port/address is my radio?" diagnostic. It marks devices already
confirmed as MeshCore companions (and the active one). Selecting a companion is a startup-only
concern: pass ``--port`` or ``--ble`` on the CLI (remembered after it connects), or pick from
the prompt shown when the menu launches (which smoke-tests the choice before confirming it).
"""

from __future__ import annotations

from typing import Any

import typer
from rich.table import Table

from ..context import AppContext
from ..core.discovery import discover_all, discover_devices
from .base import Tool, ToolResult, register

#: "MeshCore?" table cell per discovery confidence tier, for devices we have *not* yet
#: confirmed. The USB vendor ID is only a hint — a native-USB board or a bare bridge chip
#: is a "maybe", never a "yes" — so nothing is billed as MeshCore until a connection proves
#: it (see ``_confirmed_cell``).
_MAYBE_CELL: dict[str, str] = {
    "board": "[warn]maybe[/warn]",
    "bridge": "[warn]maybe[/warn]",
    "unknown": "[muted]—[/muted]",
}

#: "MeshCore?" cell for a device already confirmed to speak the protocol.
_CONFIRMED_CELL = "[ok]yes[/ok]"


@register
class DevicesTool(Tool):
    """List attached serial + in-range Bluetooth companions, flag likely LoRa, mark the default."""

    name = "devices"
    help = "List attached serial and Bluetooth companion devices and flag likely LoRa hardware."
    category = "Device"
    order = 5
    menu_visible = False  # CLI-only: a startup diagnostic with no place in a connected session

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Enumerate serial devices and render them as a table.

        Args:
            ctx: Shared application context.
            params: Unused beyond the menu's selection bookkeeping.

        Returns:
            A :class:`ToolResult` summarizing how many devices were found.
        """
        scan_ble = params.get("ble", True)
        devices = await discover_all(ble=scan_ble) if scan_ble else discover_devices()
        known = ctx.device_store.load_all()
        remembered = ctx.device_store.load()
        active = ctx.selected_device
        active_target = ctx.ble_override or ctx.port_override or (
            active.target if active else None
        )

        if ctx.json_output:
            import json

            payload = [
                {
                    "transport": d.transport,
                    "port": d.port,
                    "address": d.address,
                    "target": d.target,
                    "label": d.label,
                    "vendor": d.vendor_label,
                    "model": (known[d.stable_id].hardware_model if d.stable_id in known else ""),
                    "likely_lora": d.is_likely_lora,
                    "confidence": d.confidence,
                    "confirmed": d.stable_id in known,
                    "serial_number": d.serial_number,
                    "stable_id": d.stable_id,
                    "remembered": remembered is not None and remembered.matches(d),
                    "active": d.target == active_target,
                }
                for d in devices
            ]
            ctx.console.print_json(json.dumps(payload))
            return ToolResult(summary={"count": len(devices)})

        if not devices:
            ctx.ui.note(
                "[warn]No companion devices detected.[/warn] "
                "Connect one over USB or power on a Bluetooth companion nearby, or use "
                "[accent]--mock[/accent] for the simulator."
            )
            return ToolResult(summary={"count": 0})

        table = Table(title="Companion devices", border_style="muted", expand=False)
        table.add_column("", style="ok", no_wrap=True)  # active/confirmed markers
        table.add_column("Port / Address", style="brand")
        table.add_column("Device")
        # "Hardware" (not "Vendor"): for a confirmed device this holds the firmware's own model
        # string ("Seeed Tracker T1000-E") — the only reliable source of what the box is — and
        # for a merely-attached serial port it falls back to the USB vendor name ("Espressif").
        # One column spans both because a maker name and a model name are the same question:
        # "what hardware is this?". A BLE device that's never connected has neither, so it's "?".
        table.add_column("Hardware", style="muted")
        table.add_column("MeshCore?", justify="center")
        table.add_column("Serial", style="muted")
        for d in devices:
            confirmed = known.get(d.stable_id)  # the remembered record, if ever confirmed
            is_active = d.target == active_target
            marker = ("●" if is_active else "") + ("★" if confirmed else "")
            device_name = d.name or d.product or d.description or "[muted]?[/muted]"
            if confirmed and confirmed.node_name:  # the mesh name learned on connect
                device_name = f"{confirmed.node_name}  [muted]({device_name})[/muted]"
            # Prefer the connected-time model; fall back to the USB vendor for unconnected ports.
            hardware = (confirmed.hardware_model if confirmed else "") or d.vendor_label
            table.add_row(
                marker,
                d.target,
                device_name,
                hardware or "[muted]?[/muted]",
                _CONFIRMED_CELL if confirmed else _MAYBE_CELL[d.confidence],
                d.serial_number or "[muted]—[/muted]",
            )
        ctx.ui.show(table)
        ctx.ui.note(
            "[muted]● active   ★ confirmed MeshCore device. "
            "Pass --port <PORT> or --ble <ADDRESS> to select on the CLI.[/muted]"
        )
        return ToolResult(summary={"count": len(devices)})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``devices`` subcommand (inventory only; no connection).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _devices(
            ble: bool = typer.Option(
                True, "--ble/--no-ble", help="Include a Bluetooth LE scan (adds a few seconds)."
            ),
        ) -> None:
            run_tool_command(self, {"ble": ble})
