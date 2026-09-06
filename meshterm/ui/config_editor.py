"""Interactive device-configuration screens, rendered in the full-screen session.

Two sibling screens live here, deliberately kept distinct:

* **Device config** (:func:`edit_config`, behind the ``config`` tool) — one grouped,
  column-aligned list of every setting under its category heading, each row showing its
  current value, any staged change, and a one-line explanation. Editing a row *stages* the
  new value (shown as ``current → new``) and nothing touches the radio until *Apply*;
  backing out with staged changes asks before discarding them. The editor returns the
  staged operations for :class:`~meshterm.tools.config.ConfigTool` to execute and log.
* **Device actions** (:func:`device_actions`, behind the ``device-actions`` tool) — the
  operations that act on the box itself rather than a value: backup/restore, the
  identity key, clock sync, reboot, and factory reset. These *run immediately* (after
  their own confirmation dialog — destructive ones gate behind typing a confirmation
  word); they have no meaningful "preview", so their result is shown at once. They share
  the settings snapshot and :func:`~meshterm.tools.config.apply_ops` executor with the
  editor, which is why both screens live in this module. The everyday advert action
  lives in its own main-menu entry instead (the ``advert`` tool, via
  :func:`send_advert`), one keystroke away as a popup over the menu.

Multiple-choice values are picked in dialogs (booleans as an On/Off button pair, enums as
a floating select), the node's location can be set by pointing at the full-screen map (see
:class:`~meshterm.ui.map_screen.LocationPickScreen`), and a reboot hands off to the same
reconnect dialog the app shows when a device is unplugged. (Channels have their own
first-class manager — see the ``channels`` tool.)
"""

from __future__ import annotations

import asyncio
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from rich import box
from rich.cells import cell_len
from rich.table import Table
from rich.text import Text

from ..core.advert_store import (
    DIRECT_CADENCE_HOURS,
    FLOOD_CADENCE_HOURS,
    OFF,
    AdvertPolicy,
    cadence_label,
)
from ..core.device_config import (
    DeviceConfigError,
    SettingSpec,
    build_snapshot,
    effective_maximum,
    format_value,
    get_spec,
    parse_value,
    settings_by_category,
)
from ..platforms import Platform, on_platform
from .device_info_screen import REVEAL_KEY
from .marks import MASK_MARK
from .menus import (
    confirm_discard,
    exit_rows,
    lane_header,
    lane_row,
    menu_rows,
    run_steps,
    section_heading,
)
from .tui import Choice, SelectScreen, Separator
from .tui.select import _splice_hint

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

# Menu action sentinels (distinct from setting keys, which are plain strings).
_LOCATION = "__location__"
_PRESETS = "__presets__"
_CUSTOM = "__custom__"
_ADVERT_DIRECT = "__advert_direct__"
_ADVERT_FLOOD = "__advert_flood__"
_SYNC_CLOCK = "__sync_clock__"
_REBOOT = "__reboot__"
_BACKUP = "__backup__"
_RESTORE = "__restore__"
_IDENTITY_KEY = "__identity_key__"
_RESET = "__reset__"
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


async def cached_snapshot(ctx: AppContext, device: Device) -> dict:
    """A config snapshot for a *screen open*, reusing the devstate session cache.

    The two facts devstate already holds — ``SELF_INFO`` and the path-hash mode — are
    the slowest part of :func:`~meshterm.core.device_config.build_snapshot` to re-ask
    the radio for, and every config apply invalidates them (see
    ``tools/config.py``), so an open can trust the cache. The refresh after a restore,
    key change, or factory reset keeps calling ``build_snapshot(device)`` raw: the
    device state genuinely changed under us there.
    """
    self_info: dict | None = None
    path_hash_mode: int | None = None
    try:
        self_info = await ctx.devstate.self_info()
        path_hash_mode = await ctx.devstate.path_hash_mode()
    except Exception:  # noqa: BLE001 - fall back to the raw reads below
        pass
    return await build_snapshot(
        device, self_info=self_info, path_hash_mode=path_hash_mode
    )


