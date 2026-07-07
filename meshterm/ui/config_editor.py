"""Interactive device-configuration editor, rendered in the full-screen session.

Drives the menu flow for the ``config`` tool: it presents the device's settings (each row
showing its current value and any staged change), lets the user stage changes setting-by-
setting with type-aware prompts and validation, and handles custom variables,
backup/restore and the destructive "danger zone". (Channels have their own first-class
manager — see the ``channels`` tool.) It returns an operation list for
:class:`~meshterm.tools.config.ConfigTool` to execute and log; it performs no device writes
itself (except danger-zone actions, which run immediately and show their result at once).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.device_config import (
    DeviceConfigError,
    SettingSpec,
    build_snapshot,
    format_value,
    parse_value,
    settings_by_category,
)
from .tui import Choice, Separator

# Menu action sentinels (distinct from setting keys, which are plain strings).
_PRESETS = "__presets__"
_CUSTOM = "__custom__"
_BACKUP = "__backup__"
_RESTORE = "__restore__"
_DANGER = "__danger__"
_VIEW = "__view__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"
# Sentinel for the "enter a value myself" option on non-strict enum prompts.
_OTHER = "__other__"


async def edit_config(ctx: AppContext) -> Optional[list[tuple]]:
    """Run the interactive editor and return the operations to perform.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).

    Returns:
        A list of operation tuples for the tool to execute, or ``None`` if the user
        cancelled without choosing to apply anything.
    """
    device = await ctx.device()
    snapshot = await build_snapshot(device)
    custom = await device.get_custom_vars()

    pending: dict[str, Any] = {}  # setting key -> staged new value
    extra_ops: list[tuple] = []  # custom/channel/backup/restore/danger ops, in order

    while True:
        choice = await _main_menu(ctx, snapshot, pending, extra_ops)

        if choice in (None, _CANCEL):
            return None
        if choice == _APPLY:
            ops: list[tuple] = [("set", k, v) for k, v in pending.items()]
            ops.extend(extra_ops)
            return ops or None
        if choice == _VIEW:
            await ctx.ui.view(
                config_table(snapshot, custom, pending), title="Device configuration"
            )
        elif choice == _PRESETS:
            await _stage_preset(ctx, pending)
        elif choice == _CUSTOM:
            await _stage_custom_var(ctx, custom, extra_ops)
        elif choice == _BACKUP:
            await _stage_backup(ctx, extra_ops)
        elif choice == _RESTORE:
            await _stage_restore(ctx, extra_ops)
        elif choice == _DANGER:
            # Danger-zone actions run immediately (after confirmation), not on Apply.
            if await _danger_zone(ctx, device, snapshot):
                snapshot = await build_snapshot(device)  # state may have changed
                custom = await device.get_custom_vars()
        else:  # a setting key
            await _stage_setting(ctx, choice, snapshot, pending)


# --- rendering ---------------------------------------------------------------


def config_table(
    snapshot: dict,
    custom: dict[str, str],
    pending: Optional[dict[str, Any]] = None,
) -> Table:
    """Build the full configuration table, overlaying any staged changes.

    Args:
        snapshot: Device snapshot from ``build_snapshot``.
        custom: Current custom variables.
        pending: Optional staged changes (setting key -> new value).

    Returns:
        A Rich :class:`Table` of every setting's current (and staged) value plus custom
        variables, ready to hand to ``ctx.ui.show`` / ``ctx.ui.view``.
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
    if custom:
        table.add_section()
        cols = 4 if show_staged else 3
        table.add_row(
            "[accent]── Custom ──[/accent]", *([""] * (cols - 1))
        )
        for key, value in custom.items():
            row = [f"custom [muted]({key})[/muted]", value]
            if show_staged:
                row.append("")
            table.add_row(*row, "")
    return table


async def _main_menu(
    ctx: AppContext, snapshot: dict, pending: dict, extra_ops: list
) -> Optional[str]:
    """Show the top-level editor menu and return the chosen action or setting key.

    Each setting row shows its current value (and any staged new value) plus a one-line
    explanation, so the user can see and understand what they're changing in place.
    """
    items: list = []
    for category, specs in settings_by_category():
        items.append(Separator(f"── {category} ──"))
        for spec in specs:
            current = format_value(spec, spec.getter(snapshot))
            shown = (
                f"{current} → {format_value(spec, pending[spec.key])}"
                if spec.key in pending
                else current
            )
            items.append(
                Choice(title=f"{spec.label}: {shown}  —  {spec.help}", value=spec.key)
            )
    items.append(Separator("── More ──"))
    items.append(Choice(title="View current config (full table)", value=_VIEW))
    items.append(Choice(title="Radio presets (standard configs)", value=_PRESETS))
    items.append(Choice(title="Custom / experimental vars", value=_CUSTOM))
    items.append(Choice(title="Backup to file", value=_BACKUP))
    items.append(Choice(title="Restore from file", value=_RESTORE))
    items.append(Choice(title="⚠ Danger zone", value=_DANGER))
    items.append(Separator(" "))
    staged = len(pending) + len(extra_ops)
    items.append(Choice(title=f"✓ Apply ({staged} staged)", value=_APPLY))
    items.append(Choice(title="Cancel (discard)", value=_CANCEL))

    choice = await ctx.ui.select("Edit which setting?", items)
    return _CANCEL if choice is None else choice


# --- staging individual changes ----------------------------------------------


async def _stage_setting(
    ctx: AppContext, key: str, snapshot: dict, pending: dict[str, Any]
) -> None:
    """Prompt for one setting's new value and stage it."""
    from ..core.device_config import get_spec

    spec = get_spec(key)
    current = pending.get(key, spec.getter(snapshot))
    value = await _prompt_value(ctx, spec, current)
    if value is not None:
        pending[key] = value


