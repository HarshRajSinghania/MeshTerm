"""Interactive device-configuration editor (questionary + Rich).

Drives the menu flow for the ``config`` tool: it shows the device's current configuration,
lets the user stage changes setting-by-setting (with type-aware prompts and validation),
and handles custom variables, channels, backup/restore and the destructive "danger zone".
It returns an operation list for :class:`~meshtools.tools.config.ConfigTool` to execute and
log; it performs no device writes itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import questionary
from rich.console import Console
from rich.table import Table

from ..context import AppContext
from ..core.device_config import (
    DeviceConfigError,
    SettingSpec,
    build_snapshot,
    format_value,
    parse_value,
    settings_by_category,
)
from .menu import _MENU_STYLE

# Menu action sentinels (distinct from setting keys, which are plain strings).
_PRESETS = "__presets__"
_CUSTOM = "__custom__"
_CHANNELS = "__channels__"
_BACKUP = "__backup__"
_RESTORE = "__restore__"
_DANGER = "__danger__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"
# Sentinel for the "enter a value myself" option on non-strict enum prompts.
_OTHER = "__other__"


async def edit_config(ctx: AppContext) -> Optional[list[tuple]]:
    """Run the interactive editor and return the operations to perform.

    Args:
        ctx: Shared application context (provides the connected device and console).

    Returns:
        A list of operation tuples for the tool to execute, or ``None`` if the user
        cancelled without choosing to apply anything.
    """
    console = ctx.console
    device = await ctx.device()
    snapshot = await build_snapshot(device)
    custom = await device.get_custom_vars()

    pending: dict[str, Any] = {}  # setting key -> staged new value
    extra_ops: list[tuple] = []  # custom/channel/backup/restore/danger ops, in order

    while True:
        render_config(console, snapshot, custom, pending)
        choice = await _main_menu(snapshot, pending, extra_ops)

        if choice in (None, _CANCEL):
            return None
        if choice == _APPLY:
            ops: list[tuple] = [("set", k, v) for k, v in pending.items()]
            ops.extend(extra_ops)
            return ops or None
        if choice == _PRESETS:
            await _stage_preset(console, pending)
        elif choice == _CUSTOM:
            await _stage_custom_var(console, custom, extra_ops)
        elif choice == _CHANNELS:
            await _stage_channel(console, extra_ops)
        elif choice == _BACKUP:
            await _stage_backup(extra_ops)
        elif choice == _RESTORE:
            await _stage_restore(extra_ops)
        elif choice == _DANGER:
            # Danger-zone actions run immediately (after confirmation), not on Apply.
            if await _danger_zone(ctx, device, snapshot):
                snapshot = await build_snapshot(device)  # state may have changed
                custom = await device.get_custom_vars()
        else:  # a setting key
            await _stage_setting(console, choice, snapshot, pending)


# --- rendering ---------------------------------------------------------------


def render_config(
    console: Console,
    snapshot: dict,
    custom: dict[str, str],
    pending: Optional[dict[str, Any]] = None,
) -> None:
    """Print the current configuration, overlaying any staged changes.

    Args:
        console: Console to render into.
        snapshot: Device snapshot from ``build_snapshot``.
        custom: Current custom variables.
        pending: Optional staged changes (setting key -> new value).
    """
    pending = pending or {}
    show_staged = bool(pending)
    table = Table(title="Device configuration", border_style="muted", expand=False)
    table.add_column("Setting", style="muted")
    table.add_column("Current")
    if show_staged:
        table.add_column("Staged", style="warn")
    table.add_column("Description", style="muted")
    for category, specs in settings_by_category():
        table.add_section()
        header = [f"[accent]── {category} ──[/accent]", ""]
        if show_staged:
            header.append("")
        table.add_row(*header, "")
        for spec in specs:
            current = format_value(spec, spec.getter(snapshot))
            row = [f"{spec.label} [muted]({spec.key})[/muted]", current]
            if show_staged:
                row.append(format_value(spec, pending[spec.key]) if spec.key in pending else "")
            table.add_row(*row, spec.help)
    console.print(table)
    if custom:
        console.print(
            "[muted]custom vars:[/muted] "
            + ", ".join(f"{k}={v}" for k, v in custom.items())
        )


async def _main_menu(snapshot: dict, pending: dict, extra_ops: list) -> Optional[str]:
    """Show the top-level editor menu and return the chosen action or setting key.

    Each setting row shows its current value (and any staged new value) plus a one-line
    explanation, so the user can see and understand what they're changing in place.
    """
    choices: list[questionary.Choice | questionary.Separator] = []
    for category, specs in settings_by_category():
        choices.append(questionary.Separator(f"── {category} ──"))
        for spec in specs:
            current = format_value(spec, spec.getter(snapshot))
            shown = (
                f"{current} → {format_value(spec, pending[spec.key])}"
                if spec.key in pending
                else current
            )
            choices.append(
                questionary.Choice(
                    title=f"{spec.label}: {shown}  —  {spec.help}", value=spec.key
                )
            )
    choices.append(questionary.Separator("── More ──"))
    choices.append(questionary.Choice(title="Radio presets (standard configs)", value=_PRESETS))
    choices.append(questionary.Choice(title="Custom / experimental vars", value=_CUSTOM))
    choices.append(questionary.Choice(title="Channels", value=_CHANNELS))
    choices.append(questionary.Choice(title="Backup to file", value=_BACKUP))
    choices.append(questionary.Choice(title="Restore from file", value=_RESTORE))
    choices.append(questionary.Choice(title="⚠ Danger zone", value=_DANGER))
    choices.append(questionary.Separator(" "))
    staged = len(pending) + len(extra_ops)
    choices.append(questionary.Choice(title=f"✓ Apply ({staged} staged)", value=_APPLY))
    choices.append(questionary.Choice(title="Cancel (discard)", value=_CANCEL))

    return await questionary.select(
        "Edit which setting?", choices=choices, style=_MENU_STYLE, qmark="◆"
    ).ask_async()


# --- staging individual changes ----------------------------------------------


async def _stage_setting(
    console: Console, key: str, snapshot: dict, pending: dict[str, Any]
) -> None:
    """Prompt for one setting's new value and stage it."""
    from ..core.device_config import get_spec

    spec = get_spec(key)
    current = pending.get(key, spec.getter(snapshot))
    value = await _prompt_value(spec, current)
    if value is not None:
        pending[key] = value


