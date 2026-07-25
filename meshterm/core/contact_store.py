"""Persistence for contacts heard through MeshTerm, so they survive a device that forgets them.

Most companions keep their contact table in firmware, so MeshTerm reads contacts live from the
device and never has to remember them. A firmware-less radio bridge is the exception: it holds
its contact table only in RAM, so every contact is gone when the bridge process restarts — the
Contacts screen and the chat recipient list come up empty (or only as sparse as the adverts
heard so far this session), even though you were messaging those nodes last time.

This store is the durable backup for exactly that case. Every time MeshTerm reads the device's
contacts it records them here, keyed by the device's own public key so two radios keep separate
sets. When a list is then drawn, :func:`merge_contacts` unions the device's live contacts with
any remembered contact the device isn't currently reporting — so a forgetful device still shows
the nodes you know, and they stay messageable: a direct message addresses a node by a prefix of
its public key, which the remembered contact carries, so nothing has to be written back onto the
device to reach them.

The union is inert where it isn't needed: a firmware radio always reports its whole table, so
every remembered contact is already present and nothing is added — no device-type check required.
Recording never *removes* a contact on a device's empty read, so a bridge that just restarted
doesn't wipe the memory of what it knew.

Like the other operator state (mutes, remembered channels, saved settings), this is global
machine state in a small JSON file (``<config_dir>/contacts.json``), not the per-invocation
SQLite database. Reads are served from memory after the first load; a write happens only when the
contact set actually changes, and flushes atomically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Contact


def _norm(pubkey: str) -> str:
    """Normalise a public key (device or contact) to the lowercase hex used as a key."""
    return (pubkey or "").lower().removeprefix("0x")


def _opt_int(value: object) -> Optional[int]:
    """Coerce a JSON value to ``int``, or ``None`` if absent/unparseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: object) -> Optional[float]:
    """Coerce a JSON value to ``float``, or ``None`` if absent/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RememberedContact:
    """One contact MeshTerm remembers for a device: enough to list it and address a message.

    Attributes:
        public_key: The contact's full public key (lowercase hex) — how a direct message
            addresses it, so it is what makes a remembered contact messageable.
        name: The node's advertised name.
        node_type: The advert type (see the ``NODE_TYPE_*`` constants), so the chat picker's
            direct-messageable filter keeps behaving as it would for a live contact.
        last_advert: Unix seconds of the node's most recent advert when last heard, for the
            list's last-heard column; ``None`` when unknown.
        lat: Last advertised latitude, if it shared one.
        lon: Last advertised longitude, if it shared one.
    """

    public_key: str
    name: str
    node_type: Optional[int] = None
    last_advert: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

    @classmethod
    def from_contact(cls, contact: Contact) -> "RememberedContact":
        """Distil a live :class:`~meshterm.core.models.Contact` into the fields we persist."""
        epoch = int(contact.last_seen.timestamp()) if contact.last_seen else None
        return cls(
            public_key=_norm(contact.public_key),
            name=contact.name,
            node_type=contact.node_type,
            last_advert=epoch,
            lat=contact.lat,
            lon=contact.lon,
        )

    def to_contact(self) -> Contact:
        """Rebuild a :class:`~meshterm.core.models.Contact` for the merged list.

        The learned route is dropped (``route_hops=None``): a remembered contact floods until
        the device relearns a path from received traffic, exactly as a freshly-heard one does.
        """
        last_seen = (
            datetime.fromtimestamp(self.last_advert, tz=timezone.utc)
            if self.last_advert
            else None
        )
        return Contact(
            name=self.name,
            public_key=self.public_key,
            key_prefix=self.public_key[:12],
            last_seen=last_seen,
            node_type=self.node_type,
            lat=self.lat,
            lon=self.lon,
            route_hops=None,
        )


class ContactStore:
    """Reads and writes the per-device set of remembered contacts, memory-first.

    Interact through :meth:`contacts` (a device's remembered contacts) and :meth:`remember_all`
    (record the contacts just read from a device — an upsert that never drops one on absence).
    The backing map is loaded once on first access and kept in memory; a mutation persists the
    whole map atomically, and only when something actually changed.
    """

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on the first remembered contact).
        """
        self._path = path
        self._devices: Optional[dict[str, dict[str, RememberedContact]]] = None

    @property
    def _state(self) -> dict[str, dict[str, RememberedContact]]:
        """The device -> {contact key -> remembered contact} map, loaded on first access."""
        if self._devices is None:
            self._devices = self._load()
        return self._devices

    def _load(self) -> dict[str, dict[str, RememberedContact]]:
        """Parse the file into the device map, or empty on a missing/corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        devices: dict[str, dict[str, RememberedContact]] = {}
        for pubkey, entries in (data.get("devices") or {}).items():
            if not isinstance(entries, list):
                continue
            contacts: dict[str, RememberedContact] = {}
            for entry in entries:
                contact = _contact_from_json(entry)
                if contact is not None:
                    contacts[contact.public_key] = contact
            if contacts:
                devices[_norm(str(pubkey))] = contacts
        return devices

    def contacts(self, device_pubkey: str) -> list[RememberedContact]:
        """The contacts remembered for a device (a copy, sorted by name), empty if none."""
        remembered = self._state.get(_norm(device_pubkey), {})
        return sorted(remembered.values(), key=lambda c: (c.name.lower(), c.public_key))

    def remember_all(self, device_pubkey: str, contacts: list[Contact]) -> None:
        """Record the contacts just read from a device, upserting by public key.

        A contact new to the device, or one whose fields changed (a rename, a fresher advert),
        is stored; an unchanged one is left alone. A contact the device is *no longer* reporting
        is **kept** — so a forgetful device's empty read never erases what it once knew. Persists
        once, only if anything changed.

        Args:
            device_pubkey: The device's own public key.
            contacts: The contacts just read from that device.
        """
        dev = _norm(device_pubkey)
        if not dev:
            return
        current = dict(self._state.get(dev, {}))
        changed = False
        for contact in contacts:
            if not contact.public_key:
                continue  # unaddressable — nothing to remember it by
            remembered = RememberedContact.from_contact(contact)
            if current.get(remembered.public_key) != remembered:
                current[remembered.public_key] = remembered
                changed = True
        if changed:
            self._state[dev] = current
            self._save()

    def forget(self, device_pubkey: str, contact_pubkey: str) -> None:
        """Drop one remembered contact; persist only a real change."""
        dev = _norm(device_pubkey)
        contacts = self._state.get(dev)
        key = _norm(contact_pubkey)
        if not contacts or key not in contacts:
            return
        contacts = {k: v for k, v in contacts.items() if k != key}
        if contacts:
            self._state[dev] = contacts
        else:
            del self._state[dev]
        self._save()

    def _save(self) -> None:
        """Persist the whole device map atomically (a crash mid-write keeps the old file)."""
        data = {
            "devices": {
                pubkey: [_contact_to_json(c) for c in sorted(
                    contacts.values(), key=lambda c: (c.name.lower(), c.public_key)
                )]
                for pubkey, contacts in sorted(self._state.items())
                if contacts
            }
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)


def _contact_to_json(contact: RememberedContact) -> dict:
    """Serialise one remembered contact, omitting the fields it doesn't carry."""
    entry: dict = {"public_key": contact.public_key, "name": contact.name}
    if contact.node_type is not None:
        entry["node_type"] = contact.node_type
    if contact.last_advert is not None:
        entry["last_advert"] = contact.last_advert
    if contact.lat is not None:
        entry["lat"] = contact.lat
    if contact.lon is not None:
        entry["lon"] = contact.lon
    return entry


def _contact_from_json(entry: object) -> Optional[RememberedContact]:
    """Parse one stored contact entry, or ``None`` if it is malformed (no key or name)."""
    if not isinstance(entry, dict):
        return None
    pubkey = _norm(str(entry.get("public_key", "")))
    name = entry.get("name")
    if not pubkey or not isinstance(name, str):
        return None
    return RememberedContact(
        public_key=pubkey,
        name=name,
        node_type=_opt_int(entry.get("node_type")),
        last_advert=_opt_int(entry.get("last_advert")),
        lat=_opt_float(entry.get("lat")),
        lon=_opt_float(entry.get("lon")),
    )


def merge_contacts(
    store: ContactStore, device_pubkey: str, live: list[Contact]
) -> list[Contact]:
    """Union a device's live contacts with any remembered ones it isn't currently reporting.

    Live contacts pass through unchanged and first; a remembered contact whose key the device
    already reports is left to the live entry (the fresher of the two), so nothing is
    duplicated. A firmware radio reports its whole table, so this adds nothing there; a
    forgetful device gets the missing contacts back, rebuilt from what was remembered.

    Args:
        store: The contact store to read remembered contacts from.
        device_pubkey: The device's own public key.
        live: The contacts the device is currently reporting.

    Returns:
        The live contacts followed by the remembered contacts the device is missing.
    """
    present = {_norm(c.public_key) for c in live if c.public_key}
    extra = [
        remembered.to_contact()
        for remembered in store.contacts(device_pubkey)
        if remembered.public_key and remembered.public_key not in present
    ]
    return list(live) + extra
