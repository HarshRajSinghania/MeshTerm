"""Tests for the device-configuration registry, backup/restore, and safety gating.

All run against :class:`MockDevice` — no hardware required.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from meshterm.core.config_io import backup_config, plan_restore, read_backup
from meshterm.core.connection import MockDevice
from meshterm.core.device_config import (
    RADIO_PRESETS,
    DeviceConfigError,
    build_snapshot,
    effective_maximum,
    format_value,
    get_spec,
    parse_value,
)
from meshterm.tools.config import _require_yes


async def _connected_mock() -> MockDevice:
    """Return a connected MockDevice."""
    device = MockDevice()
    await device.connect()
    return device


# -- parsing / formatting ------------------------------------------------------


def test_parse_value_types() -> None:
    """Each value type coerces from string correctly."""
    assert parse_value(get_spec("name"), "Node-7") == "Node-7"
    assert parse_value(get_spec("radio_sf"), "9") == 9
    assert parse_value(get_spec("radio_freq"), "868.0") == 868.0
    assert parse_value(get_spec("manual_add_contacts"), "yes") is True
    assert parse_value(get_spec("manual_add_contacts"), "off") is False


def test_parse_value_range_and_enum_errors() -> None:
    """Out-of-range and invalid enum values raise DeviceConfigError."""
    with pytest.raises(DeviceConfigError, match="<= 12"):
        parse_value(get_spec("radio_sf"), "99")
    with pytest.raises(DeviceConfigError, match=">= 5"):
        parse_value(get_spec("radio_cr"), "1")
    with pytest.raises(DeviceConfigError, match="one of"):
        parse_value(get_spec("telemetry_mode_base"), "7")
    with pytest.raises(DeviceConfigError, match="boolean"):
        parse_value(get_spec("manual_add_contacts"), "maybe")


def test_format_value() -> None:
    """Formatting renders booleans, enums, and unknowns readably."""
    assert format_value(get_spec("manual_add_contacts"), True) == "true"
    assert format_value(get_spec("telemetry_mode_base"), 1) == "1 (always)"
    assert format_value(get_spec("name"), None) == "?"


def test_path_hash_mode_is_strict_enum() -> None:
    """path_hash_mode accepts its four modes and rejects anything else."""
    assert parse_value(get_spec("path_hash_mode"), "3") == 3
    assert format_value(get_spec("path_hash_mode"), 0) == "0 (1-byte hashes (default))"
    with pytest.raises(DeviceConfigError, match="one of"):
        parse_value(get_spec("path_hash_mode"), "4")


def test_highlighted_hash_highlights_path_hash_prefix() -> None:
    """The full key is shown with only its path-hash prefix bytes highlighted, lit in
    the node's hash-derived palette hue (the same hue the node's name wears)."""
    from meshterm.ui.theme import node_style
    from meshterm.ui.widgets import highlighted_hash

    pub = "aabbccddee" + "00" * 27
    # mode 2 => 3-byte hashes => first 6 hex chars are the addressable prefix.
    text = highlighted_hash(pub, prefix_bytes=3)
    assert text.plain == pub  # the full key is shown
    highlighted = [s for s in text.spans if s.style == node_style(pub)]
    assert len(highlighted) == 1
    assert text.plain[highlighted[0].start : highlighted[0].end] == "aabbcc"


def test_highlighted_hash_truncates_on_a_byte_boundary() -> None:
    """A key too long for the budget keeps whole leading bytes (an even digit count) plus
    an ellipsis, padding to the exact width — a half-byte digit never shows."""
    from meshterm.ui.widgets import highlighted_hash

    pub = "ab" * 32  # 64 hex digits
    # An even budget: width - 1 is odd, so the last (half-byte) digit is dropped and the
    # freed cell pads out — 14 digits (7 bytes) shown, not 15.
    text = highlighted_hash(pub, prefix_bytes=1, width=16)
    assert len(text.plain) == 16  # the lane still spans the full budget
    visible = text.plain.rstrip().rstrip("…")
    assert visible == "ab" * 7 and len(visible) % 2 == 0
    assert "…" in text.plain
    # An odd budget already lands on a boundary: width - 1 = 14 digits, no padding.
    assert highlighted_hash(pub, prefix_bytes=1, width=15).plain == "ab" * 7 + "…"


def _contacts_names(counts, sort: str, prefix_bytes: int = 3):
    """Render a contacts table for ``sort`` and return (table, name-column plain text)."""
    from meshterm.core.models import Contact
    from meshterm.ui.widgets import ContactsSort, contacts_table

    contacts = [
        Contact(name="Bob", public_key="9f1a2b" + "00" * 29, key_prefix="9f1a2b"),
        Contact(name="Alice", public_key="3d63c6" + "00" * 29, key_prefix="3d63c6"),
    ]
    group = contacts_table(
        "Homestead", "aabbcc" + "00" * 29, contacts, prefix_bytes, counts, ContactsSort.from_name(sort)
    )
    table = group.renderables[0]  # (table, blank line, legend)
    names = [c.plain if hasattr(c, "plain") else str(c) for c in table.columns[1].cells]
    return table, names


def test_contacts_table_lists_us_first_with_full_keys() -> None:
    """The contacts table pins our node first, sorts by name, and shows full highlighted keys."""
    counts = {"3d63c6000000": 14}  # Alice was overheard; Bob wasn't
    table, names = _contacts_names(counts, sort="name")

    # Column 1 is the name; us first, then contacts alphabetically (Alice before Bob).
    assert names[0].startswith("Homestead")
    assert names[1] == "Alice"
    assert names[2] == "Bob"

    # Packet counts (column 3): Alice shows her tally, Bob shows the em dash.
    pkts = [c.plain if hasattr(c, "plain") else str(c) for c in table.columns[3].cells]
    assert pkts[1] == "14"
    assert pkts[2] == "—"

    # Keys (column 4) are the full key with the path-hash prefix highlighted (no truncation
    # in the model; Rich ellipsizes only at render time when the terminal is too narrow).
    from meshterm.ui.theme import node_style

    keys = list(table.columns[4].cells)
    assert keys[1].plain == "3d63c6" + "00" * 29
    # The prefix is highlighted in the key's own hash-derived hue.
    assert any(s.style == node_style("3d63c6") for s in keys[1].spans)


def test_contacts_table_sorts_by_heard_and_packets() -> None:
    """Non-default sorts reorder contacts while keeping our node pinned first."""
    from datetime import timedelta

    from meshterm.core.models import Contact, utcnow

    # Bob was heard more recently than Alice, but Alice has more overheard packets.
    contacts = [
        Contact(
            name="Bob",
            public_key="9f1a2b" + "00" * 29,
            key_prefix="9f1a2b",
            last_seen=utcnow() - timedelta(minutes=5),
        ),
        Contact(
            name="Alice",
            public_key="3d63c6" + "00" * 29,
            key_prefix="3d63c6",
            last_seen=utcnow() - timedelta(days=2),
        ),
    ]
    counts = {"3d63c6000000": 14, "9f1a2b000000": 3}
    from meshterm.ui.widgets import ContactsSort, contacts_table

    def names(sort: ContactsSort):
        group = contacts_table("Us", "aabbcc" + "00" * 29, contacts, 3, counts, sort)
        cells = group.renderables[0].columns[1].cells
        return [c.plain if hasattr(c, "plain") else str(c) for c in cells]

    # Freshest first: Bob (5m) before Alice (2d) under the default (ascending-age) heard sort.
    assert names(ContactsSort.from_name("heard"))[1:] == ["Bob", "Alice"]
    # Most packets first: Alice (14) before Bob (3) under the default (descending) packets sort.
    assert names(ContactsSort.from_name("packets"))[1:] == ["Alice", "Bob"]
    # Descending flips it: oldest-heard first puts Alice (2d) above Bob (5m).
    assert names(ContactsSort(column="heard", ascending=False))[1:] == ["Alice", "Bob"]


def test_contacts_table_breaks_metric_ties_by_name_ascending() -> None:
    """Two contacts with the same metric keep an A→Z order in both sort directions (no flip)."""
    from datetime import timedelta

    from meshterm.core.models import Contact, utcnow
    from meshterm.ui.widgets import ContactsSort, _ordered_contacts

    same = utcnow() - timedelta(minutes=5)
    contacts = [
        Contact(name="Charlie", public_key="aa" * 16, last_seen=same),
        Contact(name="Alice", public_key="bb" * 16, last_seen=same),
        Contact(name="Bob", public_key="cc" * 16, last_seen=utcnow() - timedelta(hours=3)),
    ]

    def order(ascending: bool) -> list[str]:
        sort = ContactsSort(column="heard", ascending=ascending)
        return [c.name for c in _ordered_contacts(contacts, {}, sort)]

    # Freshest first: the two 5-min contacts lead, ordered Alice→Charlie, then Bob (3h).
    assert order(ascending=True) == ["Alice", "Charlie", "Bob"]
    # Oldest first: Bob leads, but the tie still breaks Alice→Charlie — not Charlie→Alice.
    assert order(ascending=False) == ["Bob", "Alice", "Charlie"]


def _contacts_sort(name: str = "name"):
    """The interactive Contacts screen's sort: the shared contact list's four-column ring."""
    from meshterm.ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
    from meshterm.ui.widgets import ContactsSort

    return ContactsSort.from_name(name, SORT_COLUMNS, SORT_OPENS_ASCENDING)


def test_contacts_screen_ctrl_arrows_steer_the_sort() -> None:
    """Ctrl+←/→ walk the shared four-column ring (wrapping); Ctrl+↑/↓ force the direction —
    the Time Machine picker's keys, now the Contacts screen's too."""
    from meshterm.ui.contacts_screen import ContactsScreen

    screen = ContactsScreen("Us", "aabbcc" + "00" * 29, [], 3, {}, _contacts_sort())
    assert (screen._sort.column, screen._sort.ascending) == ("name", True)

    screen.handle("ctrl_right")  # name -> heard, opening in its natural (ascending) direction
    assert (screen._sort.column, screen._sort.ascending) == ("heard", True)
    screen.handle("ctrl_right")  # heard -> packets, which opens descending
    assert (screen._sort.column, screen._sort.ascending) == ("packets", False)
    screen.handle("ctrl_up")  # force ascending
    assert screen._sort.ascending is True
    screen.handle("ctrl_down")  # force descending
    assert screen._sort.ascending is False
    screen.handle("ctrl_right")  # packets -> hash, the ring's fourth column
    assert (screen._sort.column, screen._sort.ascending) == ("hash", True)
    screen.handle("ctrl_right")  # hash -> wraps back to name
    assert screen._sort.column == "name"
    screen.handle("ctrl_left")  # name -> wraps to hash
    assert screen._sort.column == "hash"


def test_contacts_screen_lists_contacts_in_the_shared_lanes() -> None:
    """Contacts render in the shared NAME/HEARD/PKTS/KEY lanes, our node pinned first;
    plain arrows only move the highlight, and Enter resolves the highlighted node."""
    import re

    from meshterm.core.models import Contact, utcnow
    from meshterm.ui.contacts_screen import YOU, ContactsScreen
    from meshterm.ui.tui.screen import CANCEL

    contacts = [
        Contact(name="Alice", public_key="aa" * 32, last_seen=utcnow()),
        Contact(name="Bob", public_key="bb" * 32),
    ]
    counts = {"aa" * 6: 7}  # Alice was overheard; Bob never
    screen = ContactsScreen("Us", "cc" * 32, contacts, 1, counts, _contacts_sort())
    body = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in screen.render_body(72))

    # The shared column header (recorded sticky, so it pins once scrolled past)...
    assert "NAME" in body and "HEARD" in body and "PKTS" in body and "KEY" in body
    assert screen._sticky_headers
    # ...our own node leading the lanes, then the contacts with their tallies.
    choices = screen._choices()
    assert choices[0].value == YOU
    assert "(you)" in choices[0].label.plain
    assert "Alice" in body and "Bob" in body
    assert f"{7:>5}" in body  # Alice's overheard packets, right-aligned in its lane
    assert "never" in body  # Bob has no last_seen
    # Each contact row carries the Contact itself, so Enter hands the whole record on.
    assert all(isinstance(c.value, Contact) for c in choices[1:])

    # Plain arrows move the highlight without touching the sort.
    before = (screen._sort.column, screen._sort.ascending)
    screen.handle("down")
    assert (screen._sort.column, screen._sort.ascending) == before
    highlighted = screen._current_choice().value
    assert highlighted != YOU and isinstance(highlighted, Contact)

    # Enter resolves the highlighted contact (open_contacts opens its Node detail);
    # Esc still leaves with CANCEL.
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.handle("enter")
    assert resolved == [highlighted]
    screen.handle("escape")
    assert resolved == [highlighted, CANCEL]


def test_non_strict_enum_accepts_unlisted_value() -> None:
    """multi_acks/adv_loc_policy list common values but still accept other ints."""
    spec = get_spec("multi_acks")
    assert spec.value_type == "enum" and not spec.strict_choices
    assert parse_value(spec, "5") == 5  # not in choices, but >= minimum: accepted
    with pytest.raises(DeviceConfigError, match=">= 0"):
        parse_value(spec, "-1")  # still range-checked


def test_radio_presets_map_to_radio_settings() -> None:
    """Every preset exposes the four radio fields by their registry keys."""
    assert RADIO_PRESETS, "expected at least one standard radio preset"
    for preset in RADIO_PRESETS:
        settings = preset.as_settings()
        assert set(settings) == {"radio_freq", "radio_bw", "radio_sf", "radio_cr"}
        # Each value parses cleanly through its setting spec.
        for key, value in settings.items():
            assert parse_value(get_spec(key), value) == value


def test_get_spec_unknown_raises() -> None:
    """An unknown key produces a helpful error listing valid keys."""
    with pytest.raises(DeviceConfigError, match="unknown setting"):
        get_spec("not_a_setting")


# -- apply against the mock device ---------------------------------------------


async def test_apply_simple_setting() -> None:
    """Setting the node name round-trips through the device."""
    device = await _connected_mock()
    spec = get_spec("name")
    await spec.apply(device, parse_value(spec, "Yagi-Hub"), await build_snapshot(device))
    assert (await device.get_self_info())["name"] == "Yagi-Hub"


async def test_apply_coupled_radio_field_preserves_others() -> None:
    """Changing one radio field rebuilds set_radio from the current snapshot."""
    device = await _connected_mock()
    spec = get_spec("radio_sf")
    await spec.apply(device, 9, await build_snapshot(device))
    info = await device.get_self_info()
    assert info["radio_sf"] == 9
    assert info["radio_freq"] == 869.525  # unchanged
    assert info["radio_bw"] == 250.0
    assert info["radio_cr"] == 5


async def test_apply_telemetry_mode_preserves_siblings() -> None:
    """Editing one telemetry mode leaves the other two intact."""
    device = await _connected_mock()
    spec = get_spec("telemetry_mode_loc")
    await spec.apply(device, 2, await build_snapshot(device))
    info = await device.get_self_info()
    assert info["telemetry_mode_loc"] == 2
    assert info["telemetry_mode_base"] == 0
    assert info["telemetry_mode_env"] == 0


async def test_custom_var_round_trip() -> None:
    """Custom variables persist on the device."""
    device = await _connected_mock()
    await device.set_custom_var("exp_flag", "1")
    assert (await device.get_custom_vars())["exp_flag"] == "1"


# -- backup / restore ----------------------------------------------------------


async def test_backup_and_read_round_trip(tmp_path: Path) -> None:
    """A backup writes all settings, custom vars, and channels and reads back."""
    device = await _connected_mock()
    await device.set_name("Foo")
    await device.set_channel(0, "Public", b"\x01" * 16)
    snapshot = await build_snapshot(device)
    path = backup_config(tmp_path / "cfg.toml", snapshot, {"exp": "1"}, await _channels(device))

    backup = read_backup(path)
    assert backup.settings["name"] == "Foo"
    assert backup.settings["radio_sf"] == 11
    assert backup.custom["exp"] == "1"
    assert backup.channels[0]["name"] == "Public"
    assert backup.channels[0]["secret"] == ("01" * 16)


async def test_plan_restore_emits_only_differences(tmp_path: Path) -> None:
    """The restore planner returns ops only for values that differ from the device."""
    source = await _connected_mock()
    await source.set_name("Foo")
    path = backup_config(
        tmp_path / "cfg.toml", await build_snapshot(source), {"exp": "1"}, []
    )
    backup = read_backup(path)

    target = await _connected_mock()  # fresh device: name still "MockCompanion"
    ops = plan_restore(backup, await build_snapshot(target), await target.get_custom_vars())

    assert ("set", "name", "Foo") in ops
    assert ("set_custom", "exp", "1") in ops
    # Unchanged values (e.g. radio_sf 11 == 11) are not re-applied.
    assert "radio_sf" not in [op[1] for op in ops if op[0] == "set"]


# -- the device-reported TX power ceiling ---------------------------------------


def test_tx_power_capped_by_device_reported_maximum() -> None:
    """With a snapshot, the board's max_tx_power tightens the static bound."""
    spec = get_spec("tx_power")
    snapshot = {"max_tx_power": 22}
    assert parse_value(spec, "22", snapshot) == 22
    with pytest.raises(DeviceConfigError, match="<= 22"):
        parse_value(spec, "23", snapshot)
    # Without a snapshot only the static ceiling applies.
    assert parse_value(spec, "27") == 27
    assert effective_maximum(spec, snapshot) == 22
    assert effective_maximum(spec, None) == 30


