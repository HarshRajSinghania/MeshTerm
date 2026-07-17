"""Configuration and named device profiles.

Settings are read from a TOML file (default ``~/.meshterm/config.toml``) and may
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


#: Hard ceiling on a direct message's *soft retries* — the automatic re-sends that follow the
#: initial transmission when it goes unacknowledged. Capped at 2, so a message gets at most one
#: send plus two soft retries (three tries total) and is never re-broadcast more than twice on
#: the shared mesh before being called a failure. :data:`Settings.direct_message_soft_retries`
#: is clamped to ``0..DIRECT_MESSAGE_MAX_SOFT_RETRIES``.
DIRECT_MESSAGE_MAX_SOFT_RETRIES = 2


def default_config_dir() -> Path:
    """Return the directory MeshTerm uses for config and data.

    Resolves to ``.meshterm`` under the OS-defined home directory (``%USERPROFILE%``
    on Windows, ``$HOME`` on Unix), as reported by :meth:`Path.home`.

    Returns:
        The resolved configuration directory path (not guaranteed to exist).
    """
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
    port: Optional[str] = None
    baudrate: int = 115200
    default_tx_power: Optional[int] = None
    description: str = ""
    transport: str = "serial"
    address: Optional[str] = None
    ble_pin: Optional[str] = None
    host: Optional[str] = None
    tcp_port: Optional[int] = None

    @property
    def is_ble(self) -> bool:
        """Whether this profile addresses a Bluetooth LE companion."""
        return self.transport == "ble"

    @property
    def is_tcp(self) -> bool:
        """Whether this profile addresses a network (TCP) companion."""
        return self.transport == "tcp"

    @property
    def tcp_endpoint(self) -> Optional[str]:
        """The ``host:port`` string for a TCP profile, or ``None`` if it isn't one / has no host."""
        if not self.is_tcp or not self.host:
            return None
        from .discovery import DEFAULT_TCP_PORT

        return f"{self.host}:{self.tcp_port or DEFAULT_TCP_PORT}"


@dataclass(slots=True)
class Settings:
    """Top-level application settings.

    Attributes:
        config_dir: Directory holding the config file and database.
        db_path: SQLite database location.
        default_profile: Profile used when ``--profile`` is omitted.
        profiles: Mapping of profile name to :class:`DeviceProfile`.
        trace_cooldown_s: Minimum delay between transmit bursts (duty-cycle safety).
        tx_opt_min: Lowest TX power explored by the remote-admin optimizer (dBm).
        tx_opt_max: Highest TX power explored by the remote-admin optimizer (dBm).
        direct_message_soft_retries: How many times a direct message is automatically
            re-sent after its initial transmission goes unacknowledged, before it is reported
            as failed. Each soft retry waits a full delivery-ack window (see
            :meth:`Device.send_direct_message`) before firing, so a message only resends after
            genuinely going unanswered rather than hammering the radio. Capped at 0, 1, or 2 —
            clamped to ``0..DIRECT_MESSAGE_MAX_SOFT_RETRIES`` on load — so the send is tried at
            most three times total. ``0`` disables soft retries (one shot); the default of
            ``2`` matches the mesh convention of a few tries before giving up. The manual
            Ctrl-R resend in the chat screen is a further, user-driven retry layered on top of
            these automatic ones, not a substitute for them.
        connect_on_start: Whether the interactive session opens the companion connection
            (and starts always-on background listening) immediately at launch. When
            ``False`` the connection is opened lazily — only once monitoring is turned on
            or a tool first needs the radio — so launching the menu touches no serial port.
        history_days: How many days of overheard-packet history the recorder retains.
            Observations older than this are pruned once per session start (the
            housekeeping sweep), keeping the database bounded while the dashboard and
            Time Machine draw on everything inside the window. ``0`` disables pruning
            entirely — history grows forever.
    """

    config_dir: Path = field(default_factory=default_config_dir)
    db_path: Optional[Path] = None
    default_profile: Optional[str] = None
    profiles: dict[str, DeviceProfile] = field(default_factory=dict)
    trace_cooldown_s: float = 1.0
    tx_opt_min: int = 12
    tx_opt_max: int = 28
    direct_message_soft_retries: int = 2
    connect_on_start: bool = True
    history_days: int = 365

    def __post_init__(self) -> None:
        """Derive dependent paths that were not explicitly provided."""
        if self.db_path is None:
            self.db_path = self.config_dir / "meshterm.db"

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
            trace_cooldown_s=float(data.get("trace_cooldown_s", 1.0)),
            tx_opt_min=int(data.get("tx_opt_min", 12)),
            tx_opt_max=int(data.get("tx_opt_max", 28)),
            direct_message_soft_retries=max(
                0,
                min(
                    DIRECT_MESSAGE_MAX_SOFT_RETRIES,
                    int(data.get("direct_message_soft_retries", DIRECT_MESSAGE_MAX_SOFT_RETRIES)),
                ),
            ),
            connect_on_start=bool(data.get("connect_on_start", True)),
            history_days=max(0, int(data.get("history_days", 365))),
        )
