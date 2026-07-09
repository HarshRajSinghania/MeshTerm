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
    _active_port: Optional[str] = field(default=None, init=False, repr=False)
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
        """The serial port the current connection is open on, if any (``None`` for --mock).

        Set when a real connection is opened so the liveness watcher knows which OS port to
        poll for a mid-session unplug. Falls back to an explicit ``--port`` override when a
        connection hasn't recorded one yet.
        """
        if self.mock:
            return None
        return self._active_port or self.port_override

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
        # Record the port so the liveness watcher knows which OS port to poll (the picker
        # opened it directly, bypassing the resolution in ``device()`` that normally sets it).
        self._active_port = getattr(device, "_port", None)

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
            self._active_port = None
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
        self._active_port = resolution.port
        await self._device.connect()
        # Connection succeeded: this is now the last known good device. Learn its mesh node
        # name (best-effort — a probe failure must not block a good connection).
        if self.selected_device is not None:
            self.device_store.remember(
                self.selected_device, node_name=await self._node_name()
            )
        return self._device

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