async def _prompt_value(ctx: AppContext, spec: SettingSpec, current: Any) -> Any:
    """Prompt for a typed value for ``spec``, returning ``None`` on cancel."""
    if spec.value_type == "bool":
        return await ctx.ui.confirm(f"{spec.label}?", default=bool(current))

    if spec.value_type == "enum" and spec.choices is not None:
        items: list = [
            Choice(title=f"{k} — {label}", value=k) for k, label in spec.choices.items()
        ]
        # Non-strict enums list the common values for convenience but still accept any
        # in-range integer, so offer an escape hatch to type one in.
        if not spec.strict_choices:
            items.append(Choice(title="Other (enter a value)…", value=_OTHER))
        selected = await ctx.ui.select(
            f"{spec.label} — {spec.help}",
            items,
            default=current if current in spec.choices else None,
        )
        if selected is None:
            return None
        if selected != _OTHER:
            return selected
        # else: fall through to the free-text prompt below.

    def validate(text: str) -> bool | str:
        try:
            parse_value(spec, text)
            return True
        except DeviceConfigError as exc:
            return str(exc)

    raw = await ctx.ui.text(
        f"{spec.label} — {spec.help}",
        default="" if current is None else str(current),
        validate=validate,
    )
    return None if raw is None else parse_value(spec, raw)


async def _stage_preset(ctx: AppContext, pending: dict[str, Any]) -> None:
    """Pick a standard radio preset and stage all of its fields for review/apply."""
    from ..core.device_config import RADIO_PRESETS

    items: list = [
        Choice(
            title=f"{p.name}: {p.freq} MHz, BW {p.bw}, SF{p.sf}, CR{p.cr}  —  {p.help}",
            value=i,
        )
        for i, p in enumerate(RADIO_PRESETS)
    ]
    items.append(Separator(" "))
    items.append(Choice(title="Back", value=None))
    idx = await ctx.ui.select("Apply which radio preset?", items)
    if idx is None:
        return
    preset = RADIO_PRESETS[idx]
    pending.update(preset.as_settings())


async def _stage_custom_var(
    ctx: AppContext, custom: dict[str, str], extra_ops: list[tuple]
) -> None:
    """Prompt for a custom/experimental variable and stage a set operation."""
    key = await ctx.ui.text("Custom variable name:")
    if not key:
        return
    value = await ctx.ui.text(f"Value for {key}:", default=custom.get(key, ""))
    if value is None:
        return
    extra_ops.append(("set_custom", key.strip(), value))


async def _stage_backup(ctx: AppContext, extra_ops: list[tuple]) -> None:
    """Prompt for a backup destination path and stage the operation."""
    path = await ctx.ui.path("Write backup to:", default="meshterm-config.toml")
    if path:
        extra_ops.append(("backup", Path(path)))


async def _stage_restore(ctx: AppContext, extra_ops: list[tuple]) -> None:
    """Prompt for a backup file and whether to preview, then stage the operation."""
    path = await ctx.ui.path("Restore from:")
    if not path:
        return
    dry_run = await ctx.ui.confirm("Preview changes only (dry run)?", default=True)
    if dry_run is None:
        return
    extra_ops.append(("restore", Path(path), bool(dry_run)))


async def _danger_zone(ctx: AppContext, device: Any, snapshot: dict) -> bool:
    """Sub-menu for destructive operations, run immediately after confirmation.

    Unlike ordinary settings (which are staged and applied together), danger-zone actions
    have no meaningful "preview" and are executed the moment they're confirmed; their
    output is shown at once in a result window.

    Returns:
        ``True`` if the action may have changed device state the editor should re-read
        (e.g. a factory reset or key import), so the caller can refresh its snapshot.
    """
    from ..tools.config import apply_ops

    action = await ctx.ui.select(
        "⚠ Danger zone (runs immediately on confirmation):",
        [
            Choice("Send advert", value="advert"),
            Choice("Reboot device", value="reboot"),
            Choice("Export private key", value="export_key"),
            Choice("Import private key", value="import_key"),
            Choice("Factory reset (erase all)", value="factory_reset"),
            Separator(" "),
            Choice("Back", value=None),
        ],
    )

    op: Optional[tuple] = None
    if action in (None, "Back"):
        return False
    if action in ("advert", "export_key"):
        op = (action,)
    elif action == "import_key":
        key_hex = await ctx.ui.text("Private key (hex):", validate=_is_hex)
        if key_hex and await _confirm_typed(ctx, "IMPORT"):
            op = ("import_key", key_hex.strip())
    elif action == "reboot":
        if await ctx.ui.confirm("Reboot the device now?", default=False):
            op = ("reboot",)
    elif action == "factory_reset":
        if await _confirm_typed(
            ctx,
            "RESET",
            warning="This erases ALL data on the device and cannot be undone.",
        ):
            op = ("factory_reset",)

    if op is None:
        return False
    await apply_ops(ctx, device, snapshot, [op])
    await ctx.ui.present(title="danger zone")  # show the immediate result now
    return action in ("factory_reset", "import_key")


async def _confirm_typed(ctx: AppContext, word: str, *, warning: str = "") -> bool:
    """Require the user to type ``word`` exactly to confirm a destructive action."""
    typed = await ctx.ui.text(
        f"Type {word!r} to confirm:", help_text=warning, validate=None
    )
    if typed == word:
        return True
    await ctx.ui.view(
        Text.from_markup("[muted]confirmation did not match; skipped.[/muted]"),
        title="cancelled",
    )
    return False


# --- validators --------------------------------------------------------------


def _is_hex(text: str) -> bool | str:
    """Validate that ``text`` is a hex string."""
    try:
        bytes.fromhex(text)
        return True
    except ValueError:
        return "Enter hex characters only."