async def edit_config(ctx: AppContext) -> list[tuple] | None:
    """Run the interactive editor and return the staged operations to perform.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).

    Returns:
        A list of operation tuples for the tool to execute, or ``None`` if the user
        cancelled without anything staged to apply.
    """
    from .tui import CANCEL

    device = await ctx.device()
    snapshot = await cached_snapshot(ctx, device)
    custom = await device.get_custom_vars()
    # The background-advert cadences are app-side settings (MeshTerm sends the adverts,
    # not the firmware), but they're staged and applied exactly like device values so
    # the editor stays one coherent surface.
    policy = ctx.advert_store.load(str(snapshot.get("public_key") or ""))

    # The editor is menu-only, so a full-screen session is always present. Keep the main
    # menu *pushed on the stack* for the whole session (rather than popping it between
    # prompts): every sub-prompt then floats over it as a modal popup with its own border
    # — the quit-dialog pattern — instead of replacing the screen. See _menu_loop.
    session = getattr(ctx.ui, "session", None)
    if session is None:  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the config editor is only available in the menu")
    pending: dict[str, Any] = {}  # setting key -> staged new value
    extra_ops: list[tuple] = []  # staged custom-variable ops, in order

    # The editor's rows *are* its data — each carries its staged ``current → new`` value and
    # the title counts what is staged — so they are refreshed in place after every round
    # (``replace_items``) rather than rebuilt as a new screen. One screen for the whole
    # session means the typed filter survives editing a setting, not just the cursor.
    menu = _ConfigMenu(
        session,
        lambda reveal: _menu_items(
            snapshot, pending, len(pending) + len(extra_ops), policy, reveal
        ),
        conceals=has_pin(snapshot),
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )
    async with session.stay(menu) as visit:
        while True:
            staged = len(pending) + len(extra_ops)
            choice = await visit.result()
            if choice is CANCEL:  # Esc at the menu
                choice = _CANCEL

            if choice in (None, _CANCEL):
                if staged and not await confirm_discard(ctx, staged, verb="applying"):
                    continue  # keep editing — the same menu, the same place in it
                return None
            if choice == _APPLY:
                ops: list[tuple] = []
                for k, v in pending.items():
                    if k in (_ADVERT_DIRECT, _ADVERT_FLOOD):
                        ops.append(("advert_cadence", k == _ADVERT_FLOOD, v))
                    else:
                        ops.append(("set", k, v))
                ops.extend(extra_ops)
                return ops or None
            if choice in (_ADVERT_DIRECT, _ADVERT_FLOOD):
                await _stage_advert_cadence(ctx, choice == _ADVERT_FLOOD, policy, pending)
            elif choice == _LOCATION:
                await _stage_location(ctx, snapshot, pending)
            elif choice == _PRESETS:
                await _stage_preset(ctx, snapshot, pending)
            elif choice == _CUSTOM:
                await _stage_custom_var(ctx, custom, extra_ops)
            else:  # a setting key
                await _stage_setting(ctx, choice, snapshot, pending)
            menu.refresh()


# --- rendering ---------------------------------------------------------------


def config_table(
    snapshot: dict,
    custom: dict[str, str],
    pending: dict[str, Any] | None = None,
    *,
    reveal_pin: bool = False,
) -> Table:
    """Build the full configuration table, overlaying any staged changes.

    Args:
        snapshot: Device snapshot from ``build_snapshot``.
        custom: Current custom variables.
        pending: Optional staged changes (setting key -> new value).
        reveal_pin: Whether to print the BLE pairing PIN. It is concealed by default —
            this table is the whole-device dump, the thing that gets read over a
            shoulder, screenshotted, and pasted into a bug report, and a pairing code is
            the one value on it that lets someone else's phone onto the radio. The
            Device info page reveals it on a keypress (see
            :class:`~meshterm.ui.device_info_screen.DeviceInfoScreen`); the CLI names it
            one value at a time with ``meshterm config get device_pin``.

    Returns:
        A Rich :class:`Table` of every setting's current (and staged) value plus custom
        variables, ready to hand to ``ctx.ui.show`` / ``ctx.ui.view``.
    """
    pending = pending or {}
    show_staged = bool(pending)
    # The DESCRIPTION lane needs a screen wide enough to hold prose beside the setting and
    # its value(s) — 72 columns, and no third narrow lane. Where it doesn't fit, the table
    # is the settings alone (Rich was already dropping the column outright at those
    # widths) and the explanations stay in the editor, one row at a time.
    describe = _describe and not show_staged
    # Match the contacts list: a frameless SIMPLE_HEAD table with a left-justified accent title
    # and muted headers, so the two screens read as one family (see widgets.contacts_table).
    table = Table(
        title="[accent]Device config[/accent]",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="muted",
        expand=False,
        padding=(0, 2, 0, 0),
    )
    # The two narrow lanes are ``no_wrap`` and pre-folded to a cap (see _fold_label /
    # _fold_value), so each takes exactly the room its cap allows and DESCRIPTION — the
    # lane whose content is systematically the widest — keeps the whole remainder. Letting
    # the labels and values size themselves instead gave the descriptions 19 cells of a
    # 72-cell screen and wrapped almost every one of them three lines deep.
    table.add_column("SETTING", style="muted", no_wrap=True)
    table.add_column("CURRENT", no_wrap=True)
    if show_staged:
        table.add_column("STAGED", style="warn", no_wrap=True)
    if describe:
        table.add_column("DESCRIPTION", style="muted")

    def row_of(cells: list[str], description: str) -> list[str]:
        """One table row: the narrow lanes, plus the description where it is drawn."""
        return [*cells, description] if describe else cells

    for category, specs in settings_by_category():
        table.add_section()
        header = [f"[accent]── {category} ──[/accent]", ""]
        if show_staged:
            header.append("")
        table.add_row(*row_of(header, ""))
        for spec in specs:
            current = format_value(spec, spec.getter(snapshot))
            if spec.key == PIN_KEY and not reveal_pin:
                current = conceal(current)
            row = [_fold_label(spec.label, describe), _fold_value(current, describe)]
            if show_staged:
                row.append(
                    _fold_value(format_value(spec, pending[spec.key]), describe)
                    if spec.key in pending
                    else ""
                )
            table.add_row(*row_of(row, spec.help))
    if custom:
        table.add_section()
        blanks = 2 if show_staged else 1
        table.add_row(*row_of(["[accent]── Custom ──[/accent]", *([""] * blanks)], ""))
        for key, value in custom.items():
            row = [_fold_label(key, describe), _fold_value(value, describe)]
            if show_staged:
                row.append("")
            table.add_row(*row_of(row, ""))
    return table


