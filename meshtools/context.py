"""The application context: a small dependency container passed to every tool.

``AppContext`` owns the shared, expensive singletons (console, settings, repository,
logger) and lazily manages the device connection so tools that don't touch the radio
never open a serial port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console

from .core.config import DeviceProfile, Settings
from .core.connection import Device, make_device
from .core.device_store import DeviceStore
from .core.discovery import DiscoveredDevice, discover_devices
from .core.selection import resolve_device
from .persistence.logging import get_logger
from .persistence.repository import Repository


@dataclass(slots=True)
class AppContext:
    """Shared services and per-invocation state handed to tools.

    Attributes:
        console: Rich console for all rendering.
        settings: Loaded application settings.
        repo: Database repository.
        profile: Active device profile, if one was resolved.
        device_store: Store for the remembered "last known good" device.
        mock: Whether the simulator device is in use.
        port_override: Explicit serial port (from ``--port`` or the interactive picker),
            overriding the profile.
        json_output: Whether tools should emit machine-readable output.
        selected_device: The discovered device chosen for this session, when known, so it
            can be remembered after a successful connection.
        explicit_selection: Whether ``--port``/``--profile`` was passed explicitly (which
            suppresses the interactive picker and auto-discovery).
    """

    console: Console
    settings: Settings
    repo: Repository
    device_store: DeviceStore
    profile: Optional[DeviceProfile] = None
    mock: bool = False
    port_override: Optional[str] = None
    json_output: bool = False
    selected_device: Optional[DiscoveredDevice] = None
    explicit_selection: bool = False
    _device: Optional[Device] = field(default=None, init=False, repr=False)

    @property
    def profile_name(self) -> Optional[str]:
        """Name of the active profile, if any."""
        return self.profile.name if self.profile else None

    @property
    def log(self):  # type: ignore[no-untyped-def]
        """The application logger."""
        return get_logger()

    async def device(self) -> Device:
        """Return a connected :class:`Device`, opening the connection on first use.

        For real hardware this resolves which serial port to use (explicit ``--port`` /
        profile / remembered default / sole attached device) and, on a successful
        connection, records the device as the new "last known good" default.

        Returns:
            The shared, connected device for this invocation.

        Raises:
            ValueError: If no serial port can be chosen and the simulator is not enabled.
            DeviceSelectionError: If discovery is ambiguous (subclass of ``ValueError``).
        """
        if self._device is not None:
            return self._device

        if self.mock:
            self._device = make_device(mock=True, port=None)
            await self._device.connect()
            return self._device

        resolution = resolve_device(
            discover_devices(),
            self.device_store.load(),
            explicit_port=self.port_override,
            profile=self.profile,
        )
        if resolution.device is not None:
            self.selected_device = resolution.device
        baudrate = self.profile.baudrate if self.profile else 115200
        self._device = make_device(mock=False, port=resolution.port, baudrate=baudrate)
        await self._device.connect()
        # Connection succeeded: this is now the last known good device.
        if self.selected_device is not None:
            self.device_store.remember(self.selected_device)
        return self._device

    async def aclose(self) -> None:
        """Disconnect the device (if connected) and close the repository."""
        if self._device is not None:
            await self._device.disconnect()
            self._device = None
        self.repo.close()
