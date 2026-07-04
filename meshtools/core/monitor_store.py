"""Persistence for the passive-monitor on/off preference.

Whether background monitoring is enabled is remembered between sessions in a small JSON
file in the config directory (``<config_dir>/monitor.json``), following the same pattern
as :class:`~meshtools.core.device_store.DeviceStore`: it is machine/user state managed by
the app rather than user-authored config, and it must survive a per-invocation ``--db``
override, so it lives beside the config rather than in the swappable database.

The preference defaults to *off*: a fresh install never captures until the user opts in.
"""

from __future__ import annotations

import json
from pathlib import Path


class MonitorStore:
    """Reads and writes the remembered passive-monitor on/off preference."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    def load_enabled(self) -> bool:
        """Return whether monitoring is enabled, defaulting to ``False``.

        A missing or corrupt file is treated as "disabled" rather than an error, so a
        stray edit never blocks startup and never silently starts capturing.

        Returns:
            ``True`` if monitoring was left enabled, otherwise ``False``.
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(data.get("enabled", False)) if isinstance(data, dict) else False

    def save_enabled(self, enabled: bool) -> None:
        """Persist the monitoring on/off preference.

        Args:
            enabled: Whether background monitoring should be on for future sessions.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish replace so a crash mid-write can't leave a truncated file.
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"enabled": bool(enabled)}, indent=2), encoding="utf-8")
        tmp.replace(self._path)
