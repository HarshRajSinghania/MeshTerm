# SPDX-License-Identifier: Apache-2.0
"""TOML backup and restore for device configuration.

A backup captures every registry setting's current value plus experimental custom
variables and configured channels, so a device's configuration can be archived, diffed,
or cloned onto another node. Restore turns a backup into the same operation list the
``config`` tool executes for live edits, and supports a dry-run diff against the device's
current state.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from .device_config import DEVICE_SETTINGS, get_spec, parse_value

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


@dataclass(slots=True)
class Backup:
    """A parsed configuration backup.

    Attributes:
        settings: Registry setting key -> raw value.
        custom: Experimental custom variable name -> value.
        channels: One dict per channel with ``index``, ``name`` and hex ``secret``.
    """

    settings: dict[str, Any] = field(default_factory=dict)
    custom: dict[str, str] = field(default_factory=dict)
    channels: list[dict[str, Any]] = field(default_factory=list)


def backup_config(
    path: Path,
    snapshot: dict,
    custom_vars: dict[str, str],
    channels: list[dict],
) -> Path:
    """Write the current configuration to a TOML file.

    Args:
        path: Destination file path (parent directories are created).
        snapshot: A device snapshot (see ``device_config.build_snapshot``).
        custom_vars: Experimental custom variables.
        channels: Channel dicts (``channel_idx``, ``channel_name``, ``channel_secret``
            as raw bytes).

    Returns:
        The path written.
    """
    settings: dict[str, Any] = {}
    for spec in DEVICE_SETTINGS:
        value = spec.getter(snapshot)
        if value is not None:
            settings[spec.key] = value

    doc: dict[str, Any] = {"settings": settings}
    if custom_vars:
        doc["custom"] = dict(custom_vars)
    if channels:
        doc["channels"] = [
            {
                "index": ch.get("channel_idx", idx),
                "name": ch.get("channel_name", ""),
                "secret": _to_hex(ch.get("channel_secret")),
            }
            for idx, ch in enumerate(channels)
        ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(doc, fh)
    return path


def read_backup(path: Path) -> Backup:
    """Read and parse a TOML configuration backup.

    Args:
        path: The backup file to read.

    Returns:
        A :class:`Backup`.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    return Backup(
        settings=dict(doc.get("settings") or {}),
        custom=dict(doc.get("custom") or {}),
        channels=list(doc.get("channels") or []),
    )


def plan_restore(
    backup: Backup,
    snapshot: dict,
    current_custom: dict[str, str],
) -> list[tuple]:
    """Diff a backup against the current state and return the operations to apply.

    Only values that differ from the device's current configuration are emitted, so a
    restore (or its dry-run preview) shows exactly what would change.

    Args:
        backup: The parsed backup.
        snapshot: The device's current snapshot.
        current_custom: The device's current custom variables.

    Returns:
        A list of operation tuples compatible with the ``config`` tool:
        ``("set", key, value)``, ``("set_custom", key, value)``, and
        ``("set_channel", index, name, secret_bytes)``.

    Raises:
        DeviceConfigError: If a backed-up setting key or value is invalid.
    """
    ops: list[tuple] = []
    for key, raw in backup.settings.items():
        spec = get_spec(key)  # raises on unknown key
        value = parse_value(spec, raw, snapshot)
        if value != spec.getter(snapshot):
            ops.append(("set", key, value))

    for key, value in backup.custom.items():
        if current_custom.get(key) != value:
            ops.append(("set_custom", key, str(value)))

    for ch in backup.channels:
        secret_hex = ch.get("secret") or ""
        secret = bytes.fromhex(secret_hex) if secret_hex else None
        ops.append(("set_channel", int(ch.get("index", 0)), str(ch.get("name", "")), secret))

    return ops


def _to_hex(secret: Any) -> str:
    """Render a channel secret as a hex string, accepting bytes or str."""
    if isinstance(secret, (bytes, bytearray)):
        return secret.hex()
    return str(secret or "")
