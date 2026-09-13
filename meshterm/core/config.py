# SPDX-License-Identifier: Apache-2.0
"""Machine setup and named device profiles.

Settings are read from a TOML file (default ``~/.meshterm/config.toml``) and may
be overridden per-invocation by CLI flags. Profiles let you alias your hardware
(``yagi`` repeater, ``local`` repeater, ``observer`` bot, ``s3`` serial companion) to a
serial port and defaults so commands can target them by name.

This file answers *where things are and which device to talk to* — nothing else. How
MeshTerm itself behaves (cooldowns, retry budgets, how much history it keeps, whether it
connects at launch) is a **preference**, lives in
:mod:`meshterm.core.preferences`, and is edited on the Preferences page rather than in a
text editor. The two were one thing until the page existed, which is why a few behaviour
keys used to sit in this file with no screen behind them.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


#: Points MeshTerm's config and data somewhere other than the home directory. For trying a
#: build without letting it near a real history, for a portable install on a stick, and for
#: running two radios out of two directories. Read every time rather than cached, so a test
#: can move it between cases.
CONFIG_DIR_ENV = "MESHTERM_HOME"


def default_config_dir() -> Path:
    """Return the directory MeshTerm uses for config and data.

    ``$MESHTERM_HOME`` wins if it is set and not empty. Otherwise this resolves to
    ``.meshterm`` under the OS-defined home directory (``%USERPROFILE%`` on Windows,
    ``$HOME`` on Unix), as reported by :meth:`Path.home`.

    The override exists because the database is the valuable part of an install — a
    running record of everything the radio has overheard — and there was no way to point a
    second copy of MeshTerm at a different one. A downloaded build would happily open the
    same 34MB file as the checkout it was built from.

    Returns:
        The resolved configuration directory path (not guaranteed to exist).
    """
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".meshterm"


@dataclass(slots=True)
class DeviceProfile:
    """Connection defaults for one physical companion device.

    A profile addresses a serial companion (via ``port``), a Bluetooth one (via
    ``transport = "ble"`` and ``address``), or a network one (via ``transport = "tcp"``,
    ``host``, and ``tcp_port``). ``transport`` defaults to serial, so existing port-only
    profiles are unchanged.

    Attributes:
        name: The profile alias (e.g. ``"yagi"``).
        port: Serial port path (e.g. ``COM5`` or ``/dev/ttyUSB0``); serial profiles.
        baudrate: Serial baud rate.
        default_tx_power: TX power to assume/restore for this device, if known.
        description: Free-text note about the hardware.
        transport: ``"serial"`` (default), ``"ble"``, or ``"tcp"``.
        address: Bluetooth address (e.g. ``AA:BB:CC:DD:EE:FF``); BLE profiles.
        ble_pin: Optional BLE pairing PIN, if the Bluetooth companion requires one.
        host: Hostname or IP of a network companion (e.g. ``"192.168.1.50"``); TCP profiles.
        tcp_port: TCP port the network companion listens on; TCP profiles (defaults to
            :data:`~meshterm.core.discovery.DEFAULT_TCP_PORT` when a host is given without one).
    """

    name: str
    port: str | None = None
    baudrate: int = 115200
    default_tx_power: int | None = None
    description: str = ""
    transport: str = "serial"
    address: str | None = None
    ble_pin: str | None = None
    host: str | None = None
    tcp_port: int | None = None

    @property
    def is_ble(self) -> bool:
        """Whether this profile addresses a Bluetooth LE companion."""
        return self.transport == "ble"

    @property
    def is_tcp(self) -> bool:
        """Whether this profile addresses a network (TCP) companion."""
        return self.transport == "tcp"

    @property
    def tcp_endpoint(self) -> str | None:
        """The ``host:port`` string for a TCP profile, or ``None`` if it isn't one / has no host."""
        if not self.is_tcp or not self.host:
            return None
        from .discovery import DEFAULT_TCP_PORT

        return f"{self.host}:{self.tcp_port or DEFAULT_TCP_PORT}"


@dataclass(slots=True)
class Settings:
    """Where MeshTerm keeps its files, and which device to talk to.

    Attributes:
        config_dir: Directory holding the config file and database.
        db_path: SQLite database location.
        default_profile: Profile used when ``--profile`` is omitted.
        profiles: Mapping of profile name to :class:`DeviceProfile`.
        connect_on_start: Whether the interactive session opens the companion connection
            (and starts always-on background listening) immediately at launch. When
            ``False`` the connection is opened lazily — only once monitoring is turned on
            or a tool first needs the radio — so launching the menu touches no serial
            port. It sits here rather than among the preferences because it is about
            *which device this machine talks to and when*, alongside the profile that
            names it, and because it decides its own question before the session that
            would show a preferences page exists.
    """

    config_dir: Path = field(default_factory=default_config_dir)
    db_path: Path | None = None
    default_profile: str | None = None
    profiles: dict[str, DeviceProfile] = field(default_factory=dict)
    connect_on_start: bool = True

    def __post_init__(self) -> None:
        """Derive dependent paths that were not explicitly provided."""
        if self.db_path is None:
            self.db_path = self.config_dir / "meshterm.db"

    def resolve_profile(self, name: str | None) -> DeviceProfile | None:
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
    def load(cls, config_path: Path | None = None) -> Settings:
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
            # Infer the transport from which endpoint the profile carries when it isn't stated
            # outright: a ``host`` means TCP, an ``address`` means BLE, otherwise serial. This
            # keeps port-only profiles working untouched while a bare ``host``/``address`` is
            # enough to declare a network/Bluetooth one.
            transport = pdata.get("transport") or (
                "tcp" if pdata.get("host") else "ble" if pdata.get("address") else "serial"
            )
            tcp_port = pdata.get("tcp_port")
            profiles[pname] = DeviceProfile(
                name=pname,
                port=pdata.get("port"),
                baudrate=pdata.get("baudrate", 115200),
                default_tx_power=pdata.get("default_tx_power"),
                description=pdata.get("description", ""),
                transport=transport,
                address=pdata.get("address"),
                ble_pin=pdata.get("ble_pin"),
                host=pdata.get("host"),
                tcp_port=int(tcp_port) if tcp_port is not None else None,
            )

        return cls(
            config_dir=config_dir,
            db_path=Path(data["db_path"]) if data.get("db_path") else None,
            default_profile=data.get("default_profile"),
            profiles=profiles,
            connect_on_start=bool(data.get("connect_on_start", True)),
        )
