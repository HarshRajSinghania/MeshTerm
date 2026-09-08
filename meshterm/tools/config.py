"""The ``config`` tool: view and change every device configuration value.

Interactively it launches a full editor (see :mod:`meshterm.ui.config_editor`); on the
CLI it exposes generic key/value subcommands plus backup/restore, channels, custom vars,
and a ``--yes``-gated set of destructive operations. Everything funnels through
:func:`apply_ops`: the editor stages value changes for :meth:`ConfigTool.run` to execute
and log, and the Device actions screen (the sibling ``device-actions`` tool) and the
standalone ``advert`` tool run their immediate operations (adverts, reboot, key
management, factory reset) through the same executor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ..context import AppContext
from ..core.channels import CHANNEL_SLOT_PROBE_CAP
from ..core.config_io import backup_config, plan_restore, read_backup
from ..core.connection import Device, DeviceCommandError
from ..core.device_config import (
    SettingSpec,
    build_snapshot,
    format_value,
    get_spec,
    parse_value,
    settings_by_category,
)
from .base import Tool, ToolResult, register


@register
class ConfigTool(Tool):
    """View and edit the connected device's full configuration."""

    name = "config"
    title = "Device config"
    icon = "🔧"
    help = "Device settings — identity, radio, tuning, …"
    category = "This node"
    order = 20

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Launch the interactive editor and collect the operations to perform.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"ops": [...]}`` to execute, or ``None`` if the user cancelled.
        """
        from ..ui.config_editor import edit_config

        ops = await edit_config(ctx)
        if not ops:
            return None
        return {"ops": ops}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Execute a list of configuration operations against the device.

        Args:
            ctx: Shared application context.
            params: ``ops`` — a list of operation tuples (see module docstring).

        Returns:
            A :class:`ToolResult` summarizing how many values changed.
        """
        device = await ctx.device()
        ops: list[tuple] = list(params.get("ops") or [])
        snapshot = await build_snapshot(device)
        changes, artifacts = await apply_ops(ctx, device, snapshot, ops)

        plural = "" if changes == 1 else "s"
        message = f"[ok]✓[/ok] applied [brand]{changes}[/brand] change{plural}" if changes else None
        return ToolResult(summary={"changes": changes}, message=message, artifacts=artifacts)

    # -- CLI --------------------------------------------------------------------

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``config`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        config_app = typer.Typer(help=self.help, no_args_is_help=False, rich_markup_mode=None)

        @config_app.callback(invoke_without_command=True)
        def _root(ctx: typer.Context) -> None:
            """Show the configuration when no subcommand is given."""
            if ctx.invoked_subcommand is None:
                run_tool_command(self, {"ops": [("show",)]})

        @config_app.command("show", help="Show all current settings")
        def _show_cmd() -> None:
            run_tool_command(self, {"ops": [("show",)]})

        @config_app.command("get", help="Print one setting's value")
        def _get_cmd(key: str = typer.Argument(..., help="Setting key")) -> None:
            run_tool_command(self, {"ops": [("get", key)]})

        @config_app.command("set", help="Set one setting to a value")
        def _set_cmd(
            key: str = typer.Argument(..., help="Setting key"),
            value: str = typer.Argument(..., help="New value"),
        ) -> None:
            run_tool_command(self, {"ops": [("set", key, value)]})

        @config_app.command("backup", help="Write all settings to a TOML file")
        def _backup_cmd(path: Path = typer.Argument(..., help="Destination file")) -> None:
            run_tool_command(self, {"ops": [("backup", path)]})

        @config_app.command("restore", help="Apply settings from a TOML backup")
        def _restore_cmd(
            path: Path = typer.Argument(..., help="Backup file"),
            dry_run: bool = typer.Option(False, "--dry-run", help="Preview without applying"),
        ) -> None:
            run_tool_command(self, {"ops": [("restore", path, dry_run)]})

        @config_app.command("custom", help="Set an experimental custom variable")
        def _custom_cmd(
            key: str = typer.Argument(..., help="Variable name"),
            value: str = typer.Argument(..., help="Variable value"),
        ) -> None:
            run_tool_command(self, {"ops": [("set_custom", key, value)]})

        @config_app.command("channel", help="Configure a channel slot")
        def _channel_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
            name: str = typer.Argument(..., help="Channel name (# derives the secret)"),
            secret: str | None = typer.Option(None, "--secret", help="16-byte hex secret"),
        ) -> None:
            secret_bytes = bytes.fromhex(secret) if secret else None
            run_tool_command(self, {"ops": [("set_channel", index, name, secret_bytes)]})

        @config_app.command("advert", help="Broadcast an advertisement (zero-hop by default)")
        def _advert_cmd(
            flood: bool = typer.Option(False, "--flood", help="Flood across the mesh"),
        ) -> None:
            run_tool_command(self, {"ops": [("advert", flood)]})

        @config_app.command("share", help="Show this node's contact card as a QR code / URI")
        def _share_cmd() -> None:
            run_tool_command(self, {"ops": [("share",)]})

        @config_app.command(
            "advert-cadence",
            help="Set how often this node auto-advertises in the background (0 = off)",
        )
        def _advert_cadence_cmd(
            hours: int = typer.Argument(
                ..., min=0, help="Cadence in hours between background adverts (0 disables)"
            ),
            flood: bool = typer.Option(
                False, "--flood", help="Set the flood cadence (else the zero-hop / direct one)"
            ),
        ) -> None:
            run_tool_command(self, {"ops": [("advert_cadence", flood, hours)]})

        @config_app.command("export-key", help="Export the private key (sensitive)")
        def _export_key_cmd(
            out: Path | None = typer.Option(None, "--out", help="Write to file instead of stdout"),
        ) -> None:
            ops = [("export_key", out)] if out else [("export_key",)]
            run_tool_command(self, {"ops": ops})

        @config_app.command("import-key", help="Import a private key (overwrites identity)")
        def _import_key_cmd(
            key_hex: str = typer.Argument(..., help="Private key as hex"),
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            _require_yes(yes, "import-key overwrites the device identity")
            run_tool_command(self, {"ops": [("import_key", key_hex)]})

        @config_app.command("sync-clock", help="Set the device clock from this computer")
        def _sync_clock_cmd() -> None:
            run_tool_command(self, {"ops": [("sync_clock",)]})

        @config_app.command("reboot", help="Reboot the device")
        def _reboot_cmd(
            yes: bool = typer.Option(False, "--yes", help="Confirm reboot"),
        ) -> None:
            _require_yes(yes, "reboot restarts the device")
            run_tool_command(self, {"ops": [("reboot",)]})

        @config_app.command("factory-reset", help="Erase all data and reset to defaults")
        def _factory_reset_cmd(
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            _require_yes(yes, "factory-reset erases ALL data on the device")
            run_tool_command(self, {"ops": [("factory_reset",)]})

        app.add_typer(config_app, name=self.name)


# -- op execution (shared by the tool and the interactive editor) -------------


async def apply_ops(
    ctx: AppContext, device: Device, snapshot: dict, ops: list[tuple]
) -> tuple[int, list[str]]:
    """Execute a list of configuration operation tuples against ``device``.

    This is the single executor for every config operation, used both by
    :meth:`ConfigTool.run` (for staged, applied changes) and by the interactive editor's
    device actions (adverts, reboot, backup/restore, key management, factory reset —
    which run immediately rather than staging).

    Args:
        ctx: Shared application context (for console output).
        device: The connected device to act on.
        snapshot: The current device snapshot; updated in place as settings change so
            coupled commands rebuild from current values.
        ops: Operation tuples (see the module docstring).

    Returns:
        A ``(changes, artifacts)`` pair: the number of value changes applied and any
        file paths produced.
    """
    changes = 0
    artifacts: list[str] = []
    for op in ops:
        kind = op[0]
        if kind == "show":
            await _show(ctx, device, snapshot)
        elif kind == "get":
            # The bare value, nothing else: the caller named the key, so repeating it back
            # is one more thing for `$(meshterm config get name)` to strip off. The same
            # shape `sysctl -n` and `git config --get` print.
            spec = get_spec(op[1])
            ctx.ui.note(_script_value(spec, spec.getter(snapshot)))
        elif kind == "set":
            changes += await _apply_setting(ctx, device, op[1], op[2], snapshot)
        elif kind == "set_custom":
            await device.set_custom_var(op[1], op[2])
            ctx.ui.ack(f"[ok]✓[/ok] custom [brand]{op[1]}[/brand] = {op[2]}")
            changes += 1
        elif kind == "set_channel":
            await device.set_channel(op[1], op[2], op[3])
            ctx.ui.ack(f"[ok]✓[/ok] channel {op[1]} = [brand]{op[2]}[/brand]")
            changes += 1
        elif kind == "backup":
            artifacts.append(str(await _backup(device, snapshot, op[1])))
        elif kind == "restore":
            changes += await _restore(ctx, device, snapshot, op[1], op[2])
        elif kind == "advert":
            flood = len(op) > 1 and bool(op[1])
            await device.send_advert(flood)
            # A manual advert resets the background scheduler's countdown for its type,
            # so the next scheduled send counts from this one (see AdvertScheduler).
            public_key = str(snapshot.get("public_key") or "")
            if public_key:
                ctx.advert_store.mark_sent(public_key, flood=flood)
            kind_label = "flood" if flood else "zero-hop"
            ctx.ui.ack(f"[ok]✓[/ok] {kind_label} advertisement sent")
        elif kind == "advert_cadence":
            from ..core.advert_store import cadence_label

            flood, hours = bool(op[1]), int(op[2])
            public_key = str(snapshot.get("public_key") or "")
            if public_key:
                ctx.advert_store.set_cadence(public_key, flood=flood, hours=hours)
                kind_label = "flood" if flood else "zero-hop"
                ctx.ui.ack(
                    f"[ok]✓[/ok] background {kind_label} advert: "
                    f"[brand]{cadence_label(hours)}[/brand]"
                )
                changes += 1
            else:  # pragma: no cover - SELF_INFO always carries the key on real firmware
                raise DeviceCommandError(
                    "the device reported no public key — the advert cadence was not saved"
                )
        elif kind == "share":
            _share_contact(ctx, snapshot)
        elif kind == "sync_clock":
            import time as _time
            from datetime import datetime

            epoch = int(_time.time())
            await device.set_time(epoch)
            stamp = datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            ctx.ui.ack(f"[ok]✓[/ok] device clock set to [brand]{stamp}[/brand]")
            changes += 1
        elif kind == "reboot":
            await device.reboot()
            ctx.ui.ack("[warn]device rebooting[/warn]")
        elif kind == "export_key":
            await _export_key(ctx, device, op[1] if len(op) > 1 else None, artifacts)
        elif kind == "import_key":
            await device.import_private_key(op[1])
            ctx.ui.ack("[ok]✓[/ok] private key imported")
            changes += 1
        elif kind == "factory_reset":
            await device.factory_reset()
            ctx.ui.ack("[err]device factory-reset[/err]")
        else:  # pragma: no cover - guarded by the call sites that build ops
            raise DeviceCommandError(f"unknown config operation: {kind}")
    # Drop any session-cached facts these ops may have changed, so the next screen re-reads
    # the truth rather than a stale copy (see meshterm.services.device_state.DeviceState). A
    # reboot/reconnect resets the whole cache on its own; these cover the in-place edits.
    kinds = {op[0] for op in ops}
    _WHOLESALE = {"restore", "import_key", "factory_reset"}
    if kinds & _WHOLESALE:
        ctx.devstate.reset()
    else:
        if "set" in kinds:
            ctx.devstate.invalidate_config()  # self-info fields + path-hash mode
        if "set_channel" in kinds:
            ctx.devstate.invalidate_channels()
    return changes, artifacts


async def _apply_setting(
    ctx: AppContext, device: Device, key: str, raw: Any, snapshot: dict
) -> int:
    """Parse, apply and record one setting; keep ``snapshot`` consistent.

    The local snapshot is updated with the new value so a later coupled change in the
    same batch (e.g. another radio field) is rebuilt from current values.

    Returns:
        ``1`` (a change was applied).
    """
    spec = get_spec(key)
    value = parse_value(spec, raw, snapshot)
    await spec.apply(device, value, snapshot)
    snapshot[key] = value
    # Remember what we set, keyed by the device's own public key, so a forgetful device (a
    # firmware-less radio bridge) can be offered its settings back on the next connect (see
    # meshterm.core.settings_store). Provenance-gated: only values changed through MeshTerm.
    if ctx.settings_store is not None:
        ctx.settings_store.remember(str(snapshot.get("public_key") or ""), key, value)
    ctx.ui.ack(f"[ok]✓[/ok] [brand]{key}[/brand] = {format_value(spec, value)}")
    return 1


async def _show(ctx: AppContext, device: Device, snapshot: dict) -> None:
    """Print every current setting, plus any custom variables.

    The menu shows the annotated table; a scripted run gets one ``key value`` line per
    setting, keyed exactly as ``config get`` and ``config set`` name them, so a line read
    out of ``show`` can be typed straight back in. The labels and descriptions the table
    carries are for a reader choosing a setting, and this reader has already chosen.

    The pairing PIN stays concealed here, as it is in the table: this is the whole-device
    dump, the thing that gets redirected into a file and pasted into a bug report, and the
    PIN is the one value on it that lets someone else's phone onto the radio.
    ``config get device_pin`` names it deliberately.
    """
    from ..ui import script
    from ..ui.config_editor import PIN_KEY, conceal, config_table
    from ..ui.surface import TuiUi

    custom = await device.get_custom_vars()
    if isinstance(ctx.ui, TuiUi):
        ctx.ui.show(config_table(snapshot, custom))
        return

    rows: list[tuple[str, str]] = []
    for _category, specs in settings_by_category():
        for spec in specs:
            value = _script_value(spec, spec.getter(snapshot))
            rows.append((spec.key, conceal(value) if spec.key == PIN_KEY else value))
    # Custom variables are experimental firmware fields with no spec, so they are namespaced
    # rather than mixed in — a reader can tell which lines `config set` will take.
    rows.extend((f"custom.{key}", value) for key, value in sorted(custom.items()))
    ctx.ui.show(script.pairs(rows))


def _script_value(spec: SettingSpec, value: Any) -> str:
    """A setting's value in the form ``config set`` accepts back.

    :func:`~meshterm.core.device_config.format_value` writes for a reader choosing a
    setting: an enum reads ``0 (off)``, an unset string reads ``(not set)``, an unreported
    one reads ``?``. None of those three survive a round trip —
    :func:`~meshterm.core.device_config.parse_value` wants a bare integer for an enum, and
    would take the words ``(not set)`` as the literal value of a string. So the scripted
    dump prints what can be typed back: the enum's number, the empty string as ``""``, and
    :data:`~meshterm.ui.script.NONE` for a value the firmware never reported (which is an
    absence, not a value, and is the one line ``config set`` should not be handed back).

    Args:
        spec: The setting the value belongs to.
        value: Its current value, or ``None`` when the device did not report it.

    Returns:
        The scripted rendering.
    """
    from ..ui import script

    if value is None:
        return script.NONE
    if spec.value_type == "bool":
        return "true" if value else "false"
    if spec.value_type == "str" and value == "":
        return '""'
    return str(value)


def _share_contact(ctx: AppContext, snapshot: dict) -> None:
    """Render this node's contact card — the QR code in the menu, the URI alone on the CLI.

    The QR is for a phone pointed at the screen. Redirected into a file it is a block of
    block characters around the one thing that is the answer, so a scripted run prints the
    link by itself.
    """
    from rich.console import Group
    from rich.text import Text

    from ..ui.config_editor import contact_share_url
    from ..ui.qr import qr_text
    from ..ui.surface import TuiUi

    public_key = str(snapshot.get("public_key") or "")
    if not public_key:
        raise DeviceCommandError("the device did not report a public key — nothing to share")
    name = str(snapshot.get("name") or "this node")
    url = contact_share_url(name, public_key, int(snapshot.get("adv_type") or 1))
    if isinstance(ctx.ui, TuiUi):
        ctx.ui.show(Group(qr_text(url), Text(""), Text(url, style="accent")))
    else:
        ctx.ui.note(url)


async def _backup(device: Device, snapshot: dict, path: Path) -> Path:
    """Write a TOML backup of the current configuration."""
    custom = await device.get_custom_vars()
    channels = await _read_channels(device)
    return backup_config(Path(path), snapshot, custom, channels)


async def _restore(
    ctx: AppContext, device: Device, snapshot: dict, path: Path, dry_run: bool
) -> int:
    """Apply (or preview) a backup file against the current configuration.

    Returns:
        The number of changes applied (always ``0`` for a dry run).
    """
    backup = read_backup(Path(path))
    custom = await device.get_custom_vars()
    ops = plan_restore(backup, snapshot, custom)
    if not ops:
        ctx.ui.ack("[muted]restore: device already matches the backup.[/muted]")
        return 0

    if dry_run:
        from ..ui import script

        table = script.columns("OPERATION", "TARGET", "VALUE")
        for op in ops:
            table.add_row(op[0], str(op[1]), str(op[2]) if len(op) > 2 else script.NONE)
        ctx.ui.show(table)
        ctx.ui.ack("[muted]dry run — nothing was changed.[/muted]")
        return 0

    changes = 0
    for op in ops:
        if op[0] == "set":
            changes += await _apply_setting(ctx, device, op[1], op[2], snapshot)
        elif op[0] == "set_custom":
            await device.set_custom_var(op[1], op[2])
            changes += 1
        elif op[0] == "set_channel":
            await device.set_channel(op[1], op[2], op[3])
            changes += 1
    return changes


async def _export_key(
    ctx: AppContext, device: Device, out: Path | None, artifacts: list[str]
) -> None:
    """Export the private key, to a file if ``out`` is given, else to the console."""
    key_hex = await device.export_private_key()
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key_hex, encoding="utf-8")
        artifacts.append(str(path))
        ctx.ui.ack("[warn]private key written — keep this file secret.[/warn]")
    else:
        # The key itself is the answer, so it is output rather than an acknowledgement:
        # `meshterm config export-key > key.hex` should hold the key and nothing else. The
        # warning that comes with it belongs beside it on screen, not in the file.
        ctx.ui.ack("[warn]private key (keep secret):[/warn]")
        ctx.ui.note(f"[muted]{key_hex}[/muted]")


async def _read_channels(device: Device) -> list[dict]:
    """Probe channel slots and return the configured ones."""
    channels: list[dict] = []
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            ch = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - firmware may not support channel reads
            break
        if ch:
            channels.append(ch)
    return channels


def _require_yes(yes: bool, what: str) -> None:
    """Abort a destructive CLI command unless ``--yes`` was passed.

    Args:
        yes: Whether the user passed ``--yes``.
        what: Human-readable description of the consequence.

    Raises:
        typer.Exit: With code 1 if confirmation was not given.
    """
    if not yes:
        typer.secho(f"Refusing: {what}. Re-run with --yes to confirm.", fg="red")
        raise typer.Exit(1)
