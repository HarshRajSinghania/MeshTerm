"""The application context: a small dependency container passed to every tool.

``AppContext`` owns the shared, expensive singletons (console, settings, repository,
logger) and lazily manages the device connection so tools that don't touch the radio
never open a serial port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from rich.console import Console

from .core.admin_store import AdminStore
from .core.config import DeviceProfile, Settings
from .core.connection import Device, make_device
from .core.device_store import DeviceStore
from .core.discovery import DiscoveredDevice, discover_devices
from .core.selection import resolve_device
from .persistence.logging import get_logger
from .persistence.repository import Repository

if TYPE_CHECKING:
    from .services.event_hub import EventHub
    from .services.monitor_service import MonitorService
    from .ui.surface import Ui


@dataclass(slots=True)
class AppContext:
    """Shared services and per-invocation state handed to tools.

    Attributes:
        console: Rich console for all rendering.
        settings: Loaded application settings.
        repo: Database repository.
        profile: Active device profile, if one was resolved.
        device_store: Store for the remembered "last known good" device.
        admin_store: Store for remembered remote-node admin passwords.
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
    admin_store: AdminStore
    profile: Optional[DeviceProfile] = None
    mock: bool = False
    port_override: Optional[str] = None
    json_output: bool = False
    selected_device: Optional[DiscoveredDevice] = None
    explicit_selection: bool = False
    _device: Optional[Device] = field(default=None, init=False, repr=False)
    _events: "Optional[EventHub]" = field(default=None, init=False, repr=False)
    _monitor: "Optional[MonitorService]" = field(default=None, init=False, repr=False)
    _ui: "Optional[Ui]" = field(default=None, init=False, repr=False)

    @property
    def profile_name(self) -> Optional[str]:
        """Name of the active profile, if any."""
        return self.profile.name if self.profile else None

    @property
    def ui(self) -> "Ui":
        """The active UI surface, defaulting to the plain console surface for the CLI.

        The interactive menu replaces this with a full-screen TUI surface for the session;
        scripted CLI runs use the lazily-created plain surface, which prints directly. The
        surface module (and prompt_toolkit) is imported lazily here to keep startup fast.
        """
        if self._ui is None:
            from .ui.surface import PlainUi

            self._ui = PlainUi(self.console)
        return self._ui

    @ui.setter
    def ui(self, value: "Ui") -> None:
        """Install a UI surface (used by the menu to switch to the full-screen TUI)."""
        self._ui = value

    @property
    def events(self) -> "EventHub":
        """Return the session's always-on event hub, creating it on first use.

        The hub owns the single device event subscription and fans events out to any
        number of subscribers (the passive monitor's logging is one of them). It is
        created idle here; the interactive session starts it once a device is available.
        """
        if self._events is None:
            from .services.event_hub import EventHub

            self._events = EventHub(self)
        return self._events

    @property
    def monitor(self) -> "MonitorService":
        """Return the session's passive-monitor service, creating it on first use.

        The service is built lazily so the (cheap) database read for the "total"
        observation count and the on/off preference load happen only once, when
        monitoring is first referenced.
        """
        if self._monitor is None:
            from .core.monitor_store import MonitorStore
            from .services.monitor_service import MonitorService

            self._monitor = MonitorService(
                self, MonitorStore(self.settings.config_dir / "monitor.json")
            )
        return self._monitor

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
        """Stop monitoring, stop the event hub, disconnect the device, and close the repo."""
        if self._monitor is not None:
            await self._monitor.aclose()
        if self._events is not None:
            await self._events.aclose()
        if self._device is not None:
            await self._device.disconnect()
            self._device = None
        self.repo.close()
