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


def _nodes_names(counts, sort: str, prefix_bytes: int = 3):
    """Render a nodes table for ``sort`` and return (table, name-column plain text)."""
    from meshterm.core.models import Contact
    from meshterm.ui.widgets import NodesSort, nodes_table

    contacts = [
        Contact(name="Bob", public_key="9f1a2b" + "00" * 29, key_prefix="9f1a2b"),
        Contact(name="Alice", public_key="3d63c6" + "00" * 29, key_prefix="3d63c6"),
    ]
    group = nodes_table(
        "Homestead", "aabbcc" + "00" * 29, contacts, prefix_bytes, counts, NodesSort.from_name(sort)
    )
    table = group.renderables[0]  # (table, blank line, legend)
    names = [c.plain if hasattr(c, "plain") else str(c) for c in table.columns[1].cells]
    return table, names


def test_nodes_table_lists_us_first_with_full_keys() -> None:
    """The nodes table pins our node first, sorts by name, and shows full highlighted keys."""
    counts = {"3d63c6000000": 14}  # Alice was overheard; Bob wasn't
    table, names = _nodes_names(counts, sort="name")

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
    keys = list(table.columns[4].cells)
    assert keys[1].plain == "3d63c6" + "00" * 29
    assert any(s.style == "brand" for s in keys[1].spans)  # prefix highlighted


def test_nodes_table_sorts_by_heard_and_packets() -> None:
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
    from meshterm.ui.widgets import NodesSort, nodes_table

    def names(sort: NodesSort):
        group = nodes_table("Us", "aabbcc" + "00" * 29, contacts, 3, counts, sort)
        cells = group.renderables[0].columns[1].cells
        return [c.plain if hasattr(c, "plain") else str(c) for c in cells]

    # Freshest first: Bob (5m) before Alice (2d) under the default (ascending-age) heard sort.
    assert names(NodesSort.from_name("heard"))[1:] == ["Bob", "Alice"]
    # Most packets first: Alice (14) before Bob (3) under the default (descending) packets sort.
    assert names(NodesSort.from_name("packets"))[1:] == ["Alice", "Bob"]
    # Descending flips it: oldest-heard first puts Alice (2d) above Bob (5m).
    assert names(NodesSort(column="heard", ascending=False))[1:] == ["Alice", "Bob"]


def test_nodes_table_breaks_metric_ties_by_name_ascending() -> None:
    """Two nodes with the same metric keep an A→Z order in both sort directions (no flip)."""
    from datetime import timedelta

    from meshterm.core.models import Contact, utcnow
    from meshterm.ui.widgets import NodesSort, _ordered_contacts

    same = utcnow() - timedelta(minutes=5)
    contacts = [
        Contact(name="Charlie", public_key="aa" * 16, last_seen=same),
        Contact(name="Alice", public_key="bb" * 16, last_seen=same),
        Contact(name="Bob", public_key="cc" * 16, last_seen=utcnow() - timedelta(hours=3)),
    ]

    def order(ascending: bool) -> list[str]:
        sort = NodesSort(column="heard", ascending=ascending)
        return [c.name for c in _ordered_contacts(contacts, {}, sort)]

    # Freshest first: the two 5-min nodes lead, ordered Alice→Charlie, then Bob (3h).
    assert order(ascending=True) == ["Alice", "Charlie", "Bob"]
    # Oldest first: Bob leads, but the tie still breaks Alice→Charlie — not Charlie→Alice.
    assert order(ascending=False) == ["Bob", "Alice", "Charlie"]


def test_nodes_screen_arrows_steer_the_sort() -> None:
    """Left/right walk the sort column (wrapping); up/down set ascending/descending."""
    from meshterm.ui.nodes_screen import NodesScreen
    from meshterm.ui.widgets import NodesSort

    screen = NodesScreen("Us", "aabbcc" + "00" * 29, [], 3, {}, NodesSort())
    assert (screen._sort.column, screen._sort.ascending) == ("name", True)

    screen.handle("right")  # name -> heard, opening in its natural (ascending) direction
    assert (screen._sort.column, screen._sort.ascending) == ("heard", True)
    screen.handle("right")  # heard -> packets, which opens descending
    assert (screen._sort.column, screen._sort.ascending) == ("packets", False)
    screen.handle("up")  # force ascending
    assert screen._sort.ascending is True
    screen.handle("down")  # force descending
    assert screen._sort.ascending is False
    screen.handle("right")  # packets -> wraps back to name
    assert screen._sort.column == "name"
    screen.handle("left")  # name -> wraps to packets
    assert screen._sort.column == "packets"


def test_nodes_screen_pins_column_header_when_scrolled() -> None:
    """The Name/Heard/Pkts/Key labels stick to the top row once the rows scroll past them."""
    import re

    from meshterm.core.models import Contact
    from meshterm.ui.nodes_screen import NodesScreen
    from meshterm.ui.widgets import NodesSort

    contacts = [Contact(name=f"n{i}", public_key="ab" * 16) for i in range(12)]
    screen = NodesScreen("Us", "aabbcc" + "00" * 29, contacts, 3, {}, NodesSort())
    screen.render_body(70)

    # Exactly one sticky header — the column-label row — recorded above the table's rule.
    (idx, header), = screen._sticky_headers
    labels = re.sub(r"\x1b\[[0-9;]*m", "", header)
    assert "Name" in labels and "Heard" in labels and "Key" in labels

    # Not pinned while the header is still on screen; pinned once scrolled below it.
    assert screen.sticky_header(idx) is None
    assert screen.sticky_header(idx + 5) == header


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
