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
    """The full key is shown with only its path-hash prefix bytes highlighted."""
    from meshterm.ui.widgets import highlighted_hash

    pub = "aabbccddee" + "00" * 27
    # mode 2 => 3-byte hashes => first 6 hex chars are the addressable prefix.
    text = highlighted_hash(pub, prefix_bytes=3)
    assert text.plain == pub  # the full key is shown
    highlighted = [s for s in text.spans if s.style == "brand"]
    assert len(highlighted) == 1
    assert text.plain[highlighted[0].start : highlighted[0].end] == "aabbcc"


def test_nodes_table_lists_us_first_with_full_keys() -> None:
    """The nodes table puts our node first (name highlighted) with full keys."""
    from meshterm.core.models import Contact
    from meshterm.ui.widgets import nodes_table

    contacts = [Contact(name="Alice", public_key="3d63c6" + "00" * 29, key_prefix="3d63c6")]
    table = nodes_table("Homestead", "aabbcc" + "00" * 29, contacts, prefix_bytes=3)
    # Two body rows: our node, then Alice.
    names = [cell.plain if hasattr(cell, "plain") else str(cell) for cell in table.columns[0].cells]
    assert names[0].startswith("Homestead")
    assert "Alice" in names[1]
    keys = list(table.columns[1].cells)
    assert keys[0].plain == "aabbcc" + "00" * 29  # full key, not a short prefix
    assert any(s.style == "brand" for s in keys[0].spans)  # prefix highlighted


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