def test_effective_maximum_never_loosens_the_static_bound() -> None:
    """A device reporting a max above the static ceiling doesn't raise it."""
    spec = get_spec("tx_power")
    assert effective_maximum(spec, {"max_tx_power": 99}) == 30
    assert effective_maximum(spec, {"max_tx_power": "bogus"}) == 30  # unparseable -> static


# -- flood scope / auto-add / TX delays ------------------------------------------


def test_flood_scope_length_limited_and_formats_empty() -> None:
    """The scope name respects its 31-byte protocol slot; empty renders readably."""
    spec = get_spec("flood_scope")
    assert parse_value(spec, "alpha") == "alpha"
    with pytest.raises(DeviceConfigError, match="at most 30"):
        parse_value(spec, "x" * 31)
    assert format_value(spec, "") == "(not set)"
    assert format_value(spec, "#alpha") == "#alpha"


async def test_flood_scope_round_trips_with_hashtag_normalization() -> None:
    """Applying a bare scope name stores it with its leading #; empty clears it."""
    device = await _connected_mock()
    spec = get_spec("flood_scope")
    await spec.apply(device, "alpha", await build_snapshot(device))
    assert (await build_snapshot(device))["flood_scope"] == "#alpha"
    await spec.apply(device, "", await build_snapshot(device))
    assert (await build_snapshot(device))["flood_scope"] == ""


