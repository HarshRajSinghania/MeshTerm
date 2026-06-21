"""Configuration and named device profiles.

Settings are read from a TOML file (default ``~/.config/meshtools/config.toml``) and may
be overridden per-invocation by CLI flags. Profiles let you alias your hardware
(``yagi`` repeater, ``local`` repeater, ``observer`` bot, ``s3`` serial companion) to a
serial port and defaults so commands can target them by name.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


def default_config_dir() -> Path:
    """Return the directory MeshTools uses for config and data.

    Honors ``$XDG_CONFIG_HOME`` when set; otherwise falls back to ``~/.config``.

    Returns:
        The resolved configuration directory path (not guaranteed to exist).
    """
    import os

    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "meshtools"


@dataclass(slots=True)
class DeviceProfile:
    """Connection defaults for one physical companion device.

    Attributes:
        name: The profile alias (e.g. ``"yagi"``).
        port: Serial port path (e.g. ``COM5`` or ``/dev/ttyUSB0``).
        baudrate: Serial baud rate.
        default_tx_power: TX power to assume/restore for this device, if known.
        description: Free-text note about the hardware.
    """

    name: str
    port: Optional[str] = None
    baudrate: int = 115200
    default_tx_power: Optional[int] = None
    description: str = ""


@dataclass(slots=True)
class Settings:
    """Top-level application settings.

    Attributes:
        config_dir: Directory holding the config file and database.
        db_path: SQLite database location.
        output_dir: Where generated visualizations are written.
        default_profile: Profile used when ``--profile`` is omitted.
        profiles: Mapping of profile name to :class:`DeviceProfile`.
        trace_cooldown_s: Minimum delay between transmit bursts (duty-cycle safety).
        tx_opt_min: Lowest TX power explored by the remote-admin optimizer (dBm).
        tx_opt_max: Highest TX power explored by the remote-admin optimizer (dBm).
    """

    config_dir: Path = field(default_factory=default_config_dir)
    db_path: Optional[Path] = None
    output_dir: Optional[Path] = None
    default_profile: Optional[str] = None
    profiles: dict[str, DeviceProfile] = field(default_factory=dict)
    trace_cooldown_s: float = 1.0
    tx_opt_min: int = 12
    tx_opt_max: int = 28

    def __post_init__(self) -> None:
        """Derive dependent paths that were not explicitly provided."""
        if self.db_path is None:
            self.db_path = self.config_dir / "meshtools.db"
        if self.output_dir is None:
            self.output_dir = self.config_dir / "output"

    def resolve_profile(self, name: Optional[str]) -> Optional[DeviceProfile]:
        """Look up a profile by name, falling back to the default profile.

        Args:
            name: Requested profile name, or ``None`` to use the default.

        Returns:
            The matching :class:`DeviceProfile`, or ``None`` if neither the requested
            nor the default profile is defined.
        """
        key = name or self.default_profile
        if key is None:
            return None
        return self.profiles.get(key)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "Settings":
        """Load settings from a TOML file, returning defaults if it is absent.

        Args:
            config_path: Explicit path to a config file. Defaults to
                ``<config_dir>/config.toml``.

        Returns:
            A populated :class:`Settings` instance.
        """
        config_dir = default_config_dir()
        path = config_path or (config_dir / "config.toml")
        data: dict[str, Any] = {}
        if path.exists():
            with path.open("rb") as fh:
                data = tomllib.load(fh)

        profiles: dict[str, DeviceProfile] = {}
        for pname, pdata in (data.get("profiles") or {}).items():
            profiles[pname] = DeviceProfile(
                name=pname,
                port=pdata.get("port"),
                baudrate=pdata.get("baudrate", 115200),
                default_tx_power=pdata.get("default_tx_power"),
                description=pdata.get("description", ""),
            )

        return cls(
            config_dir=config_dir,
            db_path=Path(data["db_path"]) if data.get("db_path") else None,
            output_dir=Path(data["output_dir"]) if data.get("output_dir") else None,
            default_profile=data.get("default_profile"),
            profiles=profiles,
            trace_cooldown_s=float(data.get("trace_cooldown_s", 1.0)),
            tx_opt_min=int(data.get("tx_opt_min", 12)),
            tx_opt_max=int(data.get("tx_opt_max", 28)),
        )
