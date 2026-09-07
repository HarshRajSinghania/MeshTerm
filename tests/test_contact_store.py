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
        Contact(
            name="Big-Repeater",
            public_key="ab" * 32,
            key_prefix="ab" * 6,
            node_type=NODE_TYPE_REPEATER,
        ),
    ]
    await ctx.devstate.contacts(force=True)  # remember it
    device._contacts = []  # forget it
    restored = await ctx.devstate.contacts(force=True)
    repeater = next(c for c in restored if c.public_key == "ab" * 32)
    assert repeater.node_type == NODE_TYPE_REPEATER
    assert not is_direct_messageable(repeater.node_type)


def test_an_archived_contact_is_remembered_but_kept_out_of_the_merge(tmp_path) -> None:  # noqa: ANN001
    """Archiving frees a device slot without losing the contact — and without undoing itself.

    The sweep's whole shape (see :mod:`meshterm.ui.purge_screen`): the contact is deleted
    from the *device*, which is the scarce resource, and kept here in full. The subtlety is
    the merge — this store's normal job is to union remembered contacts back into any list
    the device isn't reporting, which would put an archived contact straight back and make
    the sweep look like it did nothing.
    """
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    alice = Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6)
    bob = Contact(name="Bob", public_key="bb" * 32, key_prefix="bb" * 6)
    store.remember_all(PUB_A, [alice, bob])

    store.archive(PUB_A, alice, when=1_700_000_000)

    # Remembered in full — the public key that makes a restore possible is still here.
    archived = store.archived(PUB_A)
    assert [c.name for c in archived] == ["Alice"]
    assert archived[0].public_key == "aa" * 32
    assert archived[0].archived_at == 1_700_000_000

    # …but withheld from the union, so the device's slot really is free.
    merged = merge_contacts(store, PUB_A, [])
    assert [c.name for c in merged] == ["Bob"]

    # Survives a reload: the mark is persisted, not just held in memory.
    reloaded = ContactStore(tmp_path / "contacts.json")
    assert [c.name for c in reloaded.archived(PUB_A)] == ["Alice"]
    assert [c.name for c in merge_contacts(reloaded, PUB_A, [])] == ["Bob"]


def test_restoring_clears_the_mark_and_the_contact_merges_again(tmp_path) -> None:  # noqa: ANN001
    """A restore is the exact inverse — the contact rejoins every list it was withheld from."""
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    alice = Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6)
    store.archive(PUB_A, alice, when=1_700_000_000)
    assert merge_contacts(store, PUB_A, []) == []

    was = store.restore(PUB_A, "aa" * 32)
    assert was is not None and was.name == "Alice"
    assert store.archived(PUB_A) == []
    assert [c.name for c in merge_contacts(store, PUB_A, [])] == ["Alice"]

    # Restoring one that isn't archived is a no-op, not an error.
    assert store.restore(PUB_A, "aa" * 32) is None
    assert store.restore(PUB_A, "ff" * 32) is None


def test_archiving_a_contact_the_store_never_saw_still_remembers_it(tmp_path) -> None:  # noqa: ANN001
    """The usual case: a firmware radio's contact is live-only until the sweep takes it.

    Archiving upserts first, so the record that makes a restore possible exists precisely
    because the sweep created it — there is no earlier read to depend on.
    """
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    unseen = Contact(name="Carol", public_key="cc" * 32, key_prefix="cc" * 6, lat=45.5, lon=-73.6)
    store.archive(PUB_A, unseen, when=1_700_000_000)

    archived = store.archived(PUB_A)
    assert [c.name for c in archived] == ["Carol"]
    assert (archived[0].lat, archived[0].lon) == (45.5, -73.6)


def test_a_keyless_contact_cannot_be_archived(tmp_path) -> None:  # noqa: ANN001
    """Nothing to restore it by, so archiving it would be a silent deletion."""
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    store.archive(PUB_A, Contact(name="Ghost"), when=1_700_000_000)
    assert store.archived(PUB_A) == []