async def _prompt_value(spec: SettingSpec, current: Any) -> Any:
    """Prompt for a typed value for ``spec``, returning ``None`` on cancel."""
    if spec.value_type == "bool":
        return await questionary.confirm(
            f"{spec.label}?", default=bool(current)
        ).ask_async()

    if spec.value_type == "enum" and spec.choices is not None:
        items: list[questionary.Choice] = [
            questionary.Choice(title=f"{k} — {label}", value=k)
            for k, label in spec.choices.items()
        ]
        # Non-strict enums list the common values for convenience but still accept any
        # in-range integer, so offer an escape hatch to type one in.
        if not spec.strict_choices:
            items.append(questionary.Choice(title="Other (enter a value)…", value=_OTHER))
        selected = await questionary.select(
            f"{spec.label} — {spec.help}",
            choices=items,
            default=current if current in spec.choices else None,
            style=_MENU_STYLE,
        ).ask_async()
        if selected != _OTHER:
            return selected
        # else: fall through to the free-text prompt below.

    def validate(text: str) -> bool | str:
        try:
            parse_value(spec, text)
            return True
        except DeviceConfigError as exc:
            return str(exc)

    raw = await questionary.text(
        f"{spec.label} — {spec.help}",
        default="" if current is None else str(current),
        validate=validate,
    ).ask_async()
    return None if raw is None else parse_value(spec, raw)


async def _stage_preset(console: Console, pending: dict[str, Any]) -> None:
    """Pick a standard radio preset and stage all of its fields for review/apply."""
    from ..core.device_config import RADIO_PRESETS

    choices = [
        questionary.Choice(
            title=f"{p.name}: {p.freq} MHz, BW {p.bw}, SF{p.sf}, CR{p.cr}  —  {p.help}",
            value=i,
        )
        for i, p in enumerate(RADIO_PRESETS)
    ]
    choices.append(questionary.Choice(title="Cancel", value=None))
    idx = await questionary.select(
        "Apply which radio preset?", choices=choices, style=_MENU_STYLE
    ).ask_async()
    if idx is None:
        return
    preset = RADIO_PRESETS[idx]
    pending.update(preset.as_settings())
    console.print(
        f"[muted]staged preset[/muted] [brand]{preset.name}[/brand] "
        "[muted](review the radio rows, then Apply)[/muted]"
    )


async def _stage_custom_var(
    console: Console, custom: dict[str, str], extra_ops: list[tuple]
) -> None:
    """Prompt for a custom/experimental variable and stage a set operation."""
    key = await questionary.text("Custom variable name:").ask_async()
    if not key:
        return
    value = await questionary.text(
        f"Value for {key}:", default=custom.get(key, "")
    ).ask_async()
    if value is None:
        return
    extra_ops.append(("set_custom", key.strip(), value))


