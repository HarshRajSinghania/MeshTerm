"""The ``info`` tool: show the connected companion device's identity and radio config."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

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
    category = "This node"
    order = 10  # what it is, before anything that changes it

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render its live status plus its full configuration.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing the device name.
        """
        from rich.console import Group

        from ..ui.config_editor import cached_snapshot, config_table, has_pin

        device = await ctx.device()
        snapshot = await cached_snapshot(ctx, device)

        # On the CLI this is the *status* report and nothing else: how the radio is doing
        # right now, one fact per line. What it is set to is `config show`'s answer, in
        # the very keys `config get`/`config set` take, so printing the settings here as
        # well would be the same table twice under two different spellings.
        session = getattr(ctx.ui, "session", None)
        if session is None:
            from ..ui import script

            ctx.ui.show(script.pairs(await status_pairs(device, snapshot)))
            return ToolResult(summary={"name": snapshot.get("name")})

        from ..ui.device_info_screen import DeviceInfoScreen

        custom = await device.get_custom_vars()
        # A live status panel (role, firmware, battery, clock, radio/packet statistics)
        # above every current setting with a short explanation of each. The panel is read
        # once; only the table differs between the two states, and it is cheap to rebuild.
        panel = await _status_panel(device, snapshot)

        def page(reveal_pin: bool) -> Group:
            return Group(panel, Text(""), config_table(snapshot, custom, reveal_pin=reveal_pin))

        # The page is its own screen because it has a key of its own: the pairing PIN is
        # masked until ``p`` uncovers it.
        await session.run_screen(
            DeviceInfoScreen(session, page, title=self.title, conceals=has_pin(snapshot))
        )
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


async def _read_status(device: Device) -> dict[str, Any]:
    """Read every live status value the firmware will answer for, best-effort.

    The one place the device is asked. Both faces build from what it returns: the menu's
    panel composes prose rows out of these values, the CLI prints them one fact per line
    (see :func:`_status_panel` and :func:`status_pairs`). Every read is optional —
    firmware predating a query simply contributes nothing — so this works on any device.

    Args:
        device: The connected device to query.

    Returns:
        The merged raw readings: ``info``, ``battery``, ``stats`` and ``clock``.
    """
    return {
        "info": await _try(device.get_device_info) or {},
        "battery": await _try(device.get_battery) or {},
        "stats": await _try(device.get_stats) or {},
        "clock": await _try(device.get_time),
    }


async def _status_panel(device: Device, snapshot: dict) -> Panel:
    """Build the live status panel: role, firmware, battery, clock, and statistics.

    The menu's face of :func:`_read_status` — each row a short phrase rather than an
    atomic value, because a person reads "-110 dBm noise floor · last RSSI -62 dBm" in one
    go where a script wants the three numbers apart.

    Args:
        device: The connected device to query.
        snapshot: The already-built settings snapshot (for role and identity fields).

    Returns:
        A Rich :class:`Panel` of label/value status rows.
    """
    rows: list[tuple[str, Text]] = []
    readings = await _read_status(device)
    info, battery, stats = readings["info"], readings["battery"], readings["stats"]

    adv_type = snapshot.get("adv_type")
    if adv_type is not None:
        role = _ROLES.get(int(adv_type), f"type {adv_type}")
        rows.append(("role", Text(role, style="brand")))

    if info.get("model"):
        rows.append(("model", Text(str(info["model"]))))
    firmware = _firmware(info)
    if firmware:
        rows.append(("firmware", Text(firmware)))

    level = battery.get("level")
    if level:
        rows.append(("battery", Text(f"{int(level) / 1000:.2f} V")))
    if battery.get("total_kb"):
        rows.append(("storage", Text(f"{battery.get('used_kb', 0)} / {battery['total_kb']} kB")))

    clock = readings["clock"]
    if clock:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock))
        rows.append(("clock", Text.assemble(stamp, (f"  {_drift(clock)}", "muted"))))

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


def _firmware(info: dict) -> str:
    """The firmware version and build joined, or ``""`` when neither was reported."""
    return " ".join(str(info[k]) for k in ("ver", "fw_build") if info.get(k))


async def status_pairs(device: Device, snapshot: dict) -> list[tuple[str, str]]:
    """The device's live status as scripted key/value facts.

    The CLI's face of :func:`_read_status`. Where the panel writes a phrase per row, this
    writes one value per key and puts the unit in the key — ``battery_v``, ``uptime_s``,
    ``noise_floor_dbm`` — so nothing has to be pulled back out of prose. A reading the
    firmware did not answer for is absent rather than :data:`~meshterm.ui.script.NONE`:
    these are optional queries, and a missing key says "this device does not report it"
    where a dash would claim it reported nothing.

    Args:
        device: The connected device to query.
        snapshot: The already-built settings snapshot (for role and identity).

    Returns:
        ``(key, value)`` pairs in reading order.
    """
    readings = await _read_status(device)
    info, battery, stats = readings["info"], readings["battery"], readings["stats"]
    pairs: list[tuple[str, str]] = []

    def add(key: str, value: Any) -> None:
        """Record one fact, skipping a reading the firmware did not answer for."""
        if value is not None and value != "":
            pairs.append((key, str(value)))

    # Identity first: which radio this is. Everything else it *can be set to* is
    # `config show`'s answer, not this one. The name is not quoted here the way it is in
    # a listing: a key/value line has exactly two fields, so the value is the rest of the
    # line whatever it contains, and quotes would only be something to strip back off.
    add("name", snapshot.get("name"))
    add("public_key", str(snapshot.get("public_key") or "").lower() or None)
    adv_type = snapshot.get("adv_type")
    add("role", _ROLES.get(int(adv_type), f"type {adv_type}") if adv_type is not None else None)

    add("model", info.get("model"))
    add("firmware", _firmware(info) or None)

    level = battery.get("level")
    add("battery_v", f"{int(level) / 1000:.2f}" if level else None)
    if battery.get("total_kb"):
        add("storage_used_kb", battery.get("used_kb", 0))
        add("storage_total_kb", battery["total_kb"])

    clock = readings["clock"]
    if clock:
        add("clock", datetime.fromtimestamp(clock, tz=timezone.utc).astimezone().isoformat())
        add("clock_drift_s", clock - int(time.time()))

    add("uptime_s", stats.get("uptime_secs"))
    add("noise_floor_dbm", stats.get("noise_floor"))
    add("last_rssi_dbm", stats.get("last_rssi"))
    add("last_snr_db", f"{stats['last_snr']:+.1f}" if stats.get("last_snr") is not None else None)
    add("tx_air_s", stats.get("tx_air_secs"))
    add("rx_air_s", stats.get("rx_air_secs"))
    add("packets_sent", stats.get("sent"))
    add("packets_received", stats.get("recv"))
    add("receive_errors", stats.get("recv_errors"))
    return pairs


async def _try(read) -> Any | None:
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