#: The one setting this table holds back: the BLE pairing PIN (the firmware reports it as
#: ``ble_pin``; :data:`~meshterm.core.device_config.SETTINGS` keys it ``device_pin``).
PIN_KEY = "device_pin"


#: How wide a concealed PIN draws: the width of the *field*, taken from the setting's own
#: upper bound (999999), not the length of the value sitting in it. A typed password masks
#: per character because the typist needs to count what they have entered; a stored one
#: shows its field, so the row can't be read for how many digits to guess — and a PIN of
#: ``0`` behind a single bullet would have read as a stray dot rather than a covered value.
_PIN_WIDTH = len(str(get_spec(PIN_KEY).maximum))


def conceal(value: str) -> str:
    """``value`` as a row of :data:`_PIN_WIDTH` mask bullets.

    A value the device could not report is not a secret, it is an absence: ``format_value``
    already renders that as ``?``, and a ``?`` behind bullets would claim there is
    something to reveal when there is nothing. So only a real value is concealed, and
    :func:`has_pin` asks the same question the other way round — a page with nothing to
    conceal advertises no key for it.
    """
    return value if value == "?" else MASK_MARK * _PIN_WIDTH


def has_pin(snapshot: dict) -> bool:
    """Whether ``snapshot`` carries a PIN at all — i.e. whether concealing it means anything."""
    return get_spec(PIN_KEY).getter(snapshot) is not None


#: Cell caps for the table's two narrow lanes, indent included. Sized to the widest label
#: and value the settings actually carry, less the few longest — the four that overflow
#: fold onto a second line, which costs nothing on rows whose description was wrapping
#: anyway, and buys every other row ten more cells of prose.
_SETTING_CAP = 24
_VALUE_CAP = 15

#: Each setting's name is indented two spaces so the rows read as sitting *under* their
#: accent section heading, which stays flush-left; a folded continuation hangs two further
#: in, so a wrapped label can never be mistaken for a heading at column 0.
_INDENT = "  "
_HANG = "    "


def _fold_label(label: str, describe: bool) -> str:
    """One SETTING cell: indented, and hard-wrapped at :data:`_SETTING_CAP` if it must be.

    The cap only buys room for the DESCRIPTION lane, so where that lane isn't drawn the
    label keeps its natural width — nothing is competing for the cells.
    """
    if not describe:
        return f"{_INDENT}{label}"
    return (
        "\n".join(
            textwrap.wrap(
                label, _SETTING_CAP, initial_indent=_INDENT, subsequent_indent=_HANG
            )
        )
        or _INDENT
    )


def _fold_value(value: str, describe: bool) -> str:
    """One CURRENT/STAGED cell, hard-wrapped at :data:`_VALUE_CAP`.

    An enum value reads ``N (label)``, so the fold prefers the break *before* the
    parenthesised label — ``1`` over ``(2-byte hashes)`` rather than a torn ``1 (2-byte``
    — and a long node name (the field is 31 bytes wide) folds instead of stretching the
    lane across the whole screen.
    """
    if not describe or cell_len(value) <= _VALUE_CAP:
        return value
    head, sep, tail = value.partition(" (")
    if sep and cell_len(head) <= _VALUE_CAP and cell_len(tail) + 1 <= _VALUE_CAP:
        return f"{head}\n({tail}"
    return "\n".join(textwrap.wrap(value, _VALUE_CAP)) or value


#: Whether this platform's screen is wide enough for a DESCRIPTION lane at all. The
#: PicoCalc's 53 columns are spent by the setting and its value.
_describe: bool = True


@on_platform
def _bind_describe(platform: Platform) -> None:
    """Bind the DESCRIPTION lane to the platform (runs now and on every switch)."""
    global _describe
    _describe = platform.readable_cols >= 72


def _setting_value(
    spec: SettingSpec, snapshot: dict, pending: dict, reveal_pin: bool = False
) -> Text:
    """One setting's VALUE lane: ``current [→ staged]``.

    The staged arrow is drawn in the warn style so a dirty row stands out at a glance;
    the span survives the select highlight (which only tints the row's base style).

    The pairing PIN is concealed here on the same terms as in :func:`config_table` — a
    staged one too: a PIN you typed a moment ago is still a PIN, and a row that uncovered
    itself the instant it was edited would leave the secret on screen for the rest of the
    session's staging.
    """
    hide = spec.key == PIN_KEY and not reveal_pin
    shown = format_value(spec, spec.getter(snapshot))
    value = Text(conceal(shown) if hide else shown)
    if spec.key in pending:
        staged = format_value(spec, pending[spec.key])
        value.append(f" → {conceal(staged) if hide else staged}", style="warn")
    return value


