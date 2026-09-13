# SPDX-License-Identifier: Apache-2.0
"""Tests for remembering channels across sessions (``core.channel_store``).

Covers the durable store itself (per-device, memory-first JSON), the ``reconcile`` replay that
restores a forgetful device's channels on connect, and the write-through that records every
channel MeshTerm creates. The reconcile tests run against the :class:`MockDevice` simulator,
whose channel table starts empty — standing in for the firmware-less radio bridge.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.channel_probe import read_channel_slots
from meshterm.core.channel_store import ChannelStore, reconcile
from meshterm.core.channels import DEFAULT_PUBLIC_SECRET, channel_identity, derive_secret
from meshterm.core.config import Settings
from meshterm.core.connection import make_device
from meshterm.core.device_store import DeviceStore
from meshterm.persistence.repository import Repository
from meshterm.ui.channels import write_channel

PUB_A = "aa" * 32
PUB_B = "bb" * 32


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context with the plain (console) UI surface."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "chan.db")
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
    """Remembered channels survive a fresh store instance, sorted by slot, keyed per device."""
    path = tmp_path / "channels.json"
    store = ChannelStore(path)
    store.remember(PUB_A, 1, "Ops", bytes(range(16)))
    store.remember(PUB_A, 0, "Public", DEFAULT_PUBLIC_SECRET)
    store.remember(PUB_B, 0, "Other", bytes(range(16, 32)))

    reloaded = ChannelStore(path)  # a new process reads the same file
    a = reloaded.channels(PUB_A)
    assert [c.idx for c in a] == [0, 1]  # slot order
    assert a[0].name == "Public" and a[0].secret == DEFAULT_PUBLIC_SECRET
    assert a[1].name == "Ops" and a[1].secret == bytes(range(16))
    # A second device keeps a separate set.
    assert [c.name for c in reloaded.channels(PUB_B)] == ["Other"]


def test_store_forget_and_replace(tmp_path: Path) -> None:
    """Forgetting drops one slot; re-remembering a slot replaces its occupant."""
    store = ChannelStore(tmp_path / "channels.json")
    store.remember(PUB_A, 0, "First", bytes(range(16)))
    store.remember(PUB_A, 0, "Second", bytes(range(16, 32)))  # same slot, new channel
    assert [c.name for c in store.channels(PUB_A)] == ["Second"]

    store.remember(PUB_A, 1, "Keep", bytes(range(16)))
    store.forget(PUB_A, 0)
    assert [c.idx for c in store.channels(PUB_A)] == [1]


def test_store_normalises_device_key(tmp_path: Path) -> None:
    """A device key is matched case-insensitively and with an optional 0x prefix stripped."""
    store = ChannelStore(tmp_path / "channels.json")
    store.remember("AABB", 0, "Ops", bytes(range(16)))
    assert store.channels("0xaabb")  # same device, different spelling


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    """A garbage or malformed file reads as empty rather than raising, and stays writable."""
    path = tmp_path / "channels.json"
    path.write_text("not json at all", encoding="utf-8")
    store = ChannelStore(path)
    assert store.channels(PUB_A) == []
    store.remember(PUB_A, 0, "Ops", bytes(range(16)))  # recovers and persists
    assert ChannelStore(path).channels(PUB_A)[0].name == "Ops"


def test_store_drops_malformed_entries(tmp_path: Path) -> None:
    """Entries with a bad secret length or missing fields are skipped, good ones kept."""
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps(
            {
                "devices": {
                    PUB_A: [
                        {"idx": 0, "name": "Good", "secret": bytes(range(16)).hex()},
                        {"idx": 1, "name": "BadKey", "secret": "00"},  # 1-byte secret
                        {"idx": 2, "name": "Nameless"},  # missing secret
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert [c.name for c in ChannelStore(path).channels(PUB_A)] == ["Good"]


# --- reconcile ---------------------------------------------------------------


async def _mock_device():
    device = make_device(mock=True, port=None)
    await device.connect()
    pubkey = (await device.get_self_info())["public_key"]
    return device, pubkey


async def test_reconcile_restores_missing_channels(tmp_path: Path) -> None:
    """A device that forgot its channels gets every remembered one replayed into its slot."""
    device, pubkey = await _mock_device()
    store = ChannelStore(tmp_path / "channels.json")
    store.remember(pubkey, 0, "Public", DEFAULT_PUBLIC_SECRET)
    store.remember(pubkey, 1, "Ops", bytes(range(16)))

    restored = await reconcile(store, device)
    assert restored == 2

    after = {s.idx: s for s in await read_channel_slots(device)}
    assert after[0].name == "Public" and after[0].secret == DEFAULT_PUBLIC_SECRET
    assert after[1].name == "Ops" and after[1].secret == bytes(range(16))

    # Idempotent: a second reconcile sees the channels already present (by identity) and writes
    # nothing more.
    assert await reconcile(store, device) == 0


async def test_reconcile_noop_when_nothing_remembered(tmp_path: Path) -> None:
    """With an empty record the device is never written to (a firmware radio pays nothing)."""
    device, _ = await _mock_device()
    store = ChannelStore(tmp_path / "channels.json")
    assert await reconcile(store, device) == 0
    assert await read_channel_slots(device) == []


async def test_reconcile_never_overwrites_an_occupied_slot(tmp_path: Path) -> None:
    """A remembered channel whose slot is taken lands on a free slot, leaving the occupant."""
    device, pubkey = await _mock_device()
    await device.set_channel(0, "Squatter", bytes(range(16, 32)))  # already on slot 0
    store = ChannelStore(tmp_path / "channels.json")
    store.remember(pubkey, 0, "Ops", bytes(range(16)))  # remembered for slot 0 — now taken

    restored = await reconcile(store, device)
    assert restored == 1

    after = {s.idx: s for s in await read_channel_slots(device)}
    assert after[0].name == "Squatter"  # untouched
    ops = next(s for s in after.values() if s.name == "Ops")
    assert ops.idx != 0  # relocated to a free slot


async def test_reconcile_leaves_a_channel_present_at_another_slot(tmp_path: Path) -> None:
    """A remembered channel already on the device (at any slot) is not duplicated."""
    device, pubkey = await _mock_device()
    await device.set_channel(3, "Ops", bytes(range(16)))  # present, but at slot 3 not 0
    store = ChannelStore(tmp_path / "channels.json")
    store.remember(pubkey, 0, "Ops", bytes(range(16)))  # remembered at slot 0

    assert await reconcile(store, device) == 0  # matched by identity — nothing to restore
    assert len([s for s in await read_channel_slots(device) if s.name == "Ops"]) == 1


# --- write-through -----------------------------------------------------------


async def test_write_channel_remembers_and_forgets(ctx) -> None:
    """Writing a channel records it under the device key; clearing the slot forgets it."""
    device = await ctx.device()
    pubkey = (await device.get_self_info())["public_key"]

    await write_channel(ctx, device, 2, "Ops", bytes(range(16)))
    remembered = ctx.channel_store.channels(pubkey)
    assert [(c.idx, c.name) for c in remembered] == [(2, "Ops")]

    await write_channel(ctx, device, 2, "", None)  # clear the slot
    assert ctx.channel_store.channels(pubkey) == []


async def test_write_channel_stores_derived_key_for_name_derived(ctx) -> None:
    """A name-derived (#) channel remembers the key the firmware derives, ready to replay."""
    device = await ctx.device()
    pubkey = (await device.get_self_info())["public_key"]

    await write_channel(ctx, device, 1, "#general", None)  # None => firmware derives from name
    stored = ctx.channel_store.channels(pubkey)[0]
    assert stored.secret == derive_secret("#general")
    assert stored.identity == channel_identity("#general", derive_secret("#general"))