async def test_autoadd_config_round_trips() -> None:
    """The auto-add bitmask reaches the device and reads back into the snapshot."""
    device = await _connected_mock()
    spec = get_spec("autoadd_config")
    await spec.apply(device, parse_value(spec, "5"), await build_snapshot(device))
    assert (await build_snapshot(device))["autoadd_config"] == 5


def test_tuning_fields_are_floats_with_firmware_ranges() -> None:
    """RX delay and airtime factor take real (unscaled) float values, firmware-bounded.

    The firmware stores both as floats (rx_delay_base 0–20 s, airtime_factor 0–9) and
    only the *wire* carries them ×1000 — the settings must speak the real units.
    """
    assert parse_value(get_spec("rx_delay"), "0.5") == 0.5
    assert parse_value(get_spec("airtime_factor"), "2.5") == 2.5
    with pytest.raises(DeviceConfigError, match="<= 20"):
        parse_value(get_spec("rx_delay"), "500")  # a raw wire value must be rejected
    with pytest.raises(DeviceConfigError, match="<= 9"):
        parse_value(get_spec("airtime_factor"), "1000")


def test_tx_delay_factors_are_not_companion_settings() -> None:
    """The repeater-only TX delay factors must not appear as (no-op) device settings.

    Companion firmware's CMD_SET_TUNING_PARAMS reads exactly rx_delay + airtime_factor
    and ignores trailing bytes, so offering these knobs here would silently do nothing.
    """
    with pytest.raises(DeviceConfigError, match="unknown setting"):
        get_spec("tx_delay_factor")
    with pytest.raises(DeviceConfigError, match="unknown setting"):
        get_spec("direct_tx_delay_factor")


