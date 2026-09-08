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
from ..core import exitcodes
from ..core.discovery import discover_all, discover_devices
from .base import Tool, ToolResult, register

#: The ``MESHCORE`` verdict per discovery confidence tier, for devices we have *not* yet
#: confirmed. The USB vendor ID is only a hint — a native-USB board or a bare bridge chip
#: is a "maybe", never a "yes" — so nothing is billed as MeshCore until a connection proves
#: it.
_MAYBE: dict[str, str] = {"board": "maybe", "bridge": "maybe", "unknown": "no"}


@register
class DevicesTool(Tool):
    """List attached serial + in-range Bluetooth companions, flag likely LoRa, mark the default."""

    name = "devices"
    title = "Devices"
    help = "List attached serial and Bluetooth devices, flagging likely companions"
    category = "This node"
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
        active_target = ctx.ble_override or ctx.port_override or (active.target if active else None)

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
            # Not an error: the scan ran and found nothing. Said on stderr so a caller
            # redirecting stdout still hears it, and reported as NO_RESULT so a script can
            # branch on it without matching prose.
            from ..ui import script

            script.stderr_console().print(
                "meshterm: no companion devices detected", style="warn", highlight=False
            )
            return ToolResult(summary={"count": 0}, exit_code=exitcodes.NO_RESULT)

        ctx.ui.show(_listing(devices, known, active_target))
        return ToolResult(summary={"count": len(devices)})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``devices`` subcommand (inventory only; no connection).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help=self.help,
            # The listing used to close with a line explaining its own markers and how to
            # act on a row. That is help, and this is where help goes.
            epilog="Select a device with --port TARGET or --ble TARGET, using the "
            "TARGET column verbatim.",
        )
        def _devices(
            ble: bool = typer.Option(
                True, "--ble/--no-ble", help="Include a Bluetooth LE scan (adds a few seconds)"
            ),
        ) -> None:
            run_tool_command(self, {"ble": ble})


def _listing(devices: list, known: dict, active_target: str | None) -> Table:
    """The scripted device inventory: one line per attached or advertising device.

    ``TARGET`` leads because it is the field a caller acts on — it is what ``--port`` and
    ``--ble`` take, verbatim. The menu's two markers become columns of their own
    (``ACTIVE``, and ``MESHCORE`` for the confirmed star), because a glyph in a margin is
    something to look at rather than something to test.

    ``MESHCORE`` is three-valued and stays that way: ``yes`` only once a connection has
    proved the device speaks the protocol, ``maybe`` for a USB vendor ID that suggests a
    LoRa board or a bridge chip, ``no`` for anything else. A vendor ID is a hint, and the
    column would be lying if it rounded one up.

    Args:
        devices: The discovered devices.
        known: Remembered device records, keyed by stable id.
        active_target: The target this invocation is (or would be) using.

    Returns:
        The scripted table (see :func:`meshterm.ui.script.columns`).
    """
    from ..ui import script

    table = script.columns(
        "TARGET", "TRANSPORT", "NAME", "HARDWARE", "MESHCORE", "SERIAL", "ACTIVE"
    )
    for device in devices:
        confirmed = known.get(device.stable_id)
        name = (confirmed.node_name if confirmed else "") or device.label
        hardware = (confirmed.hardware_model if confirmed else "") or device.vendor_label
        table.add_row(
            device.target,
            device.transport,
            script.name(name),
            script.quote(hardware) if hardware else script.NONE,
            "yes" if confirmed else _MAYBE.get(device.confidence, "no"),
            device.serial_number or script.NONE,
            "yes" if device.target == active_target else "no",
        )
    return table