def _format_coords(lat: Any, lon: Any) -> str:
    """Render a coordinate pair for display (``"not set"`` for the 0,0 no-fix value)."""
    try:
        lat_f, lon_f = float(lat or 0.0), float(lon or 0.0)
    except (TypeError, ValueError):
        return "?"
    if abs(lat_f) < 1e-6 and abs(lon_f) < 1e-6:
        return "not set"
    return f"{lat_f:.5f}, {lon_f:.5f}"


def _location_value(snapshot: dict, pending: dict) -> Text:
    """The Location row's VALUE lane, standing in for the ``adv_lat``/``adv_lon`` pair."""
    value = Text(_format_coords(snapshot.get("adv_lat"), snapshot.get("adv_lon")))
    if any(k in pending for k in _COORD_KEYS):
        lat = pending.get("adv_lat", snapshot.get("adv_lat"))
        lon = pending.get("adv_lon", snapshot.get("adv_lon"))
        value.append(f" → {_format_coords(lat, lon)}", style="warn")
    return value


class _ConfigMenu(SelectScreen):
    """The editor's list, with the pairing PIN's row concealed until ``^S`` uncovers it.

    Everything a grouped select list does, plus the same toggle the Device info page
    carries — the same chord, the same chip, the same words — because the two pages show
    the same value and a secret that comes out from under a different key on each is a
    second thing to learn for nothing. Here the key *has* to be a chord: this list filters
    as you type, so every bare letter is spoken for.

    The rows are data (each carries its staged ``current → new``), so a toggle refreshes
    them in place through :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items` —
    which follows the highlighted row by value and keeps the typed filter — rather than
    rebuilding the screen under a reader who was part-way down it.
    """

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        build: Callable[[bool], tuple[str, list]],
        *,
        conceals: bool,
        **kwargs: Any,
    ) -> None:
        """Build the list over its row builder.

        Args:
            session: The running TUI session (repainted when the PIN is toggled).
            build: Renders ``(title, items)`` for a given ``reveal_pin``.
            conceals: Whether this device reports a PIN at all. ``False`` leaves the page
                with nothing to reveal, so neither the footer nor the lane offers a key
                for it.
            **kwargs: Passed to :class:`~meshterm.ui.tui.select.SelectScreen`.
        """
        title, items = build(False)
        super().__init__(title, items, **kwargs)
        self._session = session
        self._build = build
        self._conceals = conceals
        self._revealed = False

    def refresh(self) -> None:
        """Re-read the rows for what is staged now, keeping the reveal, cursor and filter."""
        title, items = self._build(self._revealed)
        self.replace_items(items, title=title)

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The list's own hint, plus the reveal atom before the trailing Esc clause."""
        base = super().footer_hint
        if not self._conceals:
            return base
        verb = "hide" if self._revealed else "show"
        return _splice_hint(base, f"{REVEAL_KEY} {verb} PIN")

    @property
    def fkey_lane(self):
        """The list's lane with the reveal on F3 — the slot a delete-less list leaves free.

        F1/F2 are this grouped list's section jumps, F4/F5 the pager; F3 is what a select
        list spends on its own verb (``Delete``, where it has one), and this one's verb is
        the reveal. A device with no PIN leaves the slot empty rather than dim: the action
        isn't a thing on this page at all.
        """
        from .tui.fkeys import FPair

        lane = list(super().fkey_lane)
        if self._conceals:
            lane[2] = FPair("Hide" if self._revealed else "Reveal", "reveal")
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Toggle the PIN, or behave as any select list does."""
        if action == "reveal":
            if not self._conceals:
                return
            self._revealed = not self._revealed
            self.refresh()
            self._session.invalidate()
            return
        super().handle(action, data)


def _menu_items(
    snapshot: dict,
    pending: dict,
    staged: int,
    policy: AdvertPolicy,
    reveal_pin: bool = False,
) -> tuple[str, list]:
    """Build the editor menu's title and rows for the current snapshot + staged state.

    Returns the ``(title, items)`` the caller pushes as a persistent backdrop screen (so
    sub-prompts float over it). The rows sit in three aligned columns — setting, current
    value (and any staged new value), description — under one header line, so the list
    reads like the full-configuration table it stages changes for.

    ``reveal_pin`` is :class:`_ConfigMenu`'s toggle, passed straight through to the PIN's
    value lane.
    """
    # First pass: collect every row's lanes per category, so the columns can be sized to
    # their content (including any staged ``→ new`` arrows) before a single row is built.
    sections: list[tuple[str, list[tuple[str, Text, str, Any]]]] = []
    for category, specs in settings_by_category():
        rows: list[tuple[str, Text, str, Any]] = []
        for spec in specs:
            if spec.key in _COORD_KEYS:
                # Latitude/longitude collapse into one Location row (inserted in
                # adv_lat's slot so it sits where the coordinates used to).
                if spec.key == "adv_lat":
                    rows.append((
                        "Location",
                        _location_value(snapshot, pending),
                        "Advertised position; pick it on the map",
                        _LOCATION,
                    ))
                continue
            rows.append((
                spec.label,
                _setting_value(spec, snapshot, pending, reveal_pin),
                spec.help,
                spec.key,
            ))
        if category == "Radio":
            rows.append((
                "Radio presets…", Text(),
                "Stage MeshCore's settings for a region", _PRESETS,
            ))
        elif category == "Experimental":
            rows.append((
                "Custom variables…", Text(),
                "Set a raw firmware variable by name", _CUSTOM,
            ))
        sections.append((category, rows))

    # App-side rows: the background-advert cadences MeshTerm itself runs (see the
    # advert scheduler). They stage and apply like device settings, so they sit in the
    # same lanes under their own heading.
    sections.append((
        "Background adverts",
        [
            (
                "Direct advert",
                _cadence_value(policy, False, pending),
                "Scheduled zero-hop announce; any manual send resets it",
                _ADVERT_DIRECT,
            ),
            (
                "Flood advert",
                _cadence_value(policy, True, pending),
                "Scheduled mesh-wide announce via repeaters",
                _ADVERT_FLOOD,
            ),
        ],
    ))

    label_w = max(cell_len(label) for _, rows in sections for label, _, _, _ in rows)
    value_w = max(cell_len(value.plain) for _, rows in sections for _, value, _, _ in rows)

    # The shared editor header (menus.lane_header): each label over its own lane, clear of
    # the pointer column, abbreviating rather than wrapping where the terminal is too narrow
    # for the full words. It is pinned for the whole list — the lanes mean the same in every
    # category — so a scrolled row keeps both its column header and its section heading
    # overhead (see Screen.sticky_rows).
    items: list = [
        Separator(lambda w: lane_header(label_w, value_w, w), pinned=True)
    ]
    for category, rows in sections:
        items.append(section_heading(category))
        for label, value, help_text, key in rows:
            items.append(
                Choice(title=lane_row(label, value, help_text, label_w, value_w), value=key)
            )

    # Nothing at all while the editor is clean — Esc leaves, and a row saying so was
    # retired app-wide. With changes staged the pair appears below one blank line: Apply
    # has no key of its own, and Back spells out what leaving costs (see menus.exit_rows).
    items.extend(exit_rows(staged, apply_value=_APPLY, back_value=_CANCEL))

    title = "Device config" + (f" — {staged} staged" if staged else "")
    return title, items


def _cadence_value(policy: AdvertPolicy, flood: bool, pending: dict) -> Text:
    """One background-advert row's VALUE lane: ``current [→ staged]``."""
    key = _ADVERT_FLOOD if flood else _ADVERT_DIRECT
    value = Text(cadence_label(policy.cadence(flood)))
    if key in pending:
        value.append(f" → {cadence_label(pending[key])}", style="warn")
    return value


