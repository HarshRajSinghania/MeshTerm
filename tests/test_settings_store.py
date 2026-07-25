"""Tests for remembering device settings across sessions (``core.settings_store``).

Covers the durable per-device store, the drift detection that compares remembered values to a
device's live snapshot, and the ``restore``/``adopt`` reconcile actions the startup offer drives.
The reconcile tests run against the :class:`MockDevice` simulator, whose configuration resets to
firmware defaults each construction — standing in for the firmware-less radio bridge that forgets
its settings on restart.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import make_device
from meshterm.core.device_config import build_snapshot
from meshterm.core.device_store import DeviceStore
from meshterm.core.settings_store import (
    SettingsStore,
    adopt,
    restore,
    settings_drift,
)
from meshterm.persistence.repository import Repository
from meshterm.tools.config import apply_ops

PUB_A = "aa" * 32
PUB_B = "bb" * 32


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context with the plain (console) UI surface."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "cfg.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


# --- the store ---------------------------------------------------------------


def test_store_round_trips_and_persists(tmp_path: Path) -> None:
    """Remembered settings survive a fresh store instance, kept per device."""
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.remember(PUB_A, "name", "Ops-Node")
    store.remember(PUB_A, "radio_freq", 915.0)
    store.remember(PUB_B, "tx_power", 20)

    reloaded = SettingsStore(path)  # a new process reads the same file
    assert reloaded.settings(PUB_A) == {"name": "Ops-Node", "radio_freq": 915.0}
    assert reloaded.settings(PUB_B) == {"tx_power": 20}  # a second device keeps its own set


def test_store_replace_forget_and_forget_all(tmp_path: Path) -> None:
    """Re-remembering a key replaces it; forget drops one; forget_all clears the device."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(PUB_A, "name", "First")
    store.remember(PUB_A, "name", "Second")  # same key, new value
    store.remember(PUB_A, "tx_power", 20)
    assert store.settings(PUB_A) == {"name": "Second", "tx_power": 20}

    store.forget(PUB_A, "name")
    assert store.settings(PUB_A) == {"tx_power": 20}

    store.forget_all(PUB_A)
    assert store.settings(PUB_A) == {}
    # The device drops out of the persisted file entirely once it has nothing remembered.
    assert json.loads((tmp_path / "settings.json").read_text())["devices"] == {}


def test_store_normalises_device_key(tmp_path: Path) -> None:
    """A device key is matched case-insensitively and with an optional 0x prefix stripped."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember("AABB", "name", "Ops")
    assert store.settings("0xaabb") == {"name": "Ops"}


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    """A garbage file reads as empty rather than raising, and stays writable."""
    path = tmp_path / "settings.json"
    path.write_text("not json at all", encoding="utf-8")
    store = SettingsStore(path)
    assert store.settings(PUB_A) == {}
    store.remember(PUB_A, "name", "Ops")  # recovers and persists
    assert SettingsStore(path).settings(PUB_A) == {"name": "Ops"}


def test_store_drops_malformed_values(tmp_path: Path) -> None:
    """Non-scalar values (a container, a null) are skipped; scalar ones are kept."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "devices": {
                    PUB_A: {"name": "Good", "bad_list": [1, 2], "bad_null": None, "tx_power": 20}
                }
            }
        ),
        encoding="utf-8",
    )
    assert SettingsStore(path).settings(PUB_A) == {"name": "Good", "tx_power": 20}


def test_store_ignores_non_scalar_remember(tmp_path: Path) -> None:
    """Remembering a non-scalar value is a no-op — only strings, numbers, and bools persist."""
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(PUB_A, "name", ["not", "a", "scalar"])
    assert store.settings(PUB_A) == {}


# --- drift -------------------------------------------------------------------


async def _mock_device():
    device = make_device(mock=True, port=None)
    await device.connect()
    pubkey = (await device.get_self_info())["public_key"]
    return device, pubkey


async def test_drift_reports_only_changed_settings(tmp_path: Path) -> None:
    """Drift lists remembered settings that differ from the device, in registry order."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "radio_freq", 915.0)  # device default is 869.525 — drifts
    store.remember(pubkey, "name", "MockCompanion")  # equals the device default — no drift

    snapshot = await build_snapshot(device)
    drifted = settings_drift(store, pubkey, snapshot)
    assert [d.key for d in drifted] == ["radio_freq"]
    assert drifted[0].remembered == 915.0 and drifted[0].current == 869.525


async def test_drift_empty_when_nothing_remembered(tmp_path: Path) -> None:
    """A device with no remembered settings never shows drift (firmware radios pay nothing)."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    snapshot = await build_snapshot(device)
    assert settings_drift(store, pubkey, snapshot) == []


async def test_drift_skips_unknown_keys(tmp_path: Path) -> None:
    """A remembered key the registry no longer defines is ignored, not reported as drift."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "gone_from_registry", "whatever")
    snapshot = await build_snapshot(device)
    assert settings_drift(store, pubkey, snapshot) == []


# --- restore / adopt ---------------------------------------------------------


async def test_restore_writes_remembered_values_including_coupled(tmp_path: Path) -> None:
    """Restore replays saved values onto the device, coupled radio fields rebuilt together."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "name", "Ops-Node")
    store.remember(pubkey, "radio_freq", 915.0)
    store.remember(pubkey, "radio_sf", 12)  # a second radio field: exercises the coupled apply

    snapshot = await build_snapshot(device)
    drifted = settings_drift(store, pubkey, snapshot)
    written = await restore(store, device, snapshot, [d.key for d in drifted])
    assert written == 3

    after = await build_snapshot(device)
    assert after["name"] == "Ops-Node"
    assert after["radio_freq"] == 915.0
    assert after["radio_sf"] == 12
    assert after["radio_bw"] == 250.0  # untouched radio field preserved by the coupled rebuild
    # Idempotent: with the device now matching, there is nothing left to restore.
    assert settings_drift(store, pubkey, after) == []


async def test_adopt_updates_store_to_device_values(tmp_path: Path) -> None:
    """Adopt takes the device's current values as the new saved truth, clearing the drift."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "radio_freq", 915.0)  # differs from the device's 869.525

    snapshot = await build_snapshot(device)
    drifted = settings_drift(store, pubkey, snapshot)
    adopt(store, pubkey, snapshot, [d.key for d in drifted])

    assert store.settings(pubkey)["radio_freq"] == 869.525  # now matches the device
    assert settings_drift(store, pubkey, snapshot) == []


async def test_adopt_forgets_a_setting_the_device_no_longer_reports(tmp_path: Path) -> None:
    """Adopting a key the device reports no value for forgets it rather than storing a null."""
    device, pubkey = await _mock_device()
    store = SettingsStore(tmp_path / "settings.json")
    store.remember(pubkey, "flood_scope", "#ops")

    snapshot = await build_snapshot(device)
    snapshot.pop("flood_scope", None)  # simulate firmware that doesn't report this field
    adopt(store, pubkey, snapshot, ["flood_scope"])
    assert "flood_scope" not in store.settings(pubkey)


# --- write-through -----------------------------------------------------------


async def test_apply_setting_remembers_through_the_config_executor(ctx) -> None:
    """Every setting changed through the config executor is recorded under the device key."""
    device = await ctx.device()
    pubkey = (await device.get_self_info())["public_key"]
    snapshot = await build_snapshot(device)

    changes, _ = await apply_ops(
        ctx, device, snapshot, [("set", "name", "Ops-Node"), ("set", "tx_power", "18")]
    )
    assert changes == 2
    assert ctx.settings_store.settings(pubkey) == {"name": "Ops-Node", "tx_power": 18}
