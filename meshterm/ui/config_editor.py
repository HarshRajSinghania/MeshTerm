"""Interactive device-configuration editor, rendered in the full-screen session.

Drives the menu flow for the ``config`` tool. The screen is one grouped list: every
setting under its category heading (each row showing its current value, any staged change,
and a one-line explanation), followed by a *Device actions* section for the operations that
act on the box itself rather than a value — adverts, reboot, backup/restore, the identity
key, and factory reset.

Two kinds of interaction live here, deliberately kept distinct:

* **Settings are staged.** Editing a row stages the new value (shown as ``current → new``
  in the row) and nothing touches the radio until *Apply*; backing out with staged changes
  asks before discarding them. The editor returns the staged operations for
  :class:`~meshterm.tools.config.ConfigTool` to execute and log.
* **Device actions run immediately** (after their own confirmation dialog — destructive
  ones gate behind typing a confirmation word). They have no meaningful "preview", so their
  result is shown at once.

Multiple-choice values are picked in dialogs (booleans as an On/Off button pair, enums as
a floating select), the node's location can be set by pointing at the full-screen map (see
:class:`~meshterm.ui.map_screen.LocationPickScreen`), and a reboot hands off to the same
reconnect dialog the app shows when a device is unplugged. (Channels have their own
first-class manager — see the ``channels`` tool.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..core.device_config import (
    DeviceConfigError,
    SettingSpec,
    build_snapshot,
    format_value,
    get_spec,
    parse_value,
    settings_by_category,
)
from .tui import Choice, Separator

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

# Menu action sentinels (distinct from setting keys, which are plain strings).
_LOCATION = "__location__"
_PRESETS = "__presets__"
_CUSTOM = "__custom__"
_ADVERT = "__advert__"
_REBOOT = "__reboot__"
_BACKUP = "__backup__"
_RESTORE = "__restore__"
_IDENTITY_KEY = "__identity_key__"
_RESET = "__reset__"
_VIEW = "__view__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"
# Sentinel for the "enter a value myself" option on non-strict enum prompts.
_OTHER = "__other__"

#: The setting keys folded into the single "Location" row (they stay individually
#: addressable from the CLI; only the editor presents them as one place-on-earth value).
_COORD_KEYS = ("adv_lat", "adv_lon")

#: How long the reboot flow waits to *observe* the link actually dropping before handing
#: off to the session's reconnect dialog (seconds). A companion normally vanishes from the
#: bus well within this; on timeout we hand off anyway.
_REBOOT_DROP_TIMEOUT_S = 10.0

#: Poll cadence while waiting for the rebooting companion's link to drop (seconds).
_REBOOT_DROP_POLL_S = 0.25


async def edit_config(ctx: "AppContext") -> Optional[list[tuple]]:
    """Run the interactive editor and return the staged operations to perform.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).

    Returns:
        A list of operation tuples for the tool to execute, or ``None`` if the user
        cancelled (or a device action — e.g. a reboot — ended the session) without
        anything staged to apply.
    """
    from .tui import CANCEL, SelectScreen

    device = await ctx.device()
    snapshot = await build_snapshot(device)
    custom = await device.get_custom_vars()

    # The editor is menu-only, so a full-screen session is always present. Keep the main
    # menu *pushed on the stack* for the whole session (rather than popping it between
    # prompts): every sub-prompt then floats over it as a modal popup with its own border
    # — the quit-dialog pattern — instead of replacing the screen. See _menu_loop.
    session = getattr(ctx.ui, "session", None)
    if session is None:  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the config editor is only available in the menu")
    loop = asyncio.get_running_loop()

    pending: dict[str, Any] = {}  # setting key -> staged new value
    extra_ops: list[tuple] = []  # staged custom-variable ops, in order
    cursor: Any = None  # the row to re-highlight, so the menu reopens where you left it

    while True:
        staged = len(pending) + len(extra_ops)
        title, items = _menu_items(snapshot, pending, staged)
        menu = SelectScreen(
            title, items, default=cursor, wrap=False,
            footer_hint="↑↓ move · type to filter · Enter select · Esc back",
        )
        menu.future = loop.create_future()
        session.push(menu)
        # Dispatch the choice while the menu is still pushed, so a sub-prompt floats over
        # it; the menu is always popped in the finally, even on an early return.
        try:
            choice = await menu.future
            if choice is CANCEL:  # Esc at the menu
                choice = _CANCEL
            if choice not in (None, _CANCEL):
                cursor = choice

            if choice in (None, _CANCEL):
                if staged and not await _confirm_discard(ctx, staged):
                    continue  # keep editing — the same menu is rebuilt next loop
                return None
            if choice == _APPLY:
                ops: list[tuple] = [("set", k, v) for k, v in pending.items()]
                ops.extend(extra_ops)
                return ops or None
            if choice == _VIEW:
                await ctx.ui.view(
                    config_table(snapshot, custom, pending), title="Device configuration"
                )
            elif choice == _LOCATION:
                await _stage_location(ctx, snapshot, pending)
            elif choice == _PRESETS:
                await _stage_preset(ctx, pending)
            elif choice == _CUSTOM:
                await _stage_custom_var(ctx, custom, extra_ops)
            elif choice == _ADVERT:
                await _advert_menu(ctx, device, snapshot)
            elif choice == _REBOOT:
                if await _reboot(ctx, device, snapshot, staged):
                    return None  # the link is dropping; the reconnect dialog takes over
            elif choice == _BACKUP:
                await _backup_now(ctx, device, snapshot)
            elif choice == _RESTORE:
                if await _restore_now(ctx, device, snapshot):
                    snapshot = await build_snapshot(device)
                    custom = await device.get_custom_vars()
            elif choice == _IDENTITY_KEY:
                if await _identity_key_menu(ctx, device, snapshot):
                    snapshot = await build_snapshot(device)
            elif choice == _RESET:
                if await _factory_reset(ctx, device, snapshot):
                    # Everything the editor knew about the device is gone; start clean.
                    pending.clear()
                    extra_ops.clear()
                    snapshot = await build_snapshot(device)
                    custom = await device.get_custom_vars()
            else:  # a setting key
                await _stage_setting(ctx, choice, snapshot, pending)
        finally:
            session.pop(menu)


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
    # Match the nodes list: a frameless SIMPLE_HEAD table with a left-justified accent title
    # and muted headers, so the two screens read as one family (see widgets.nodes_table).
    table = Table(
        title="[accent]Device configuration[/accent]",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="muted",
        expand=False,
        padding=(0, 2, 0, 0),
    )
    table.add_column("SETTING", style="muted", no_wrap=True)
    table.add_column("CURRENT", no_wrap=True)
    if show_staged:
        table.add_column("STAGED", style="warn", no_wrap=True)
    table.add_column("DESCRIPTION", style="muted")
    # Each setting's name is indented two spaces so the rows read as sitting *under* their
    # accent section heading, which stays flush-left.
    indent = "  "
    for category, specs in settings_by_category():
        table.add_section()
        header = [f"[accent]── {category.upper()} ──[/accent]", ""]
        if show_staged:
            header.append("")
        table.add_row(*header, "")
        for spec in specs:
            current = format_value(spec, spec.getter(snapshot))
            row = [f"{indent}{spec.label}", current]
            if show_staged:
                row.append(format_value(spec, pending[spec.key]) if spec.key in pending else "")
            table.add_row(*row, spec.help)
    if custom:
        table.add_section()
        cols = 4 if show_staged else 3
        table.add_row(
            "[accent]── CUSTOM ──[/accent]", *([""] * (cols - 1))
        )
        for key, value in custom.items():
            row = [f"{indent}{key}", value]
            if show_staged:
                row.append("")
            table.add_row(*row, "")
    return table


def _setting_row(spec: SettingSpec, snapshot: dict, pending: dict) -> Text:
    """Build one setting's menu row: ``label: current [→ staged]  —  help``.

    The staged arrow is drawn in the warn style so a dirty row stands out at a glance,
    and the help trails muted; both spans survive the select highlight (which only tints
    the row's base style).
    """
    row = Text(f"{spec.label}: ")
    row.append(format_value(spec, spec.getter(snapshot)))
    if spec.key in pending:
        row.append(f" → {format_value(spec, pending[spec.key])}", style="warn")
    row.append(f"  —  {spec.help}", style="muted")
    return row


def _format_coords(lat: Any, lon: Any) -> str:
    """Render a coordinate pair for display (``"not set"`` for the 0,0 no-fix value)."""
    try:
        lat_f, lon_f = float(lat or 0.0), float(lon or 0.0)
    except (TypeError, ValueError):
        return "?"
    if abs(lat_f) < 1e-6 and abs(lon_f) < 1e-6:
        return "not set"
    return f"{lat_f:.5f}, {lon_f:.5f}"


def _location_row(snapshot: dict, pending: dict) -> Text:
    """The single Location row standing in for the ``adv_lat``/``adv_lon`` pair."""
    row = Text("Location: ")
    row.append(_format_coords(snapshot.get("adv_lat"), snapshot.get("adv_lon")))
    if any(k in pending for k in _COORD_KEYS):
        lat = pending.get("adv_lat", snapshot.get("adv_lat"))
        lon = pending.get("adv_lon", snapshot.get("adv_lon"))
        row.append(f" → {_format_coords(lat, lon)}", style="warn")
    row.append("  —  Advertised position; pick it on the map", style="muted")
    return row


def _menu_items(
    snapshot: dict, pending: dict, staged: int
) -> tuple[str, list]:
    """Build the editor menu's title and rows for the current snapshot + staged state.

    Returns the ``(title, items)`` the caller pushes as a persistent backdrop screen (so
    sub-prompts float over it). Each setting row shows its current value (and any staged
    new value) plus a one-line explanation, so the user can see and understand what
    they're changing in place. The device actions that run immediately live in their own
    section below the settings.
    """
    items: list = []
    for category, specs in settings_by_category():
        items.append(Separator(f"── {category.upper()} ──"))
        for spec in specs:
            if spec.key in _COORD_KEYS:
                # Latitude/longitude collapse into one Location row (inserted in
                # adv_lat's slot so it sits where the coordinates used to).
                if spec.key == "adv_lat":
                    items.append(Choice(title=_location_row(snapshot, pending), value=_LOCATION))
                continue
            items.append(Choice(title=_setting_row(spec, snapshot, pending), value=spec.key))
        if category == "Radio":
            items.append(
                Choice(
                    title=Text.assemble(
                        "Radio presets…",
                        ("  —  Apply a standard regional or trade-off config", "muted"),
                    ),
                    value=_PRESETS,
                )
            )
        elif category == "Experimental":
            items.append(
                Choice(
                    title=Text.assemble(
                        "Custom variables…",
                        ("  —  Set a raw firmware variable by name", "muted"),
                    ),
                    value=_CUSTOM,
                )
            )

    items.append(Separator("── DEVICE ACTIONS (RUN IMMEDIATELY) ──"))
    items.append(_action("📡 Send advert…", "Zero-hop, flood, or share this node as a QR code", _ADVERT))
    items.append(_action("💾 Back up config to a file…", "Write every setting to TOML", _BACKUP))
    items.append(_action("📂 Restore config from a backup…", "Preview or apply a saved TOML", _RESTORE))
    items.append(_action("🔐 Identity key…", "Export or import the node's private key", _IDENTITY_KEY))
    items.append(_action("🔄 Reboot device…", "Restart the companion and reconnect", _REBOOT))
    items.append(
        Choice(
            title=Text.assemble(
                ("⚠ Factory reset…", "err"),
                ("  —  Erase everything (typed confirmation)", "muted"),
            ),
            value=_RESET,
        )
    )

    items.append(Separator("── REVIEW ──"))
    items.append(_action("🧾 View full configuration", "Every value in one table", _VIEW))
    if staged:
        items.append(
            Choice(
                title=Text.assemble(("✓ ", "ok"), f"Apply {_changes(staged)}"),
                value=_APPLY,
            )
        )

    # The backtracking row sits alone below a blank line, like every other screen's Back
    # (the main menu's Quit included); with changes staged it spells out the consequence.
    items.append(Separator(" "))
    if staged:
        items.append(
            Choice(
                title=Text.assemble(("✗ ", "err"), "Back — discard staged changes"),
                value=_CANCEL,
            )
        )
    else:
        items.append(Choice(title="Back", value=_CANCEL))

    title = "Device configuration" + (f" — {staged} staged" if staged else "")
    return title, items


def _action(label: str, help_text: str, value: str) -> Choice:
    """Build a device-action menu row: a label with a muted explanation."""
    return Choice(title=Text.assemble(label, (f"  —  {help_text}", "muted")), value=value)


def _changes(count: int) -> str:
    """``"1 staged change"`` / ``"3 staged changes"`` for dialogs and menu rows."""
    return f"{count} staged change{'' if count == 1 else 's'}"


# --- staging individual changes ----------------------------------------------


async def _stage_setting(
    ctx: "AppContext", key: str, snapshot: dict, pending: dict[str, Any]
) -> None:
    """Prompt for one setting's new value and stage it."""
    spec = get_spec(key)
    current = pending.get(key, spec.getter(snapshot))
    value = await _prompt_value(ctx, spec, current)
    if value is None:
        return
    if value == spec.getter(snapshot):
        pending.pop(key, None)  # set back to the device's value — nothing to change
    else:
        pending[key] = value


def _range_hint(spec: SettingSpec) -> str:
    """A muted "allowed values" hint for a numeric prompt, from the spec's bounds."""
    if spec.minimum is not None and spec.maximum is not None:
        return f"Allowed: {spec.minimum:g} – {spec.maximum:g}"
    if spec.minimum is not None:
        return f"Allowed: ≥ {spec.minimum:g}"
    if spec.maximum is not None:
        return f"Allowed: ≤ {spec.maximum:g}"
    return ""


async def _prompt_value(ctx: "AppContext", spec: SettingSpec, current: Any) -> Any:
    """Prompt for a typed value for ``spec`` (in the fitting dialog), ``None`` on cancel."""
    if spec.value_type == "bool":
        # A straight two-state choice reads best as a button pair; the current state is
        # the highlighted default so Enter changes nothing by accident.
        return await ctx.ui.dialog(
            spec.help,
            [("Off", False), ("On", True)],
            title=spec.label,
            default=1 if current else 0,
            keys={"0": False, "1": True, "n": False, "y": True},
        )

    if spec.value_type == "enum" and spec.choices is not None:
        items: list = [
            Choice(
                title=f"{k} — {label}" + ("  (current)" if k == current else ""),
                value=k,
            )
            for k, label in spec.choices.items()
        ]
        # Non-strict enums list the common values for convenience but still accept any
        # in-range integer, so offer an escape hatch to type one in.
        if not spec.strict_choices:
            items.append(Choice(title="Other (enter a value)…", value=_OTHER))
        selected = await ctx.ui.select(
            spec.label,
            items,
            prompt=spec.help,
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
        spec.label,
        prompt=spec.help,
        default="" if current is None else str(current),
        validate=validate,
        help_text=_range_hint(spec),
    )
    return None if raw is None else parse_value(spec, raw)


async def _stage_location(
    ctx: "AppContext", snapshot: dict, pending: dict[str, Any]
) -> None:
    """Set the advertised location: on the map, typed as a pair, or cleared.

    Staged like any other setting — the coordinates only reach the device on Apply.
    """
    lat = pending.get("adv_lat", snapshot.get("adv_lat"))
    lon = pending.get("adv_lon", snapshot.get("adv_lon"))
    choice = await ctx.ui.dialog(
        f"Advertised location: {_format_coords(lat, lon)}",
        [("Pick on map", "map"), ("Type coordinates", "type"), ("Clear", "clear")],
        title="📍 Location",
    )
    if choice is None:
        return

    if choice == "map":
        from .map_screen import pick_location

        initial = None
        try:
            if abs(float(lat or 0.0)) >= 1e-6 or abs(float(lon or 0.0)) >= 1e-6:
                initial = (float(lat), float(lon))
        except (TypeError, ValueError):
            initial = None
        picked = await pick_location(ctx, initial=initial)
        if picked is None:
            return
        # Six decimals ≈ 0.1 m — beyond the map's own precision, plenty for an advert.
        pending["adv_lat"] = round(picked[0], 6)
        pending["adv_lon"] = round(picked[1], 6)
    elif choice == "type":
        raw = await ctx.ui.text(
            "Set location",
            prompt="Enter latitude, longitude in decimal degrees",
            default=f"{lat}, {lon}" if _format_coords(lat, lon) != "not set" else "",
            validate=_valid_coords,
            help_text="e.g. 45.50000, -73.60000",
        )
        if not raw:
            return
        parsed = _parse_coords(raw)
        pending["adv_lat"], pending["adv_lon"] = parsed
    elif choice == "clear":
        # 0, 0 is MeshCore's "no fix" value: the node stops advertising a position.
        pending["adv_lat"], pending["adv_lon"] = 0.0, 0.0

    # Staging the device's own values back is a no-op; drop them so the row reads clean.
    for key in _COORD_KEYS:
        if key in pending and pending[key] == snapshot.get(key):
            del pending[key]


def _parse_coords(text: str) -> tuple[float, float]:
    """Parse a ``lat, lon`` pair (comma or space separated), range-checked via the specs.

    Raises:
        DeviceConfigError: If the text is not two in-range decimal degrees.
    """
    parts = [p for p in text.replace(",", " ").split() if p]
    if len(parts) != 2:
        raise DeviceConfigError("enter two numbers: latitude, longitude")
    lat = parse_value(get_spec("adv_lat"), parts[0])
    lon = parse_value(get_spec("adv_lon"), parts[1])
    return float(lat), float(lon)


def _valid_coords(text: str) -> bool | str:
    """Validate a typed coordinate pair, returning the parse error as the message."""
    try:
        _parse_coords(text)
        return True
    except DeviceConfigError as exc:
        return str(exc)


async def _stage_preset(ctx: "AppContext", pending: dict[str, Any]) -> None:
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
    idx = await ctx.ui.select(
        "Radio presets",
        items,
        prompt="Stage a standard set of radio parameters:",
    )
    if idx is None:
        return
    preset = RADIO_PRESETS[idx]
    pending.update(preset.as_settings())


async def _stage_custom_var(
    ctx: "AppContext", custom: dict[str, str], extra_ops: list[tuple]
) -> None:
    """Prompt for a custom/experimental variable and stage a set operation.

    Known variable names are offered as suggestions so an existing one can be recalled
    without retyping it; any new name is accepted as free text.
    """
    if custom:
        key = await ctx.ui.autocomplete(
            "Custom variable",
            sorted(custom),
            prompt="Name of the firmware variable to set:",
        )
    else:
        key = await ctx.ui.text(
            "Custom variable", prompt="Name of the firmware variable to set:"
        )
    if not key or not key.strip():
        return
    key = key.strip()
    value = await ctx.ui.text(
        "Custom variable", prompt=f"Value for {key}:", default=custom.get(key, "")
    )
    if value is None:
        return
    extra_ops.append(("set_custom", key, value))


# --- device actions (run immediately) -----------------------------------------


async def _run_now(
    ctx: "AppContext", device: "Device", snapshot: dict, ops: list[tuple], title: str
) -> int:
    """Execute ``ops`` on the device right away and show the result window.

    The immediate-action counterpart of the staged Apply path: same executor
    (:func:`~meshterm.tools.config.apply_ops`), so the notes and behavior match, but the
    output is presented at once instead of waiting for the tool to finish.

    Returns:
        The number of changes applied.
    """
    from ..tools.config import apply_ops

    changes, artifacts = await apply_ops(ctx, device, snapshot, ops)
    for artifact in artifacts:
        ctx.ui.note(f"[ok]●[/ok] wrote [accent]{artifact}[/accent]")
    await ctx.ui.present(title=title)
    return changes


async def _advert_menu(ctx: "AppContext", device: "Device", snapshot: dict) -> None:
    """Send an advert (zero-hop or flood) or show this node's shareable contact card."""
    choice = await ctx.ui.select(
        "📡 Send advert",
        [
            Choice(title="Zero-hop  —  Announce directly to neighbours in range", value="zero"),
            Choice(title="Flood  —  Repeaters rebroadcast it across the mesh", value="flood"),
            Choice(title="Share QR / URI  —  Show this node's contact card", value="share"),
            Separator(" "),
            Choice(title="Back", value=None),
        ],
        prompt="Announce this node to the mesh:",
    )
    if choice is None:
        return
    if choice == "share":
        await _show_contact_card(ctx, snapshot)
        return
    await _run_now(ctx, device, snapshot, [("advert", choice == "flood")], "Advert")


def contact_share_url(name: str, public_key: str, node_type: int = 1) -> str:
    """Build the MeshCore ``meshcore://contact/add`` share URL for this node.

    The companion-app format (see the MeshCore ``qr_codes`` doc): the advertised name,
    the full 32-byte public key as hex, and the node type (1 = companion, 2 = repeater,
    3 = room server, 4 = sensor).

    Args:
        name: The node's advertised name.
        public_key: The node's public key as a hex string.
        node_type: The MeshCore advert type byte.

    Returns:
        A ``meshcore://contact/add?name=…&public_key=…&type=…`` URL.
    """
    return (
        f"meshcore://contact/add?name={quote(name, safe='')}"
        f"&public_key={public_key.lower()}&type={int(node_type)}"
    )


async def _show_contact_card(ctx: "AppContext", snapshot: dict) -> None:
    """Show this node's contact card as a scannable QR code plus the raw URI."""
    from .qr import qr_text

    public_key = str(snapshot.get("public_key") or "")
    if not public_key:
        ctx.ui.note("[err]the device did not report a public key — nothing to share[/err]")
        await ctx.ui.present(title="Share contact")
        return
    name = str(snapshot.get("name") or "this node")
    url = contact_share_url(name, public_key, int(snapshot.get("adv_type") or 1))
    body = Group(
        Text("Scan to add this node as a contact:", style="muted"),
        Text(""),
        qr_text(url),
        Text(""),
        Text(url, style="accent"),
    )
    await ctx.ui.view(body, title=f"Share {name}", footer_hint="Esc back")


async def _reboot(
    ctx: "AppContext", device: "Device", snapshot: dict, staged: int
) -> bool:
    """Confirm and reboot the device, handing off to the session's reconnect dialog.

    The confirmation dialog warns when staged changes would be lost (a reboot ends the
    editor, discarding them). After the command is sent, we wait to actually observe the
    link dropping — flagging :attr:`~meshterm.context.AppContext.reboot_in_progress` so
    the session-wide disconnect watcher labels the ensuing dialog as a reboot, waits for
    the companion to come back, and reconnects — exactly the unplugged-device flow.

    Returns:
        ``True`` if the reboot was sent and the editor should close; ``False`` if the
        user backed out (or the simulator, which has no link to drop, absorbed it).
    """
    warning = "Reboot the device now?"
    if staged:
        warning = (
            f"Reboot the device now? Your {_changes(staged)} have not been "
            "applied and will be discarded."
        )
    choice = await ctx.ui.dialog(
        warning,
        [("Cancel", None), ("Reboot", "reboot")],
        title="🔄 Reboot device",
        default=1,
        danger=True,
    )
    if choice != "reboot":
        return False

    if ctx.active_transport is None:
        # The simulator has no link to drop and comes back instantly; just send it.
        await device.reboot()
        ctx.ui.note("[warn]device rebooting[/warn]")
        await ctx.ui.present(title="Reboot")
        return False

    # Flag the drop as expected *before* sending, so however quickly the watcher fires,
    # the reconnect dialog already knows to present it as a reboot.
    ctx.reboot_in_progress = True
    try:
        await device.reboot()
    except Exception:
        ctx.reboot_in_progress = False
        raise
    # Hold here until the link is actually observed down (or a generous timeout), so the
    # editor doesn't flash back to the menu for the second or two before the watcher
    # notices. The watcher may cancel us mid-wait when it fires — that's the handoff.
    deadline = asyncio.get_running_loop().time() + _REBOOT_DROP_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        if not await ctx.link_alive():
            break
        await asyncio.sleep(_REBOOT_DROP_POLL_S)
    return True


async def _backup_now(ctx: "AppContext", device: "Device", snapshot: dict) -> None:
    """Prompt for a destination and write the TOML backup immediately."""
    path = await ctx.ui.path(
        "Back up config",
        prompt="Write every setting to this TOML file:",
        default="meshterm-config.toml",
    )
    if path:
        await _run_now(ctx, device, snapshot, [("backup", Path(path))], "Backup")


async def _restore_now(ctx: "AppContext", device: "Device", snapshot: dict) -> bool:
    """Restore from a TOML backup: pick the file, preview if wanted, then apply.

    Returns:
        ``True`` if the device was changed (so the caller refreshes its snapshot).
    """
    raw = await ctx.ui.path(
        "Restore config", prompt="Read settings from this TOML backup file:"
    )
    if not raw:
        return False
    path = Path(raw)
    if not path.exists():
        ctx.ui.note(f"[err]no such file:[/err] {path}")
        await ctx.ui.present(title="Restore")
        return False

    choice = await ctx.ui.dialog(
        "Apply the backup now, or preview the changes first?",
        [("Cancel", None), ("Preview", "preview"), ("Apply", "apply")],
        title="📂 Restore from backup",
        default=1,
    )
    if choice == "preview":
        await _run_now(ctx, device, snapshot, [("restore", path, True)], "Restore preview")
        choice = await ctx.ui.dialog(
            "Apply these changes to the device?",
            [("Cancel", None), ("Apply", "apply")],
            title="📂 Restore from backup",
            default=1,
        )
    if choice != "apply":
        return False
    changed = await _run_now(ctx, device, snapshot, [("restore", path, False)], "Restore")
    return changed > 0


async def _identity_key_menu(ctx: "AppContext", device: "Device", snapshot: dict) -> bool:
    """Export or import the device's private identity key.

    Returns:
        ``True`` if the identity changed (a key was imported), so the caller re-reads
        its snapshot.
    """
    choice = await ctx.ui.select(
        "🔐 Identity key",
        [
            Choice(title="Show private key  —  Display it on screen (sensitive)", value="show"),
            Choice(title="Export to a file…  —  Write it to disk (keep it secret)", value="file"),
            Choice(title="Import a key…  —  Replace this device's identity", value="import"),
            Separator(" "),
            Choice(title="Back", value=None),
        ],
        prompt="Manage this node's private identity key:",
    )
    if choice is None:
        return False

    if choice == "show":
        ok = await ctx.ui.dialog(
            "The private key IS the node's identity — anyone who sees it can impersonate "
            "this node. Show it on screen?",
            [("Cancel", None), ("Show key", "show")],
            title="🔐 Show private key",
            default=1,
            danger=True,
        )
        if ok == "show":
            await _run_now(ctx, device, snapshot, [("export_key",)], "Private key")
        return False

    if choice == "file":
        path = await ctx.ui.path(
            "Export identity key",
            prompt="Write the private key to this file (keep it secret):",
            default="meshterm-identity.key",
        )
        if path:
            await _run_now(ctx, device, snapshot, [("export_key", Path(path))], "Private key")
        return False

    # Import: collect the key, then gate behind the typed confirmation.
    key_hex = await ctx.ui.text(
        "Import identity key",
        prompt="Paste the private key as hex:",
        validate=_is_hex,
    )
    if not key_hex:
        return False
    confirmed = await ctx.ui.typed_confirm(
        "Importing a key permanently overwrites this device's identity. Contacts and "
        "messages keyed to the old identity will no longer match it.",
        "IMPORT",
        title="🔐 Import private key",
    )
    if not confirmed:
        return False
    await _run_now(ctx, device, snapshot, [("import_key", key_hex.strip())], "Import key")
    return True


async def _factory_reset(ctx: "AppContext", device: "Device", snapshot: dict) -> bool:
    """Factory-reset the device behind a typed confirmation.

    Returns:
        ``True`` if the reset ran (so the caller drops everything it staged and re-reads
        the device).
    """
    confirmed = await ctx.ui.typed_confirm(
        "This erases EVERYTHING on the device — identity, contacts, channels, and every "
        "setting — and cannot be undone.",
        "RESET",
        title="⚠ Factory reset",
    )
    if not confirmed:
        return False
    await _run_now(ctx, device, snapshot, [("factory_reset",)], "Factory reset")
    return True


# --- confirmations -------------------------------------------------------------


async def _confirm_discard(ctx: "AppContext", staged: int) -> bool:
    """Ask before dropping staged changes on the way out; ``True`` means discard."""
    choice = await ctx.ui.dialog(
        f"Discard {_changes(staged)} without applying them?",
        [("Keep editing", "keep"), ("Discard", "discard")],
        title="Unsaved changes",
        default=1,
        danger=True,
    )
    return choice == "discard"


# --- validators --------------------------------------------------------------


def _is_hex(text: str) -> bool | str:
    """Validate that ``text`` is a hex string."""
    try:
        bytes.fromhex(text)
        return True
    except ValueError:
        return "Enter hex characters only."