async def test_apply_tuning_field_preserves_the_other() -> None:
    """Editing one tuning field resends the pair without clobbering its sibling."""
    device = await _connected_mock()
    await device.set_tuning(0.5, 2.0)
    spec = get_spec("airtime_factor")
    await spec.apply(device, 3.5, await build_snapshot(device))
    assert await device.get_tuning() == {"rx_delay": 0.5, "airtime_factor": 3.5}


# -- clock sync -------------------------------------------------------------------


async def test_mock_clock_drifts_until_set_time_corrects_it() -> None:
    """The simulator's clock reads behind the host until set_time re-anchors it."""
    import time as _time

    device = await _connected_mock()
    before = await device.get_time()
    assert before is not None and before < int(_time.time())  # seeded drift
    now = int(_time.time())
    await device.set_time(now)
    after = await device.get_time()
    assert after is not None and abs(after - now) <= 1


# -- the Device info status panel -------------------------------------------------


async def test_status_panel_reports_role_battery_clock_and_stats() -> None:
    """The info tool's status panel surfaces the read-only device state in one place."""
    import io

    from rich.console import Console

    from meshterm.tools.info import _status_panel
    from meshterm.ui.theme import MESH_THEME

    device = await _connected_mock()
    panel = await _status_panel(device, await build_snapshot(device))
    console = Console(theme=MESH_THEME, file=io.StringIO(), width=100)
    console.print(panel)
    out = console.file.getvalue()

    assert "companion" in out  # adv_type 1 rendered as the node role
    assert "MeshCore Simulator" in out
    assert "4.10 V" in out
    assert "1d 2h 3m" in out  # 93784 s of simulated uptime
    assert "s behind" in out  # the mock's seeded clock drift
    assert "-110 dBm noise floor" in out
    assert "210 sent" in out and "1234 received" in out


def test_uptime_renders_compact_units() -> None:
    """Uptime shows seconds only under a minute, then d/h/m parts as needed."""
    from meshterm.tools.info import _uptime

    assert _uptime(45) == "45 s"
    assert _uptime(3660) == "1h 1m"
    assert _uptime(93784) == "1d 2h 3m"
    assert _uptime(86400) == "1d 0m"


# -- safety gating -------------------------------------------------------------


def test_require_yes_blocks_without_confirmation() -> None:
    """Destructive CLI commands abort unless --yes is passed."""
    with pytest.raises(typer.Exit):
        _require_yes(False, "factory reset erases all data")
    _require_yes(True, "factory reset erases all data")  # does not raise


async def _channels(device: MockDevice) -> list[dict]:
    """Collect the mock's configured channels for backup."""
    channels = []
    for idx in range(8):
        ch = await device.get_channel(idx)
        if ch:
            channels.append(ch)
    return channels
