"""Persistence for the last successfully connected companion device.

The "last known good" device is remembered in a small JSON file in the config directory
(``<config_dir>/devices.json``), deliberately *not* in the SQLite database: the database
can be swapped per-invocation with ``--db``, whereas the remembered device is global
machine state that should survive that. The record is matched back to currently attached
hardware by :attr:`~meshterm.core.discovery.DiscoveredDevice.stable_id`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .discovery import DiscoveredDevice
from .models import utcnow


@dataclass(slots=True)
class RememberedDevice:
    """A previously connected device recorded for next-time defaulting.

    Attributes:
        stable_id: The device's :attr:`DiscoveredDevice.stable_id` at connect time.
        port: The serial port it was last seen on (informational; may have changed).
        label: A friendly label for display in prompts and tables.
        node_name: The device's own mesh node name, learned at connect time (may be empty).
        last_connected: ISO-8601 timestamp of the last successful connection.
    """

    stable_id: str
    port: str
    label: str
    last_connected: str
    node_name: str = ""

    def matches(self, device: DiscoveredDevice) -> bool:
        """Return whether ``device`` is the same hardware as this record."""
        return device.stable_id == self.stable_id


class DeviceStore:
    """Reads and writes the remembered "last known good" device."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    def load(self) -> Optional[RememberedDevice]:
        """Return the remembered device, or ``None`` if absent or unreadable.

        A missing or corrupt file is treated as "nothing remembered" rather than an
        error, so a stray edit never blocks startup.

        Returns:
            The :class:`RememberedDevice`, or ``None``.
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return RememberedDevice(
                stable_id=data["stable_id"],
                port=data["port"],
                label=data.get("label", data["port"]),
                last_connected=data.get("last_connected", ""),
                node_name=data.get("node_name", ""),
            )
        except (KeyError, TypeError):
            return None

    def remember(self, device: DiscoveredDevice, *, node_name: str = "") -> None:
        """Record ``device`` as the last known good connection.

        Args:
            device: The device that just connected successfully.
            node_name: The device's own mesh node name, if known; preserved across
                reconnects and shown in the picker. A blank value keeps any name already
                on file rather than erasing it.
        """
        if not node_name:
            existing = self.load()
            if existing is not None and existing.matches(device):
                node_name = existing.node_name
        record = RememberedDevice(
            stable_id=device.stable_id,
            port=device.port,
            label=device.label,
            last_connected=utcnow().isoformat(),
            node_name=node_name,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stable_id": record.stable_id,
            "port": record.port,
            "label": record.label,
            "last_connected": record.last_connected,
            "node_name": record.node_name,
        }
        # Atomic-ish replace so a crash mid-write can't truncate the existing record.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
