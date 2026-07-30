"""Tests for remembering contacts across sessions (``core.contact_store``).

Covers the durable per-device store, the ``RememberedContact`` <-> ``Contact`` round-trip, the
``merge_contacts`` union that surfaces remembered contacts a device is no longer reporting, and
the write-through/merge wired into :class:`~meshterm.services.device_state.DeviceState`. The
integration test runs against the :class:`MockDevice` simulator, emptying its contact table to
stand in for the firmware-less radio bridge that loses its contacts on restart.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.contact_store import ContactStore, RememberedContact, merge_contacts
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_REPEATER,
    Contact,
    is_direct_messageable,
    utcnow,
)
from meshterm.persistence.repository import Repository

PUB_A = "aa" * 32
PUB_B = "bb" * 32


def _contact(name: str, key_byte: str, **kw) -> Contact:
    """A live Contact addressed by a full 32-byte key built from a repeated hex byte."""
    pub = key_byte * 32
    return Contact(name=name, public_key=pub, key_prefix=pub[:12], **kw)


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context with the plain (console) UI surface."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "contacts.db")
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
    """Remembered contacts survive a fresh store instance, kept per device, sorted by name."""
    path = tmp_path / "contacts.json"
    store = ContactStore(path)
    store.remember_all(
        PUB_A, [_contact("Bob", "cc", node_type=NODE_TYPE_CHAT), _contact("Al", "dd")]
    )
    store.remember_all(PUB_B, [_contact("Carol", "ee")])

    reloaded = ContactStore(path)  # a new process reads the same file
    a = reloaded.contacts(PUB_A)
    assert [c.name for c in a] == ["Al", "Bob"]  # name order
    assert a[1].public_key == "cc" * 32 and a[1].node_type == NODE_TYPE_CHAT
    assert [c.name for c in reloaded.contacts(PUB_B)] == ["Carol"]  # separate device


def test_remember_all_upserts_and_never_drops_absent(tmp_path: Path) -> None:
    """A rename updates in place; a contact absent from a later read is kept, not removed."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [_contact("Alice", "cc"), _contact("Bob", "dd")])
    # A later read the device only reports Alice under a new name — Bob must survive.
    store.remember_all(PUB_A, [_contact("Alice-2", "cc")])

    by_key = {c.public_key: c.name for c in store.contacts(PUB_A)}
    assert by_key == {"cc" * 32: "Alice-2", "dd" * 32: "Bob"}


def test_remember_all_skips_keyless_contacts(tmp_path: Path) -> None:
    """A contact with no public key is unaddressable and is not remembered."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [Contact(name="Ghost", public_key="")])
    assert store.contacts(PUB_A) == []


def test_store_normalises_keys(tmp_path: Path) -> None:
    """Both the device key and the contact key are matched case-insensitively, 0x stripped."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all("AABB", [Contact(name="Al", public_key="0xCCDD")])
    remembered = store.contacts("0xaabb")
    assert remembered and remembered[0].public_key == "ccdd"


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    """A garbage file reads as empty rather than raising, and stays writable."""
    path = tmp_path / "contacts.json"
    path.write_text("not json at all", encoding="utf-8")
    store = ContactStore(path)
    assert store.contacts(PUB_A) == []
    store.remember_all(PUB_A, [_contact("Al", "cc")])  # recovers and persists
    assert [c.name for c in ContactStore(path).contacts(PUB_A)] == ["Al"]