# --- staging individual changes ----------------------------------------------


async def _stage_setting(
    ctx: AppContext, key: str, snapshot: dict, pending: dict[str, Any]
) -> None:
    """Prompt for one setting's new value and stage it."""
    spec = get_spec(key)
    current = pending.get(key, spec.getter(snapshot))
    value = await _prompt_value(ctx, spec, current, snapshot)
    if value is None:
        return
    if value == spec.getter(snapshot):
        pending.pop(key, None)  # set back to the device's value — nothing to change
    else:
        pending[key] = value


def _range_hint(spec: SettingSpec, snapshot: dict) -> str:
    """A muted "allowed values" hint for a numeric prompt.

    Uses the *effective* maximum — the device-reported bound (e.g. this board's max TX
    power) when the spec names one, else the static bound — so the hint promises exactly
    what validation will accept.
    """
    maximum = effective_maximum(spec, snapshot)
    if spec.minimum is not None and maximum is not None:
        return f"Allowed: {spec.minimum:g} – {maximum:g}"
    if spec.minimum is not None:
        return f"Allowed: ≥ {spec.minimum:g}"
    if maximum is not None:
        return f"Allowed: ≤ {maximum:g}"
    return ""


async def _prompt_value(
    ctx: AppContext, spec: SettingSpec, current: Any, snapshot: dict
) -> Any:
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
            parse_value(spec, text, snapshot)
            return True
        except DeviceConfigError as exc:
            return str(exc)

    raw = await ctx.ui.text(
        spec.label,
        prompt=spec.help,
        default="" if current is None else str(current),
        validate=validate,
        help_text=_range_hint(spec, snapshot),
    )
    return None if raw is None else parse_value(spec, raw, snapshot)


async def _stage_advert_cadence(
    ctx: AppContext, flood: bool, policy: AdvertPolicy, pending: dict[str, Any]
) -> None:
    """Pick one background-advert type's cadence and stage it.

    Staged under the type's sentinel key; Apply turns it into an ``advert_cadence`` op
    (see :func:`~meshterm.tools.config.apply_ops`). Picking the value already in force
    un-stages the row, matching :func:`_stage_setting`.
    """
    key = _ADVERT_FLOOD if flood else _ADVERT_DIRECT
    in_force = policy.cadence(flood)
    current = pending.get(key, in_force)
    hours_choices = FLOOD_CADENCE_HOURS if flood else DIRECT_CADENCE_HOURS
    items: list = [
        Choice(
            title=cadence_label(hours).capitalize()
            + ("  (current)" if hours == current else ""),
            value=hours,
        )
        for hours in (*hours_choices, OFF)
    ]
    if flood:
        prompt = "How often repeaters rebroadcast this node across the mesh:"
    else:
        prompt = "How often this node announces itself to neighbours in range:"
    selected = await ctx.ui.select(
        "Flood advert" if flood else "Direct advert",
        items,
        prompt=prompt,
        default=current,
    )
    if selected is None:
        return
    if selected == in_force:
        pending.pop(key, None)  # set back to the value in force — nothing to change
    else:
        pending[key] = selected


