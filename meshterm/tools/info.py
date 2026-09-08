"""The ``info`` tool: show the connected companion device's identity and radio config."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import typer
from rich.panel import Panel
from rich.text import Text

from ..context import AppContext
from ..core.connection import Device
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Column, Facts

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
            return ToolResult(
                summary={"name": snapshot.get("name")},
                report=(await status_facts(device, snapshot),),
            )

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
    (see :func:`_status_panel` and :func:`status_facts`). Every read is optional —
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


async def status_facts(device: Device, snapshot: dict) -> Facts:
    """The device's live status as one set of facts.

    The CLI's face of :func:`_read_status`. Where the panel writes a phrase per row, this
    writes one value per key and puts the unit *in the key* — ``battery_v``, ``uptime_s``,
    ``noise_floor_dbm`` — so nothing has to be pulled back out of prose. A number never
    carries a unit in its value, on either face.

    The three second-valued readings and the clock drift carry a **gloss** beside the
    figure: ``uptime_s  93784  (1d 2h)``. This is the one key/value block that may, and
    the reason is that it is the one with no ``get`` and no ``set`` behind it — there is no
    round trip to protect, the key still says what the number counts, and ``93784`` is a
    reading nobody can hold in their head. The document carries the number alone, having
    nowhere useful to put a phrase.

    A reading the firmware did not answer for is **left out** of the plain block rather
    than dashed: these are optional queries, and a missing key says "this device does not
    report it" where a dash would claim it reported nothing. The document keeps the key and
    writes ``null`` — see :attr:`~meshterm.ui.report.Facts.omit_absent` for why the two
    faces disagree here on purpose.

    Args:
        device: The connected device to query.
        snapshot: The already-built settings snapshot (for role and identity).

    Returns:
        The facts block.
    """
    from ..ui import fields
    from ..ui.report import Facts

    readings = await _read_status(device)
    info, battery, stats = readings["info"], readings["battery"], readings["stats"]

    level = battery.get("level")
    adv_type = snapshot.get("adv_type")
    clock = readings["clock"]
    values: dict[str, Any] = {
        # Identity first: which radio this is. Everything it *can be set to* is
        # `config show`'s answer, not this one.
        "name": snapshot.get("name"),
        "public_key": str(snapshot.get("public_key") or "").lower() or None,
        "role": _ROLES.get(int(adv_type), f"type {adv_type}") if adv_type is not None else None,
        "model": info.get("model"),
        "firmware": _firmware(info) or None,
        "battery_v": round(int(level) / 1000, 2) if level else None,
        "storage_used_kb": battery.get("used_kb", 0) if battery.get("total_kb") else None,
        "storage_total_kb": battery.get("total_kb"),
        "clock_at": (
            datetime.fromtimestamp(clock, tz=timezone.utc).astimezone() if clock else None
        ),
        "clock_drift_s": (clock - int(time.time())) if clock else None,
        "uptime_s": stats.get("uptime_secs"),
        "noise_floor_dbm": stats.get("noise_floor"),
        "last_rssi_dbm": stats.get("last_rssi"),
        "last_snr_db": stats.get("last_snr"),
        "tx_air_s": stats.get("tx_air_secs"),
        "rx_air_s": stats.get("rx_air_secs"),
        "packets_sent": stats.get("sent"),
        "packets_received": stats.get("recv"),
        "receive_errors": stats.get("recv_errors"),
    }
    return Facts(
        key="info",
        fields=(
            fields.word("name", "name"),
            fields.hexid("public_key", "public_key"),
            fields.word("role", "role"),
            fields.word("model", "model"),
            fields.word("firmware", "firmware"),
            fields.decimal("battery_v", "battery_v", ".2f"),
            fields.integer("storage_used_kb", "storage_used_kb"),
            fields.integer("storage_total_kb", "storage_total_kb"),
            fields.instant("clock_at", "clock_at"),
            _glossed("clock_drift_s", _drift_gloss),
            _glossed("uptime_s", _span_gloss),
            fields.integer("noise_floor_dbm", "noise_floor_dbm"),
            fields.integer("last_rssi_dbm", "last_rssi_dbm"),
            fields.snr("last_snr_db", "last_snr_db"),
            _glossed("tx_air_s", _span_gloss),
            _glossed("rx_air_s", _span_gloss),
            fields.integer("packets_sent", "packets_sent"),
            fields.integer("packets_received", "packets_received"),
            fields.integer("receive_errors", "receive_errors"),
        ),
        values=values,
        omit_absent=True,
    )


def _glossed(key: str, gloss: Callable[[int], str]) -> Column:
    """A seconds reading printed as its own number with a readable span beside it."""
    from ..ui import script
    from ..ui.report import Column, Lane

    def cell(value: Any) -> str:
        if value is None:
            return script.NONE
        return f"{value}  ({gloss(int(value))})"

    return Column(key=key, lanes=(Lane(header=key, render=cell),))


def _span_gloss(seconds: int) -> str:
    """``93784`` → ``1d 2h``: the figure a person can hold in their head."""
    from ..ui import script

    return script.duration(seconds)


def _drift_gloss(seconds: int) -> str:
    """The device clock's error, in the direction a reader would say it."""
    from ..ui import script

    if abs(seconds) < 2:
        return "in sync"
    return f"{script.duration(seconds)} {'fast' if seconds > 0 else 'slow'}"


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
