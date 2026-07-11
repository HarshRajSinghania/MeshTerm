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
from typing import Optional

from .admin_store import admin_key
from .models import Contact, utcnow

#: How many command-line entries are kept per node (newest last).
HISTORY_CAP = 100


@dataclass(frozen=True, slots=True)
class CachedValue:
    """One remembered remote setting: what the node last said, and when.

    Attributes:
        value: The value as display text.
        read_at: When it was read from (or written to) the node.
    """

    value: str
    read_at: Optional[datetime]


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
            if not isinstance(entry, dict) or "value" not in entry:
                continue
            read_at: Optional[datetime] = None
            stamp = entry.get("read_at")
            if isinstance(stamp, str):
                try:
                    read_at = datetime.fromisoformat(stamp)
                except ValueError:
                    read_at = None
            out[key] = CachedValue(value=str(entry["value"]), read_at=read_at)
        return out

    def remember_setting(self, node: Contact, key: str, value: str) -> None:
        """Cache one setting's value for ``node``, stamped now."""
        records = self._load_all()
        record = records.setdefault(admin_key(node), {})
        settings = record.setdefault("settings", {})
        settings[key] = {"value": value, "read_at": utcnow().isoformat()}
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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        tmp.replace(self._path)