async def _stage_channel(console: Console, extra_ops: list[tuple]) -> None:
    """Prompt for a channel slot's name/secret and stage a set operation."""
    idx_raw = await questionary.text(
        "Channel index:", default="0", validate=_is_int
    ).ask_async()
    if idx_raw is None:
        return
    name = await questionary.text(
        "Channel name (leading # derives the secret from the name):"
    ).ask_async()
    if not name:
        return
    secret_hex = await questionary.text(
        "Secret (32 hex chars / 16 bytes; blank to derive from name):",
        validate=_is_optional_secret,
    ).ask_async()
    secret = bytes.fromhex(secret_hex) if secret_hex else None
    extra_ops.append(("set_channel", int(idx_raw), name, secret))


async def _stage_backup(extra_ops: list[tuple]) -> None:
    """Prompt for a backup destination path and stage the operation."""
    path = await questionary.path("Write backup to:", default="meshtools-config.toml").ask_async()
    if path:
        extra_ops.append(("backup", Path(path)))


async def _stage_restore(extra_ops: list[tuple]) -> None:
    """Prompt for a backup file and whether to preview, then stage the operation."""
    path = await questionary.path("Restore from:").ask_async()
    if not path:
        return
    dry_run = await questionary.confirm(
        "Preview changes only (dry run)?", default=True
    ).ask_async()
    extra_ops.append(("restore", Path(path), bool(dry_run)))


async def _danger_zone(ctx: AppContext, device: Any, snapshot: dict) -> bool:
    """Sub-menu for destructive operations, run immediately after confirmation.

    Unlike ordinary settings (which are staged and applied together), danger-zone actions
    have no meaningful "preview" and are executed the moment they're confirmed.

    Returns:
        ``True`` if the action may have changed device state the editor should re-read
        (e.g. a factory reset or key import), so the caller can refresh its snapshot.
    """
    from ..tools.config import apply_ops

    console = ctx.console
    action = await questionary.select(
        "⚠ Danger zone (runs immediately on confirmation):",
        choices=[
            questionary.Choice("Send advert", value="advert"),
            questionary.Choice("Reboot device", value="reboot"),
            questionary.Choice("Export private key", value="export_key"),
            questionary.Choice("Import private key", value="import_key"),
            questionary.Choice("Factory reset (erase all)", value="factory_reset"),
            questionary.Choice("Back", value=None),
        ],
        style=_MENU_STYLE,
    ).ask_async()

    op: Optional[tuple] = None
    if action in (None, "Back"):
        return False
    if action in ("advert", "export_key"):
        op = (action,)
    elif action == "import_key":
        key_hex = await questionary.text("Private key (hex):", validate=_is_hex).ask_async()
        if key_hex and await _confirm_typed(console, "IMPORT"):
            op = ("import_key", key_hex.strip())
    elif action == "reboot":
        if await questionary.confirm("Reboot the device now?", default=False).ask_async():
            op = ("reboot",)
    elif action == "factory_reset":
        console.print("[err]This erases ALL data on the device and cannot be undone.[/err]")
        if await _confirm_typed(console, "RESET"):
            op = ("factory_reset",)

    if op is None:
        return False
    await apply_ops(ctx, device, snapshot, [op])
    return action in ("factory_reset", "import_key")


async def _confirm_typed(console: Console, word: str) -> bool:
    """Require the user to type ``word`` exactly to confirm a destructive action."""
    typed = await questionary.text(f"Type {word!r} to confirm:").ask_async()
    if typed == word:
        return True
    console.print("[muted]confirmation did not match; skipped.[/muted]")
    return False


# --- validators --------------------------------------------------------------


def _is_int(text: str) -> bool | str:
    """Validate that ``text`` is an integer."""
    try:
        int(text)
        return True
    except ValueError:
        return "Enter a whole number."


def _is_hex(text: str) -> bool | str:
    """Validate that ``text`` is a hex string."""
    try:
        bytes.fromhex(text)
        return True
    except ValueError:
        return "Enter hex characters only."


def _is_optional_secret(text: str) -> bool | str:
    """Validate an optional 16-byte hex channel secret."""
    if text == "":
        return True
    try:
        return len(bytes.fromhex(text)) == 16 or "Secret must be exactly 16 bytes (32 hex chars)."
    except ValueError:
        return "Enter hex characters only."
