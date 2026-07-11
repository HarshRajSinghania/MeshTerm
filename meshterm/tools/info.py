"""The ``info`` tool: show the connected companion device's identity and radio config."""

from __future__ import annotations

import time
from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.text import Text

from ..context import AppContext
from ..core.connection import Device
from .base import Tool, ToolResult, register

#: MeshCore advert-type byte -> human-readable node role.
_ROLES = {1: "companion", 2: "repeater", 3: "room server", 4: "sensor"}


@register
class InfoTool(Tool):
    """Display device identity and radio configuration.

    The known-contacts list lives in the separate ``nodes`` tool.
    """

    name = "info"
    title = "Device info"
    icon = "📋"
    help = "Show device status, identity, and radio config"
    category = "Device"
    order = 11  # after Send advert (10), which slots in behind Device actions

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render its live status plus its full configuration.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing the device name.
        """
        from ..core.device_config import build_snapshot
        from ..ui.config_editor import config_table

        device = await ctx.device()
        snapshot = await build_snapshot(device)
        custom = await device.get_custom_vars()

        # A live status panel (role, firmware, battery, clock, radio/packet statistics)
        # above every current setting with a short explanation of each.
        ctx.ui.show(await _status_panel(device, snapshot), Text(""), config_table(snapshot, custom))

        return ToolResult(summary={"name": snapshot.get("name")})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``info`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _info() -> None:
            run_tool_command(self, {})


# -- the status panel -----------------------------------------------------------


async def _status_panel(device: Device, snapshot: dict) -> Panel:
    """Build the live status panel: role, firmware, battery, clock, and statistics.

    Every read beyond the snapshot is best-effort — firmware predating a query (battery,
    clock, stats) simply contributes no row — so the panel renders on any device.

    Args:
        device: The connected device to query.
        snapshot: The already-built settings snapshot (for role and identity fields).

    Returns:
        A Rich :class:`Panel` of label/value status rows.
    """
    rows: list[tuple[str, Text]] = []

    adv_type = snapshot.get("adv_type")
    if adv_type is not None:
        role = _ROLES.get(int(adv_type), f"type {adv_type}")
        rows.append(("role", Text(role, style="brand")))

    info = await _try(device.get_device_info) or {}
    if info.get("model"):
        rows.append(("model", Text(str(info["model"]))))
    firmware = " ".join(str(info[k]) for k in ("ver", "fw_build") if info.get(k))
    if firmware:
        rows.append(("firmware", Text(firmware)))

    battery = await _try(device.get_battery) or {}
    level = battery.get("level")
    if level:
        rows.append(("battery", Text(f"{int(level) / 1000:.2f} V")))
    if battery.get("total_kb"):
        rows.append(
            ("storage", Text(f"{battery.get('used_kb', 0)} / {battery['total_kb']} kB"))
        )

    clock = await _try(device.get_time)
    if clock:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock))
        rows.append(("clock", Text.assemble(stamp, (f"  {_drift(clock)}", "muted"))))

    stats = await _try(device.get_stats) or {}
    if stats.get("uptime_secs") is not None:
        rows.append(("uptime", Text(_uptime(int(stats["uptime_secs"])))))
    if stats.get("noise_floor") is not None:
        parts = Text(f"{stats['noise_floor']} dBm noise floor")
        if stats.get("last_rssi") is not None:
            parts.append(f" · last RSSI {stats['last_rssi']} dBm", style="muted")
        if stats.get("last_snr") is not None:
            parts.append(f" · last SNR {stats['last_snr']:+.1f} dB", style="muted")
        rows.append(("radio", parts))
    if stats.get("tx_air_secs") is not None:
        rows.append(
            ("airtime", Text(f"TX {stats['tx_air_secs']} s · RX {stats.get('rx_air_secs', 0)} s"))
        )
    if stats.get("recv") is not None:
        packets = Text(f"{stats.get('sent', 0)} sent · {stats['recv']} received")
        if stats.get("recv_errors"):
            packets.append(f" · {stats['recv_errors']} receive errors", style="warn")
        rows.append(("packets", packets))

    width = max((len(label) for label, _ in rows), default=0)
    body = Text()
    for i, (label, value) in enumerate(rows):
        if i:
            body.append("\n")
        body.append(f"{label.ljust(width)}  ", style="muted")
        body.append_text(value)
    if not rows:
        body = Text("no status reported", style="muted")
    return Panel(body, title="[accent]Status[/accent]", border_style="accent", expand=False)


async def _try(read) -> Optional[Any]:
    """Await a device read, returning ``None`` when the firmware doesn't support it."""
    try:
        return await read()
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        return None


def _drift(device_epoch: int) -> str:
    """Describe the device clock's drift against this computer's clock."""
    drift = device_epoch - int(time.time())
    if abs(drift) < 2:
        return "(in sync)"
    direction = "ahead" if drift > 0 else "behind"
    return f"({abs(drift)} s {direction})"


def _uptime(secs: int) -> str:
    """Render an uptime as ``3d 2h 41m`` (seconds shown only under a minute)."""
    if secs < 60:
        return f"{secs} s"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)