async def _stage_location(
    ctx: AppContext, snapshot: dict, pending: dict[str, Any]
) -> None:
    """Set the advertised location: on the map, typed as a pair, or cleared.

    Staged like any other setting — the coordinates only reach the device on Apply.
    """
    lat = pending.get("adv_lat", snapshot.get("adv_lat"))
    lon = pending.get("adv_lon", snapshot.get("adv_lon"))
    choice = await ctx.ui.dialog(
        f"Advertised location: {_format_coords(lat, lon)}",
        [("Pick on map", "map"), ("Type coordinates", "type"), ("Clear", "clear")],
        title="Location",
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


async def _stage_preset(ctx: AppContext, snapshot: dict, pending: dict[str, Any]) -> None:
    """Pick a MeshCore radio preset and stage every field it names for review/apply.

    The rows are MeshCore's own suggested settings (see
    :data:`~meshterm.core.device_config.RADIO_PRESETS`), name in one lane and parameters
    in the other, and the list opens on the preset the radio is already tuned to where
    one matches — the "which of these am I on?" reading the phone app gives, so picking a
    neighbour is a comparison rather than a guess. Staged values count: a preset picked
    after a hand-edited frequency is read against what would be applied, not against what
    the radio still holds.
    """
    from ..core.device_config import RADIO_PRESETS, current_preset

    items = menu_rows([(p.name, p.summary, i) for i, p in enumerate(RADIO_PRESETS)])
    active = current_preset({**snapshot, **pending})
    idx = await ctx.ui.select(
        "Radio presets",
        items,
        prompt="Stage a standard set of radio parameters:",
        default=RADIO_PRESETS.index(active) if active is not None else None,
    )
    if idx is None:
        return
    pending.update(RADIO_PRESETS[idx].as_settings())


async def _stage_custom_var(
    ctx: AppContext, custom: dict[str, str], extra_ops: list[tuple]
) -> None:
    """Prompt for a custom/experimental variable and stage a set operation.

    Known variable names are offered as suggestions so an existing one can be recalled
    without retyping it; any new name is accepted as free text.

    Name then value, as a stack (:func:`~meshterm.ui.menus.run_steps`): Esc on the value
    steps back to the name it is for, rather than dropping both and starting over.
    """
    answers = await run_steps(
        [
            lambda vals: _ask_custom_name(ctx, custom, vals[0]),
            lambda vals: ctx.ui.text(
                "Custom variable",
                prompt=f"Value for {vals[0]}:",
                default=custom.get(vals[0], "") if vals[1] is None else vals[1],
            ),
        ]
    )
    if answers is None:
        return
    key, value = answers
    extra_ops.append(("set_custom", key, value))


async def _ask_custom_name(
    ctx: AppContext, custom: dict[str, str], previous: str | None
) -> str | None:
    """The variable-name step: suggestions where there are any, free text otherwise.

    Args:
        ctx: Shared application context.
        custom: The variables the device already reports, offered for recall.
        previous: What this step last returned, so coming back to it opens on it.

    Returns:
        The trimmed name, or ``None`` if it was left blank or cancelled.
    """
    prompt = "Name of the firmware variable to set:"
    if custom:
        typed = await ctx.ui.autocomplete(
            "Custom variable", sorted(custom), prompt=prompt, default=previous or ""
        )
    else:
        typed = await ctx.ui.text(
            "Custom variable", prompt=prompt, default=previous or ""
        )
    return typed.strip() if typed and typed.strip() else None


# --- device actions (run immediately) -----------------------------------------


async def device_actions(ctx: AppContext) -> None:
    """Run the Device actions screen: immediate operations on the companion itself.

    The action counterpart of :func:`edit_config`, behind the ``device-actions`` tool.
    Nothing here is staged — each action runs as soon as its own confirmation is given
    (destructive ones gate behind typing a confirmation word) and presents its result at
    once. The menu stays open between actions so several can be run in a row; Esc (or
    Back) returns to the main menu, and a reboot closes the screen and hands off to the
    session's reconnect dialog.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).
    """
    from .tui import CANCEL, SelectScreen

    device = await ctx.device()
    snapshot = await cached_snapshot(ctx, device)

    # Same persistent-backdrop pattern as the editor: the menu stays pushed while each
    # action's prompts float over it as modal popups (see edit_config).
    session = getattr(ctx.ui, "session", None)
    if session is None:  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("device actions are only available in the menu")
    # One screen for the whole visit: the action rows are fixed, so nothing here needs
    # rebuilding — and the cursor and any typed filter simply stay where the reader left them
    # while each action's prompts float over the list.
    menu = SelectScreen("Device actions", _action_items())
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL or choice is None:  # Esc
                return
            if choice == _SYNC_CLOCK:
                await _sync_clock(ctx, device, snapshot)
            elif choice == _BACKUP:
                await _backup_now(ctx, device, snapshot)
            elif choice == _RESTORE:
                if await _restore_now(ctx, device, snapshot):
                    snapshot = await build_snapshot(device)
            elif choice == _IDENTITY_KEY:
                if await _identity_key_menu(ctx, device, snapshot):
                    snapshot = await build_snapshot(device)
            elif choice == _REBOOT:
                if await _reboot(ctx, device, snapshot):
                    return  # the link is dropping; the reconnect dialog takes over
            elif choice == _RESET:
                if await _factory_reset(ctx, device, snapshot):
                    snapshot = await build_snapshot(device)


def _action_items() -> list:
    """Build the Device actions rows: label and description in two aligned columns.

    The shared menu-row presentation (see :func:`~meshterm.ui.menus.menu_rows`).
    Factory reset keeps its err-tinted label so the one irreversible row reads as such.
    """
    items = menu_rows(
        [
            ("🕒 Sync clock…", "Set the device clock from this computer", _SYNC_CLOCK),
            ("💾 Back up config to a file…", "Write every setting to TOML", _BACKUP),
            ("📂 Restore config from a backup…", "Preview or apply a saved TOML", _RESTORE),
            ("🔐 Identity key…", "Export or import the node's private key", _IDENTITY_KEY),
            ("🔄 Reboot device…", "Restart the companion and reconnect", _REBOOT),
            (
                Text("⚠ Factory reset…", style="err"),
                "Erase everything (typed confirmation)",
                _RESET,
            ),
        ]
    )
    return items


async def _run_now(
    ctx: AppContext, device: Device, snapshot: dict, ops: list[tuple], title: str
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


async def send_advert(ctx: AppContext) -> None:
    """Run the Send advert flow, behind the main menu's ``advert`` popup tool.

    Send an advert (zero-hop or flood) or show this node's shareable contact card. Either
    advert waits out the transmit cooldown first — a flood advert its own, longer one —
    which is a countdown the reader can cancel where the wait is long enough to notice.
    Only
    ``SELF_INFO`` is read here: the advert command itself never consults the snapshot,
    and the contact card needs just the name, public key, and advert type — so opening
    this skips the tuning/path-hash reads of a full
    :func:`~meshterm.core.device_config.build_snapshot` and stays snappy over Bluetooth.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).
    """
    device = await ctx.device()
    snapshot = dict(await ctx.devstate.self_info())  # the session cache; no re-read
    # No emoji in this floating dialog's title: a terminal that paints an emoji a cell
    # narrower than Rich measures it leaves the content-sized popup's title border short,
    # bleeding the backdrop through the frame. The menu row keeps its 📡 icon (drawn in the
    # full-width base, where the miscount has nowhere to show). filterable=False too: a
    # stray key must not narrow (and so resize) this fixed four-item list.
    choice = await ctx.ui.select(
        "Send advert",
        [
            *menu_rows(
                [
                    ("Zero-hop", "Announce to direct neighbours", "zero"),
                    ("Flood", "Repeaters rebroadcast it across the mesh", "flood"),
                    ("Share QR / URI", "Show this node's contact card", "share"),
                ]
            ),
        ],
        prompt="Announce this node to the mesh:",
        filterable=False,
    )
    if choice is None:
        return
    if choice == "share":
        await _show_contact_card(ctx, snapshot)
        return

    # An advert is the one transmission a reader fires by hand, over and over, so it is
    # where the shared transmit cooldown is felt (see meshterm.ui.cooldown). A wait worth
    # noticing becomes a countdown they can back out of; a short one just happens.
    from .cooldown import wait_for_cooldown

    flood = choice == "flood"
    if not await wait_for_cooldown(
        ctx, action="Flood advert" if flood else "Zero-hop advert", flood_advert=flood
    ):
        return
    await _run_now(ctx, device, snapshot, [("advert", flood)], "Advert")


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


async def show_contact_card(
    ctx: AppContext, name: str, public_key: str, node_type: int = 1
) -> None:
    """Pop up a node's contact card: a scannable QR code over the raw share link.

    THE share-a-contact popup, used both for our own node (the advert menu's
    ``Share QR / URI``) and for any full-keyed contact (the node detail page's
    ``Share contact``): a phone scans the code — or the link is passed along as text —
    and the companion app adds the node as a contact.

    Args:
        ctx: Shared application context (provides the UI surface).
        name: The node's advertised name (the card's title and the link's name field).
        public_key: The node's full public key as hex (the link is useless without it).
        node_type: The MeshCore advert type byte (1 companion, 2 repeater, 3 room,
            4 sensor).
    """
    from .qr import share_popup

    await share_popup(
        ctx,
        name=name,
        url=contact_share_url(name, public_key, node_type),
        intro="Scan to add this node as a contact:",
    )


async def _show_contact_card(ctx: AppContext, snapshot: dict) -> None:
    """Show our own node's contact card from its ``SELF_INFO`` snapshot."""
    public_key = str(snapshot.get("public_key") or "")
    if not public_key:
        ctx.ui.note("[err]the device did not report a public key — nothing to share[/err]")
        await ctx.ui.present(title="Share contact")
        return
    name = str(snapshot.get("name") or "this node")
    await show_contact_card(ctx, name, public_key, int(snapshot.get("adv_type") or 1))


async def _reboot(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Confirm and reboot the device, handing off to the session's reconnect dialog.

    After the command is sent, we wait to actually observe the link dropping — flagging
    :attr:`~meshterm.context.AppContext.reboot_in_progress` so the session-wide
    disconnect watcher labels the ensuing dialog as a reboot, waits for the companion to
    come back, and reconnects — exactly the unplugged-device flow.

    Returns:
        ``True`` if the reboot was sent and the actions screen should close; ``False``
        if the user backed out (or the simulator, which has no link to drop, absorbed it).
    """
    choice = await ctx.ui.dialog(
        "Reboot the device now?",
        [("Cancel", None), ("Reboot", "reboot")],
        title="Reboot device",
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
    # actions screen doesn't flash back to the menu for the second or two before the
    # watcher notices. The watcher may cancel us mid-wait when it fires — that's the handoff.
    deadline = asyncio.get_running_loop().time() + _REBOOT_DROP_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        if not await ctx.link_alive():
            break
        await asyncio.sleep(_REBOOT_DROP_POLL_S)
    return True


async def _sync_clock(ctx: AppContext, device: Device, snapshot: dict) -> None:
    """Show the device clock's drift against this computer and offer to correct it.

    A companion that boots with a bad RTC stamps every message wrongly, so the dialog
    leads with the measured drift (or admits the clock is unreadable) before the Sync
    button writes the host's time.
    """
    import time

    device_time: int | None = None
    try:
        device_time = await device.get_time()
    except Exception:  # noqa: BLE001 - old firmware; offer the blind sync instead
        pass
    if device_time:
        drift = device_time - int(time.time())
        stamp = _clock_text(device_time)
        prompt = (
            f"The device clock reads {stamp} — "
            f"{_drift_text(drift)}. Set it from this computer?"
        )
    else:
        prompt = (
            "The device did not report its clock. Set it from this computer anyway?"
        )
    choice = await ctx.ui.dialog(
        prompt,
        [("Cancel", None), ("Sync", "sync")],
        title="Sync clock",
        default=1,
    )
    if choice == "sync":
        await _run_now(ctx, device, snapshot, [("sync_clock",)], "Sync clock")


def _clock_text(epoch: int) -> str:
    """A device timestamp rendered in this computer's local time."""
    from datetime import datetime

    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _drift_text(drift: int) -> str:
    """Describe a clock drift in seconds: ``"12 s behind"``, ``"3 s ahead"``, ``"in sync"``."""
    if abs(drift) < 2:
        return "in sync with this computer"
    direction = "ahead of" if drift > 0 else "behind"
    return f"{abs(drift)} s {direction} this computer"


async def _backup_now(ctx: AppContext, device: Device, snapshot: dict) -> None:
    """Prompt for a destination and write the TOML backup immediately."""
    path = await ctx.ui.path(
        "Back up config",
        prompt="Write every setting to this TOML file:",
        default="meshterm-config.toml",
    )
    if path:
        await _run_now(ctx, device, snapshot, [("backup", Path(path))], "Backup")


async def _restore_now(ctx: AppContext, device: Device, snapshot: dict) -> bool:
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
        title="Restore from backup",
        default=1,
    )
    if choice == "preview":
        await _run_now(ctx, device, snapshot, [("restore", path, True)], "Restore preview")
        choice = await ctx.ui.dialog(
            "Apply these changes to the device?",
            [("Cancel", None), ("Apply", "apply")],
            title="Restore from backup",
            default=1,
        )
    if choice != "apply":
        return False
    changed = await _run_now(ctx, device, snapshot, [("restore", path, False)], "Restore")
    return changed > 0


async def _identity_key_menu(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Export or import the device's private identity key.

    Returns:
        ``True`` if the identity changed (a key was imported), so the caller re-reads
        its snapshot.
    """
    choice = await ctx.ui.select(
        "Identity key",
        [
            *menu_rows(
                [
                    ("Show private key", "Display it on screen (sensitive)", "show"),
                    ("Export to a file…", "Write it to disk (keep it secret)", "file"),
                    ("Import a key…", "Replace this device's identity", "import"),
                ]
            ),
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
            title="Show private key",
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
        title="Import private key",
    )
    if not confirmed:
        return False
    await _run_now(ctx, device, snapshot, [("import_key", key_hex.strip())], "Import key")
    return True


async def _factory_reset(ctx: AppContext, device: Device, snapshot: dict) -> bool:
    """Factory-reset the device behind a typed confirmation.

    Returns:
        ``True`` if the reset ran (so the caller drops everything it staged and re-reads
        the device).
    """
    confirmed = await ctx.ui.typed_confirm(
        "This erases EVERYTHING on the device — identity, contacts, channels, and every "
        "setting — and cannot be undone.",
        "RESET",
        title="Factory reset",
    )
    if not confirmed:
        return False
    await _run_now(ctx, device, snapshot, [("factory_reset",)], "Factory reset")
    return True


# --- validators --------------------------------------------------------------


def _is_hex(text: str) -> bool | str:
    """Validate that ``text`` is a hex string."""
    try:
        bytes.fromhex(text)
        return True
    except ValueError:
        return "Enter hex characters only."
