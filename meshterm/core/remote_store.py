# SPDX-License-Identifier: Apache-2.0
"""Persistence for remote-node admin state: last-read settings and CLI history.

Reading a repeater's configuration costs one paced mesh round trip per value, so the
repeater-admin screen never bulk-reads on open — it shows what the *last* read (or the
last applied ``set``) said, stamped with when, and refreshes on demand. That cache lives
here, per node (keyed like the admin passwords, by public key), in a small JSON file in
the config directory (``<config_dir>/remote.json``) alongside each node's remote
command-line history — global machine state, like the other JSON stores, deliberately
outside the per-invocation SQLite database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .admin_store import admin_key
from .atomicwrite import write_atomically
from .models import Contact, utcnow

#: How many command-line entries are kept per node (newest last).
HISTORY_CAP = 100


@dataclass(frozen=True, slots=True)
class CachedValue:
    """One remembered remote setting: what the node last said, and when.

    Attributes:
        value: The value as stored text (empty when unsupported).
        read_at: When it was read from (or written to) the node.
        supported: ``False`` when the node answered the read with an error — its firmware
            has no such setting (a board without a front-end module, a build without a
            bridge). Kept apart from *never read*, which is no entry at all.
        discovered: ``True`` for a setting the catalog hasn't got, learned from a command
            the reader ran on *this node's* command line. Such a row reads ``n/a`` like any
            other when the node stops answering it, and is removed only by hand: its key is
            recorded nowhere else, so a misread reply must not be able to delete it.
    """

    value: str
    read_at: datetime | None
    supported: bool = True
    discovered: bool = False


class RemoteStore:
    """Reads and writes per-node remote-admin state (setting cache + CLI history)."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    def _load_all(self) -> dict[str, dict]:
        """Return the raw key -> record mapping, or empty on missing/corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # -- the settings cache -------------------------------------------------------

    def settings(self, node: Contact) -> dict[str, CachedValue]:
        """Every remembered setting for ``node``, keyed by CLI parameter name."""
        record = self._load_all().get(admin_key(node))
        raw = record.get("settings") if isinstance(record, dict) else None
        out: dict[str, CachedValue] = {}
        if not isinstance(raw, dict):
            return out
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            supported = not entry.get("unsupported", False)
            if supported and "value" not in entry:
                continue
            read_at: datetime | None = None
            stamp = entry.get("read_at")
            if isinstance(stamp, str):
                try:
                    read_at = datetime.fromisoformat(stamp)
                except ValueError:
                    read_at = None
            out[key] = CachedValue(
                value=str(entry.get("value", "")),
                read_at=read_at,
                supported=supported,
                discovered=bool(entry.get("discovered", False)),
            )
        return out

    def remember_setting(self, node: Contact, key: str, value: str) -> None:
        """Cache one setting's value for ``node``, stamped now."""
        self._put(node, key, {"value": value, "read_at": utcnow().isoformat()})

    def remember_discovered(self, node: Contact, key: str, value: str) -> None:
        """Cache a setting the catalog hasn't got, which ``node`` has just proved it has."""
        self._put(node, key, {"value": value, "read_at": utcnow().isoformat(), "discovered": True})

    def remember_unsupported(self, node: Contact, key: str) -> None:
        """Record that ``node`` answered a read of ``key`` with an error, stamped now."""
        self._put(node, key, {"unsupported": True, "read_at": utcnow().isoformat()})

    def forget_setting(self, node: Contact, key: str) -> None:
        """Drop one cached setting, so the row reads as never read."""
        records = self._load_all()
        settings = records.get(admin_key(node), {}).get("settings")
        if isinstance(settings, dict) and settings.pop(key, None) is not None:
            self._write(records)

    def _put(self, node: Contact, key: str, entry: dict) -> None:
        """Store one setting's cache entry for ``node``, keeping it discovered if it was.

        A discovered row is refreshed by the same reads and writes as any other — through
        :meth:`remember_setting` — and that must not quietly demote it to a catalog row it
        has no entry for, which would leave a row nothing draws.
        """
        records = self._load_all()
        record = records.setdefault(admin_key(node), {})
        settings = record.setdefault("settings", {})
        previous = settings.get(key)
        if isinstance(previous, dict) and previous.get("discovered"):
            entry = {**entry, "discovered": True}
        settings[key] = entry
        self._write(records)

    # -- the command-line history ---------------------------------------------------

    def history(self, node: Contact) -> list[str]:
        """The node's remote CLI history, oldest first."""
        record = self._load_all().get(admin_key(node))
        raw = record.get("history") if isinstance(record, dict) else None
        return [str(c) for c in raw] if isinstance(raw, list) else []

    def append_history(self, node: Contact, command: str) -> None:
        """Append one sent command to the node's history (dropping an adjacent dupe)."""
        command = command.strip()
        if not command:
            return
        records = self._load_all()
        record = records.setdefault(admin_key(node), {})
        history = record.setdefault("history", [])
        if history and history[-1] == command:
            return  # re-running the last command shouldn't stutter the recall
        history.append(command)
        del history[:-HISTORY_CAP]
        self._write(records)

    def _write(self, records: dict[str, dict]) -> None:
        """Persist ``records`` atomically (crash mid-write keeps the previous file)."""
        write_atomically(self._path, json.dumps(records, indent=2))
