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
    from .services.chat_service import ChatService
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
        ble_override: Explicit Bluetooth address (from ``--ble`` or the interactive picker),
            selecting the BLE transport. Takes precedence over ``port_override``.
        ble_pin: Optional BLE pairing PIN for the chosen Bluetooth device.
        json_output: Whether tools should emit machine-readable output.
        selected_device: The discovered device chosen for this session, when known, so it
            can be remembered after a successful connection.
        explicit_selection: Whether ``--port``/``--ble``/``--profile`` was passed explicitly
            (which suppresses the interactive picker and auto-discovery).
    """

    console: Console
    settings: Settings
    repo: Repository
    device_store: DeviceStore
    admin_store: AdminStore
    profile: Optional[DeviceProfile] = None
    mock: bool = False
    port_override: Optional[str] = None
    ble_override: Optional[str] = None
    ble_pin: Optional[str] = None
    json_output: bool = False
    selected_device: Optional[DiscoveredDevice] = None
    explicit_selection: bool = False
    _device: Optional[Device] = field(default=None, init=False, repr=False)
    _active_port: Optional[str] = field(default=None, init=False, repr=False)
    _active_transport: Optional[str] = field(default=None, init=False, repr=False)
    _active_address: Optional[str] = field(default=None, init=False, repr=False)
    unpair_on_exit: bool = field(default=False, init=False, repr=False)
    #: Set by the config editor just before it sends a reboot command, so the session's
    #: disconnect watcher can label the ensuing (expected) link drop as a reboot in
    #: progress rather than a surprise unplug. Cleared by the reconnect dialog that
    #: consumes it (see :func:`meshterm.ui.menu._handle_disconnect`).
    reboot_in_progress: bool = field(default=False, init=False, repr=False)
    _resume_intent: Optional[tuple[bool, bool, bool]] = field(
        default=None, init=False, repr=False
    )
    _events: "Optional[EventHub]" = field(default=None, init=False, repr=False)
    _monitor: "Optional[MonitorService]" = field(default=None, init=False, repr=False)
    _chat: "Optional[ChatService]" = field(default=None, init=False, repr=False)
    _ui: "Optional[Ui]" = field(default=None, init=False, repr=False)

    @property
    def profile_name(self) -> Optional[str]:
        """Name of the active profile, if any."""
        return self.profile.name if self.profile else None

    @property
    def is_connected(self) -> bool:
        """Whether a device connection is currently open."""
        return self._device is not None

    @property
    def active_port(self) -> Optional[str]:
        """The serial port the current connection is open on, if any (``None`` for --mock/BLE).

        Set when a real *serial* connection is opened so the reconnect flow knows which OS
        port to wait on. ``None`` for the simulator and for BLE connections (which have no
        serial port). Falls back to an explicit ``--port`` override when a connection hasn't
        recorded one yet.
        """
        if self.mock or self.active_transport == "ble":
            return None
        return self._active_port or self.port_override

    @property
    def active_address(self) -> Optional[str]:
        """The Bluetooth address of the current BLE connection, if any (``None`` otherwise).

        Set when a real *BLE* connection is opened, so post-session teardown (e.g. the quit
        dialog's unpair step) can address the peripheral even after the device handle is torn
        down. ``None`` for the simulator and for serial connections, which have no BLE address.
        """
        if self.mock or self.active_transport != "ble":
            return None
        return self._active_address or self.ble_override

    @property
    def active_transport(self) -> Optional[str]:
        """The transport of the current/selected connection: ``"serial"``, ``"ble"``, or ``None``.

        ``None`` for the simulator. For a real device it reflects the open connection when
        one exists, otherwise the transport implied by the pending selection (an explicit
        ``--ble`` selects BLE), defaulting to serial.
        """
        if self.mock:
            return None
        if self._active_transport is not None:
            return self._active_transport
        return "ble" if self.ble_override else "serial"

    async def link_alive(self) -> bool:
        """Whether the current device's transport link is still up (best-effort, non-invasive).

        Delegates to the connected device's :meth:`~meshterm.core.connection.Device.link_present`
        — OS port enumeration for serial, the BLE client's connection flag for Bluetooth — so
        the session's liveness watcher is transport-agnostic. Returns ``False`` when nothing is
        connected (there is no live link), and ``True`` on any check hiccup so a transient
        lookup failure never fakes a disconnect.
        """
        device = self._device
        if device is None:
            return False
        try:
            return await device.link_present()
        except Exception:  # noqa: BLE001 - a liveness-check failure must not fake a disconnect
            return True

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
    def chat(self) -> "ChatService":
        """Return the session's chat service, creating it on first use.

        The service records inbound messages to history (as a subscriber of the always-on
        event hub) and owns the outbound send path and the per-conversation unread counts.
        It is created idle here; the interactive session starts it once a device is
        available (and the live chat screen starts it lazily otherwise).
        """
        if self._chat is None:
            from .services.chat_service import ChatService

            self._chat = ChatService(self)
        return self._chat

    @property
    def log(self):  # type: ignore[no-untyped-def]
        """The application logger."""
        return get_logger()

    def adopt_device(self, device: Device) -> None:
        """Adopt an already-connected device as the session's device.

        Used by the startup picker: it opens and confirms the chosen companion during its
        smoke test, and hands that live connection here so :meth:`device` reuses it instead
        of opening the radio a second time (many boards reset on each serial open, making a
        reconnect slow and unreliable).

        Args:
            device: A connected :class:`Device` to serve as this session's radio.
        """
        self._device = device
        # Record the transport and endpoint the picker opened directly, bypassing the
        # resolution in ``device()`` that normally sets them — so the liveness watcher knows
        # what to poll and ``reconnect`` can rebuild the same connection.
        self._active_transport = getattr(device, "transport", "serial")
        self._active_port = getattr(device, "_port", None)
        self._active_address = getattr(device, "_address", None)

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
            self._active_transport = None
            self._active_port = None
            self._active_address = None
            await self._device.connect()
            return self._device

        # A Bluetooth endpoint (an explicit ``--ble``, a BLE profile, a device picked at
        # startup, or the remembered BLE default) is opened directly by address — no serial
        # resolution, and no re-scan needed to reconnect to a known address.
        ble_address, ble_pin = self._resolve_ble_endpoint()
        if ble_address:
            self._device = make_device(
                mock=False, port=None, transport="ble", address=ble_address, pin=ble_pin
            )
            self._active_transport = "ble"
            self._active_address = ble_address
            self._active_port = None
            await self._device.connect()
            await self._remember_connected()
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
        self._active_transport = "serial"
        self._active_port = resolution.port
        self._active_address = None
        await self._device.connect()
        await self._remember_connected()
        return self._device

    def _resolve_ble_endpoint(self) -> tuple[Optional[str], Optional[str]]:
        """Return the ``(address, pin)`` to open over Bluetooth, or ``(None, None)`` for serial.

        Resolves a BLE endpoint in priority order — an explicit ``--ble``, a BLE
        :class:`~meshterm.core.config.DeviceProfile`, then the remembered BLE default — so a
        Bluetooth companion is honored wherever a serial one would be. Returns ``(None, None)``
        when the session should fall through to serial resolution.
        """
        if self.ble_override:
            return self.ble_override, self.ble_pin
        # An explicit serial selection (``--port`` or a serial profile with a port) wins over a
        # remembered BLE default, mirroring the serial resolution priority.
        explicit_serial = bool(self.port_override) or (
            self.profile is not None and not self.profile.is_ble and bool(self.profile.port)
        )
        if self.profile is not None and self.profile.is_ble and self.profile.address:
            return self.profile.address, self.ble_pin or self.profile.ble_pin
        if explicit_serial:
            return None, None
        remembered = self.device_store.load()
        if remembered is not None and remembered.is_ble and remembered.target:
            self.selected_device = None  # remembered, not freshly discovered this session
            return remembered.target, self.ble_pin
        return None, None

    async def _remember_connected(self) -> None:
        """Record the just-connected device as the last known good default (best-effort).

        Learns the device's mesh node name (a probe failure must not block a good
        connection). Only devices discovered this session carry a
        :class:`~meshterm.core.discovery.DiscoveredDevice` to remember; a bare ``--port`` or
        remembered-by-address reconnect has nothing new to upsert.
        """
        if self.selected_device is not None:
            self.device_store.remember(
                self.selected_device,
                node_name=await self._node_name(),
                hardware_model=await self._hardware_model(),
            )

    async def reconnect(self) -> None:
        """Drop a lost device connection and rebuild it, restoring live services.

        Called after the companion link is detected as gone (see
        :func:`~meshterm.core.connection.is_connection_lost`). Tears down the dead
        connection and the services riding on it, opens a fresh connection to the same
        device, then restarts whatever was running before — so passive monitoring and chat
        recording resume transparently across a replug.

        Raises:
            Exception: If a new connection could not be opened (e.g. the device is still
                absent); the caller can surface it and offer to retry.
        """
        # Remember what was live so it can be restored after the reconnect — but capture it
        # only *once*, and hold it across retries. The teardown below stops the services, so
        # a second attempt (after the first failed because the device wasn't back yet) would
        # otherwise read the now-idle flags and restore nothing. The intent is cleared only
        # after a reconnect actually succeeds.
        if self._resume_intent is None:
            self._resume_intent = (
                self._events is not None and self._events.active,
                self._monitor is not None and self._monitor.active,
                self._chat is not None and self._chat.active,
            )
        resume_events, resume_monitor, resume_chat = self._resume_intent

        # Release the stale hub/service subscriptions and discard the dead device. The
        # subscriptions are in-process (to the hub), so they survive the link drop and must
        # be torn down explicitly before a fresh connection is opened underneath them.
        if self._monitor is not None:
            await self._monitor.stop()
        if self._chat is not None:
            await self._chat.stop()
        if self._events is not None:
            await self._events.stop()
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:  # noqa: BLE001 - the link is already gone; best-effort
                pass
            self._device = None

        # Open a fresh connection (raises if the device still can't be reached, leaving the
        # remembered intent in place for the next attempt), then restart whatever was running
        # before it dropped and clear the intent now that we're back.
        await self.device()
        if resume_events:
            await self.events.start()
        if resume_monitor:
            await self.monitor.start()
        if resume_chat:
            await self.chat.start()
        self._resume_intent = None

    async def _node_name(self) -> str:
        """Return the connected device's own mesh node name, or ``""`` if unavailable."""
        try:
            info = await self._device.get_self_info()
        except Exception:  # noqa: BLE001 - identity probe is best-effort, never fatal
            return ""
        return str(info.get("adv_name") or info.get("name") or "")

    async def _hardware_model(self) -> str:
        """Return the connected device's firmware model string, or ``""`` if unavailable.

        Sourced from the device-query frame — the only place the model is exposed — so a
        reconnect keeps the remembered hardware column fresh. Best-effort: a probe failure or
        older firmware just leaves any previously-remembered model untouched.
        """
        try:
            info = await self._device.get_device_info()
        except Exception:  # noqa: BLE001 - model lookup is best-effort, never fatal
            return ""
        return str(info.get("model") or "")

    async def aclose(self) -> None:
        """Stop monitoring and chat, stop the event hub, disconnect, and close the repo."""
        if self._monitor is not None:
            await self._monitor.aclose()
        if self._chat is not None:
            await self._chat.aclose()
        if self._events is not None:
            await self._events.aclose()
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:  # noqa: BLE001 - a dead/lost link must not crash teardown
                pass
            self._device = None
        self.repo.close()
