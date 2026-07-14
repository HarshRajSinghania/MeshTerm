"""Persistence for confirmed companion devices.

Every device that has ever spoken the MeshCore protocol to us successfully is remembered
forever in a small JSON registry in the config directory (``<config_dir>/devices.json``),
keyed by :attr:`~meshterm.core.discovery.DiscoveredDevice.stable_id` (a USB serial number
when available, so the record survives a changed COM number). This is deliberately *not* in
the SQLite database: the database can be swapped per-invocation with ``--db``, whereas the
remembered devices are global machine state that should survive that.

The registry also tracks which device was most recently connected (``last``), used to
preselect and star a default on the startup splash. Membership in the registry is what marks
a device as a confirmed MeshCore companion — the splash reserves its "MeshCore device" tag
for these, rather than guessing from the USB vendor ID.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .discovery import (
    TRANSPORT_BLE,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
    DiscoveredDevice,
)
from .models import utcnow


@dataclass(slots=True)
class RememberedDevice:
    """A previously confirmed device recorded for next-time defaulting.

    Attributes:
        stable_id: The device's :attr:`DiscoveredDevice.stable_id` at connect time.
        port: The serial port it was last seen on (informational; may have changed). Blank
            for a BLE/TCP device.
        label: A friendly label for display in prompts and tables.
        last_connected: ISO-8601 timestamp of the last successful connection.
        node_name: The device's own mesh node name, learned at connect time (may be empty).
        transport: ``"serial"``, ``"ble"``, or ``"tcp"`` — how this device was last reached.
        address: The Bluetooth address, for a BLE device (blank otherwise), so it can be
            reconnected directly without re-scanning.
        hardware_model: The firmware's own model string (e.g. ``"Seeed Tracker T1000-E"``),
            learned from the device-query at connect time. This is the only reliable source of
            the model — a BLE companion advertises none — so it's remembered here to fill the
            hardware column even when the device is merely attached, not connected. May be empty
            for a serial device confirmed by an older firmware that predates the query.
        host: The network host, for a TCP device (blank otherwise), so it can be reconnected
            directly. A TCP companion isn't discoverable, so this remembered endpoint is the
            *only* way it reappears in the picker.
        tcp_port: The TCP port, for a TCP device (0 otherwise).
    """

    stable_id: str
    port: str
    label: str
    last_connected: str
    node_name: str = ""
    transport: str = TRANSPORT_SERIAL
    address: str = ""
    hardware_model: str = ""
    host: str = ""
    tcp_port: int = 0

    @property
    def is_ble(self) -> bool:
        """Whether this remembered device was reached over Bluetooth LE."""
        return self.transport == TRANSPORT_BLE

    @property
    def is_tcp(self) -> bool:
        """Whether this remembered device was reached over a TCP network connection."""
        return self.transport == TRANSPORT_TCP

    @property
    def target(self) -> str:
        """The connection target: ``host:port`` for TCP, the BLE address for Bluetooth, else
        the serial port."""
        if self.is_tcp:
            return f"{self.host}:{self.tcp_port}"
        return self.address or self.port if self.is_ble else self.port

    def matches(self, device: DiscoveredDevice) -> bool:
        """Return whether ``device`` is the same hardware as this record."""
        return device.stable_id == self.stable_id


class DeviceStore:
    """Reads and writes the registry of confirmed companion devices."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    def _read(self) -> tuple[dict[str, RememberedDevice], Optional[str]]:
        """Return the parsed ``(registry, last_id)``; empty on a missing/corrupt file.

        A missing or corrupt file is treated as "nothing remembered" rather than an error,
        so a stray edit never blocks startup. An old flat-format file (a single record at
        the top level, from before the registry) is migrated in-memory to a one-entry
        registry so upgrades are seamless.
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, None
        if not isinstance(data, dict):
            return {}, None

        # Old flat shape: a single record with ``stable_id`` at the top level.
        if "devices" not in data and "stable_id" in data:
            record = self._record_from(data)
            if record is None:
                return {}, None
            return {record.stable_id: record}, record.stable_id

        registry: dict[str, RememberedDevice] = {}
        for entry in (data.get("devices") or {}).values():
            record = self._record_from(entry)
            if record is not None:
                registry[record.stable_id] = record
        last = data.get("last")
        if last not in registry:
            last = None
        return registry, last

    @staticmethod
    def _record_from(entry: object) -> Optional[RememberedDevice]:
        """Build a :class:`RememberedDevice` from one raw JSON entry, or ``None`` if invalid."""
        if not isinstance(entry, dict):
            return None
        try:
            return RememberedDevice(
                stable_id=entry["stable_id"],
                port=entry.get("port", ""),
                label=entry.get("label", entry.get("port", "")),
                last_connected=entry.get("last_connected", ""),
                node_name=entry.get("node_name", ""),
                transport=entry.get("transport", TRANSPORT_SERIAL),
                address=entry.get("address", ""),
                hardware_model=entry.get("hardware_model", ""),
                host=entry.get("host", ""),
                tcp_port=int(entry.get("tcp_port", 0) or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def load(self) -> Optional[RememberedDevice]:
        """Return the most recently connected device, or ``None`` if none is remembered.

        This is the "last known good" default used to preselect and star a row on the
        startup splash and to resolve a port non-interactively.

        Returns:
            The last-connected :class:`RememberedDevice`, or ``None``.
        """
        registry, last = self._read()
        return registry.get(last) if last is not None else None

    def load_all(self) -> dict[str, RememberedDevice]:
        """Return the full registry of confirmed devices, keyed by ``stable_id``."""
        registry, _ = self._read()
        return registry

    def is_known(self, device: DiscoveredDevice) -> bool:
        """Return whether ``device`` has ever been confirmed as a MeshCore companion."""
        registry, _ = self._read()
        return device.stable_id in registry

    def remember(
        self, device: DiscoveredDevice, *, node_name: str = "", hardware_model: str = ""
    ) -> None:
        """Record ``device`` as a confirmed connection and the new default.

        Upserts the device into the registry (so it is remembered forever) and marks it as
        the most recently connected one.

        Args:
            device: The device that just connected successfully.
            node_name: The device's own mesh node name, if known; preserved across
                reconnects and shown in the picker. A blank value keeps any name already
                on file for this device rather than erasing it.
            hardware_model: The firmware's model string, if known; preserved the same way, so
                a reconnect on firmware that couldn't answer the query doesn't erase a model
                learned earlier.
        """
        registry, _ = self._read()
        existing = registry.get(device.stable_id)
        if not node_name and existing is not None:
            node_name = existing.node_name
        if not hardware_model and existing is not None:
            hardware_model = existing.hardware_model
        registry[device.stable_id] = RememberedDevice(
            stable_id=device.stable_id,
            port=device.port,
            label=device.label,
            last_connected=utcnow().isoformat(),
            node_name=node_name,
            transport=device.transport,
            address=device.address or "",
            hardware_model=hardware_model,
            host=device.host or "",
            tcp_port=device.tcp_port or 0,
        )
        self._write(registry, device.stable_id)

    def forget(self, stable_id: str) -> bool:
        """Drop a remembered device from the registry.

        The inverse of :meth:`remember`: removes the record keyed by ``stable_id`` so the
        device is no longer listed as a confirmed companion. Used by the device picker's
        Delete action to prune a network (TCP) companion the user no longer wants — a TCP
        device is listed *only* from its remembered endpoint, so forgetting it is what makes
        it leave the picker. If the forgotten device was the most-recently-connected default,
        that pointer is handed to the newest surviving record (or cleared when none remain),
        so the next startup still preselects a sensible device.

        Args:
            stable_id: The :attr:`RememberedDevice.stable_id` of the device to forget.

        Returns:
            ``True`` if a record was removed, ``False`` if none matched.
        """
        registry, last = self._read()
        if stable_id not in registry:
            return False
        del registry[stable_id]
        if last == stable_id:
            last = max(
                registry, key=lambda k: registry[k].last_connected, default=None
            )
        self._write(registry, last)
        return True

    def _write(self, registry: dict[str, RememberedDevice], last: Optional[str]) -> None:
        """Persist the registry, marking ``last`` as the most recently connected device."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "devices": {
                stable_id: {
                    "stable_id": record.stable_id,
                    "port": record.port,
                    "label": record.label,
                    "last_connected": record.last_connected,
                    "node_name": record.node_name,
                    "transport": record.transport,
                    "address": record.address,
                    "hardware_model": record.hardware_model,
                    "host": record.host,
                    "tcp_port": record.tcp_port,
                }
                for stable_id, record in registry.items()
            },
            "last": last,
        }
        # Atomic-ish replace so a crash mid-write can't truncate the existing registry.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