def test_store_drops_malformed_entries(tmp_path: Path) -> None:
    """Entries missing a public key or name are skipped; good ones kept."""
    path = tmp_path / "contacts.json"
    path.write_text(
        json.dumps(
            {
                "devices": {
                    PUB_A: [
                        {"public_key": "cc" * 32, "name": "Good"},
                        {"name": "Keyless"},  # no public_key
                        {"public_key": "dd" * 32},  # no name
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert [c.name for c in ContactStore(path).contacts(PUB_A)] == ["Good"]


def test_forget_drops_one_contact(tmp_path: Path) -> None:
    """Forgetting removes a single remembered contact and persists the change."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [_contact("Al", "cc"), _contact("Bob", "dd")])
    store.forget(PUB_A, "cc" * 32)
    assert [c.name for c in store.contacts(PUB_A)] == ["Bob"]


# --- RememberedContact <-> Contact -------------------------------------------


def test_remembered_contact_round_trip() -> None:
    """A live contact distils to remembered fields and rebuilds with them intact."""
    heard = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    live = _contact("Alice", "cc", node_type=NODE_TYPE_CHAT, last_seen=heard, lat=45.5, lon=-73.6)
    remembered = RememberedContact.from_contact(live)
    assert remembered.public_key == "cc" * 32 and remembered.last_advert == int(heard.timestamp())

    rebuilt = remembered.to_contact()
    assert rebuilt.name == "Alice"
    assert rebuilt.public_key == "cc" * 32 and rebuilt.key_prefix == "cc" * 6
    assert rebuilt.last_seen == heard
    assert rebuilt.node_type == NODE_TYPE_CHAT and rebuilt.lat == 45.5 and rebuilt.lon == -73.6
    assert rebuilt.route_hops is None  # a remembered contact floods until a path is relearned


def test_remembered_future_advert_rebuilds_as_never_heard() -> None:
    """A future-stamped epoch remembered before this guard existed rebuilds as unknown.

    The stored epoch re-enters through ``models.advert_time``, so a poisoned value written
    by an old session can't resurface a contact as "heard in the future" on a bridge.
    """
    future = int(utcnow().timestamp()) + 7 * 86400
    entry = RememberedContact(public_key="cc" * 32, name="Bogus-Clock", last_advert=future)
    assert entry.to_contact().last_seen is None


# --- merge -------------------------------------------------------------------


def test_merge_unions_remembered_the_device_forgot(tmp_path: Path) -> None:
    """Merge appends remembered contacts the device forgot, without duplicating present ones."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [_contact("Al", "cc"), _contact("Bob", "dd"), _contact("Cy", "ee")])

    live = [_contact("Al", "cc")]  # the device only currently reports Al
    merged = merge_contacts(store, PUB_A, live)
    names = [c.name for c in merged]
    assert names[0] == "Al"  # live first, untouched
    assert set(names) == {"Al", "Bob", "Cy"}  # the two forgotten ones are unioned back in
    assert names.count("Al") == 1  # present contact not duplicated


def test_merge_adds_nothing_when_device_reports_all(tmp_path: Path) -> None:
    """A device reporting its whole table (a firmware radio) gets no additions."""
    store = ContactStore(tmp_path / "contacts.json")
    live = [_contact("Al", "cc"), _contact("Bob", "dd")]
    store.remember_all(PUB_A, live)
    assert len(merge_contacts(store, PUB_A, live)) == 2


# --- DeviceState integration -------------------------------------------------


async def test_devstate_remembers_and_restores_forgotten_contacts(ctx) -> None:
    """Reading contacts remembers them; a device that then forgets its table still lists them."""
    device = await ctx.device()
    original = await ctx.devstate.contacts(force=True)  # first read remembers the mock's table
    original_keys = {c.public_key for c in original if c.public_key}
    assert original_keys  # the simulator ships a non-empty contact table

    # The bridge "restarts": its RAM contact table is wiped. A fresh read finds nothing live...
    device._contacts = []
    restored = await ctx.devstate.contacts(force=True)
    restored_keys = {c.public_key for c in restored}

    # ...yet every contact we'd remembered is merged back in and stays messageable.
    assert original_keys <= restored_keys
    messageable = {c.public_key for c in original if is_direct_messageable(c.node_type)}
    still = {c.public_key for c in restored if is_direct_messageable(c.node_type)}
    assert messageable <= still


async def test_devstate_merge_keeps_a_repeater_a_repeater(ctx) -> None:
    """A remembered repeater rebuilds with its type, so the DM filter still excludes it."""
    device = await ctx.device()
    device._contacts = [
        Contact(name="Big-Repeater", public_key="ab" * 32, key_prefix="ab" * 6,
                node_type=NODE_TYPE_REPEATER),
    ]
    await ctx.devstate.contacts(force=True)  # remember it
    device._contacts = []  # forget it
    restored = await ctx.devstate.contacts(force=True)
    repeater = next(c for c in restored if c.public_key == "ab" * 32)
    assert repeater.node_type == NODE_TYPE_REPEATER
    assert not is_direct_messageable(repeater.node_type)
