"""Tests for graceful handling of a lost device connection.

Covers the exception classifier that distinguishes a dropped serial link from an ordinary
command failure, and :meth:`AppContext.reconnect`, which rebuilds the connection and restores
the services (event hub, passive monitor, chat) that were running before the drop. All run
against the :class:`MockDevice` simulator; no hardware required.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core import connection
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import DeviceCommandError, is_connection_lost
from meshterm.core.device_store import DeviceStore
from meshterm.persistence.repository import Repository
from meshterm.ui import menu


class _SerialException(Exception):
    """A stand-in for ``serial.SerialException`` (matched by type name, not import)."""


class _FakeSerialDevice:
    """A minimal stand-in for a connected serial :class:`Device` in liveness tests.

    Its :meth:`link_present` mirrors the real serial device: it reports whether the port is
    still enumerated by the OS, reading through the (monkeypatched) module function so tests
    can flip presence on and off.
    """

    transport = "serial"

    def __init__(self, port: str = "COM_TEST") -> None:
        self._port = port

    async def link_present(self) -> bool:
        return connection.serial_port_present(self._port)


def test_is_connection_lost_matches_serial_exception() -> None:
    """A pyserial-style read/write failure is classified as a dropped link."""
    assert is_connection_lost(_SerialException("ClearCommError failed (Access is denied.)"))


def test_is_connection_lost_walks_the_exception_chain() -> None:
    """A link error wrapped in a higher-level error is still detected via its cause."""
    try:
        try:
            raise _SerialException("WriteFile failed")
        except Exception as cause:
            raise RuntimeError("device write failed") from cause
    except Exception as exc:
        assert is_connection_lost(exc)


def test_is_connection_lost_matches_os_level_drops() -> None:
    """OS-layer connection teardown and telltale I/O messages count as a lost link."""
    assert is_connection_lost(ConnectionResetError("reset"))
    assert is_connection_lost(OSError("input/output error"))


def test_is_connection_lost_ignores_ordinary_failures() -> None:
    """A transient command timeout or a generic error is not a dropped link."""
    assert not is_connection_lost(DeviceCommandError("the companion didn't respond in time"))
    assert not is_connection_lost(ValueError("bad value"))


# These stand-ins are named to match bleak's real classes, since the classifier matches by
# type name (not import) — see ``_CONNECTION_LOST_TYPES``.
class BleakError(Exception):  # noqa: N818 - mirrors bleak's own (non-Error-suffixed) name
    """A stand-in for ``bleak.exc.BleakError`` (matched by type name, not import)."""


class BleakDeviceNotFoundError(Exception):
    """A stand-in for ``bleak.exc.BleakDeviceNotFoundError``."""


def test_is_connection_lost_matches_ble_errors() -> None:
    """A dropped Bluetooth link is classified as a lost connection, like a serial unplug."""
    assert is_connection_lost(BleakError("gatt operation failed"))  # matched by type name
    assert is_connection_lost(BleakDeviceNotFoundError("AA:BB:CC not found"))
    # The meshcore BLE transport reports link loss via a callback reason string.
    assert is_connection_lost(RuntimeError("ble_transport_lost"))


class BleakGATTProtocolError(Exception):
    """Stand-in for bleak's GATT auth rejection (classifier matches its message, not import)."""


def test_ble_auth_error_is_recognized_and_is_not_a_lost_link() -> None:
    """A PIN/pairing rejection is its own actionable case — never mistaken for a dropped link."""
    exc = BleakGATTProtocolError("(5, 'GATT Protocol Error: Insufficient Authentication')")
    assert connection._is_ble_auth_error(exc)
    assert not is_connection_lost(exc)  # so the session offers a PIN, not a reconnect


def test_ble_auth_error_walks_the_exception_chain() -> None:
    """An auth rejection wrapped by the meshcore transport is still recognized via its cause."""
    try:
        try:
            raise BleakGATTProtocolError("Insufficient Encryption")
        except Exception as cause:
            raise RuntimeError("connect failed") from cause
    except Exception as exc:
        assert connection._is_ble_auth_error(exc)


async def _disable_windows_pairing(dev: "connection.MeshCoreDevice") -> None:
    """Stub out the WinRT ProvidePin step so ``_create_ble`` stays hermetic in tests.

    On a real Windows host ``_pair_ble_windows`` would reach the OS Bluetooth stack (and the
    physical device); pinning it to a no-op reproduces the non-Windows / no-winrt path so the
    auth-translation logic can be exercised without hardware.
    """

    async def _never_pairs(*, force: bool) -> bool:
        return False

    dev._pair_ble_windows = _never_pairs  # type: ignore[method-assign]


async def test_create_ble_translates_auth_error_to_pin_guidance() -> None:
    """A raw GATT auth rejection becomes a DeviceAuthenticationError that names the PIN fix."""

    class _FakeMeshCore:
        @staticmethod
        async def create_ble(**kwargs):
            raise BleakGATTProtocolError("Insufficient Authentication")

    # No PIN supplied → tell the user to pass one. The subclass lets the interactive picker
    # catch "needs a PIN" specifically, while the CLI still catches it as DeviceCommandError.
    dev = connection.MeshCoreDevice(transport="ble", address="00:11:22:33:44:55")
    await _disable_windows_pairing(dev)
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo:
        await dev._create_ble(_FakeMeshCore)
    assert isinstance(excinfo.value, DeviceCommandError)  # so the scripted CLI catches it too
    assert "--ble-pin" in str(excinfo.value)

    # PIN supplied but rejected → say it was wrong, not that none was given.
    dev_pin = connection.MeshCoreDevice(
        transport="ble", address="00:11:22:33:44:55", pin="123456"
    )
    await _disable_windows_pairing(dev_pin)
    with pytest.raises(connection.DeviceAuthenticationError) as excinfo_pin:
        await dev_pin._create_ble(_FakeMeshCore)
    assert "rejected" in str(excinfo_pin.value).lower()


async def test_create_ble_repairs_stale_bond_and_retries_once() -> None:
    """A first auth failure triggers one unpair-and-re-pair, then the retried connect succeeds.

    Models the Windows upgrade case: a leftover unauthenticated "Just Works" bond makes the
    first connect fail even with the right PIN, so ``_pair_ble_windows(force=True)`` clears it
    and the second connect goes through.
    """
    attempts: list[int] = []

    class _FakeMeshCore:
        @staticmethod
        async def create_ble(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise BleakGATTProtocolError("Insufficient Authentication")
            return "connected-client"

    dev = connection.MeshCoreDevice(
        transport="ble", address="00:11:22:33:44:55", pin="123456"
    )
    repairs: list[bool] = []

    async def _pair(*, force: bool) -> bool:
        repairs.append(force)
        return force  # the pre-connect pass (force=False) no-ops; the repair (force=True) works

    dev._pair_ble_windows = _pair  # type: ignore[method-assign]
    result = await dev._create_ble(_FakeMeshCore)
    assert result == "connected-client"
    assert len(attempts) == 2  # failed once, retried once
    assert repairs == [False, True]  # pre-connect attempt, then the healing re-pair


async def test_create_ble_gives_up_after_one_repair() -> None:
    """A wrong PIN that never bonds fails cleanly rather than looping on the repair retry."""

    class _FakeMeshCore:
        @staticmethod
        async def create_ble(**kwargs):
            raise BleakGATTProtocolError("Insufficient Authentication")

    dev = connection.MeshCoreDevice(
        transport="ble", address="00:11:22:33:44:55", pin="000000"
    )
    calls: list[bool] = []

    async def _pair(*, force: bool) -> bool:
        calls.append(force)
        return force  # even the repair "succeeds" so we prove the retry runs exactly once

    dev._pair_ble_windows = _pair  # type: ignore[method-assign]
    with pytest.raises(connection.DeviceAuthenticationError):
        await dev._create_ble(_FakeMeshCore)
    # force=False (pre-connect), then force=True (repair). The repair's retry passes
    # allow_repair=False, so there is no third pairing attempt even though it keeps failing.
    assert calls == [False, True]


async def test_create_ble_retries_a_transient_link_failure(monkeypatch) -> None:
    """A transport-level ConnectionError is retried once, and the scanned BLEDevice rides along.

    Models the common Windows flake: the first link open misses the (slow-advertising)
    peripheral and the meshcore client raises a bare ``ConnectionError``; users learned to
    work around it by re-selecting the device — a manual retry — so the connect retries
    itself before surfacing the failure.
    """
    scanned_device = object()  # the BLEDevice the discovery scan produced
    attempts: list[dict] = []

    class _FakeMeshCore:
        @staticmethod
        async def create_ble(**kwargs):
            attempts.append(kwargs)
            if len(attempts) == 1:
                raise ConnectionError("Failed to connect to device")
            return "connected-client"

    monkeypatch.setattr(connection, "_BLE_CONNECT_RETRY_DELAY_S", 0.0)
    dev = connection.MeshCoreDevice(
        transport="ble", address="AA:BB:CC:DD:EE:FF", ble_device=scanned_device
    )
    await _disable_windows_pairing(dev)
    assert await dev._create_ble(_FakeMeshCore) == "connected-client"
    assert len(attempts) == 2  # failed once, retried once
    # Every attempt connects through the already-discovered BLEDevice, never a bare address.
    assert all(kw["device"] is scanned_device for kw in attempts)


async def test_create_ble_gives_up_after_the_retry(monkeypatch) -> None:
    """A link that never opens surfaces its ConnectionError after the bounded retries."""
    attempts: list[int] = []

    class _FakeMeshCore:
        @staticmethod
        async def create_ble(**kwargs):
            attempts.append(1)
            raise ConnectionError("Failed to connect to device")

    monkeypatch.setattr(connection, "_BLE_CONNECT_RETRY_DELAY_S", 0.0)
    dev = connection.MeshCoreDevice(transport="ble", address="AA:BB:CC:DD:EE:FF")
    await _disable_windows_pairing(dev)
    with pytest.raises(ConnectionError):
        await dev._create_ble(_FakeMeshCore)
    assert len(attempts) == connection._BLE_CONNECT_ATTEMPTS


async def test_create_ble_never_retries_an_auth_rejection() -> None:
    """A PIN/bond rejection is translated on the first attempt — never looped by the retry."""
    attempts: list[int] = []

    class _FakeMeshCore:
        @staticmethod
        async def create_ble(**kwargs):
            attempts.append(1)
            raise BleakGATTProtocolError("Insufficient Authentication")

    dev = connection.MeshCoreDevice(transport="ble", address="00:11:22:33:44:55")
    await _disable_windows_pairing(dev)
    with pytest.raises(connection.DeviceAuthenticationError):
        await dev._create_ble(_FakeMeshCore)
    assert len(attempts) == 1


async def test_pair_ble_windows_noops_without_pin() -> None:
    """Pairing is skipped (no WinRT touched) when no PIN is set — the fast, hermetic path."""
    dev = connection.MeshCoreDevice(transport="ble", address="00:11:22:33:44:55")
    assert await dev._pair_ble_windows(force=False) is False


def test_ble_address_int_parses_macs_and_rejects_others() -> None:
    """A colon/dash MAC becomes a 48-bit int; a non-MAC (e.g. a macOS UUID) yields None."""
    parse = connection.MeshCoreDevice._ble_address_int
    assert parse("00:11:22:33:44:55") == 0xDAC5216B7C7C
    assert parse("da-c5-21-6b-7c-7c") == 0xDAC5216B7C7C
    assert parse("not-a-mac") is None
    assert parse("550e8400-e29b-41d4-a716-446655440000") is None  # CoreBluetooth UUID


async def test_ble_pairing_helpers_noop_for_non_mac_address() -> None:
    """The bond query and unpair short-circuit (never touching WinRT) for a non-MAC address."""
    # A non-MAC address fails the parse before any winrt import, so these stay hermetic on any
    # platform — no real Bluetooth stack is consulted.
    assert await connection.MeshCoreDevice.is_ble_paired("not-a-mac") is False
    assert await connection.MeshCoreDevice.unpair_ble("not-a-mac") is False
    assert await connection.MeshCoreDevice.is_ble_paired("") is False


async def test_can_unpair_only_for_bonded_ble(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quit dialog offers unpair only on a BLE link that Windows actually holds a bond for."""

    async def _is_paired(address: str) -> bool:
        return address == "00:11:22:33:44:55"

    monkeypatch.setattr(
        connection.MeshCoreDevice, "is_ble_paired", staticmethod(_is_paired)
    )
    # Serial: never offered, whatever the address.
    serial_ctx = SimpleNamespace(active_transport="serial", active_address=None)
    assert await menu._can_unpair(serial_ctx) is False
    # BLE but no address to act on: not offered.
    ble_no_addr = SimpleNamespace(active_transport="ble", active_address=None)
    assert await menu._can_unpair(ble_no_addr) is False
    # BLE with a live bond: offered.
    bonded = SimpleNamespace(active_transport="ble", active_address="00:11:22:33:44:55")
    assert await menu._can_unpair(bonded) is True
    # BLE but open (no bond, e.g. the PIN-less companion): not offered.
    open_ble = SimpleNamespace(active_transport="ble", active_address="AA:BB:CC:DD:EE:FF")
    assert await menu._can_unpair(open_ble) is False


async def test_unpair_on_exit_disconnects_before_unpairing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teardown drops the live link first, then forgets the OS bond (order matters)."""
    order: list[str] = []

    class _Dev:
        async def disconnect(self) -> None:
            order.append("disconnect")

    async def _unpair(address: str) -> bool:
        order.append(f"unpair:{address}")
        return True

    monkeypatch.setattr(connection.MeshCoreDevice, "unpair_ble", staticmethod(_unpair))
    ctx = SimpleNamespace(active_address="00:11:22:33:44:55", _device=_Dev())
    await menu._unpair_on_exit(ctx)
    # Disconnect precedes unpair (a bond can't be dropped while in use), and the device handle
    # is released. The device_store is never touched — the remembered record survives.
    assert order == ["disconnect", "unpair:00:11:22:33:44:55"]
    assert ctx._device is None


def _make_ctx(tmp_path: Path) -> AppContext:
    """Build a real, mock-backed application context for reconnect tests."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "disc.db")
    return AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )


async def test_reconnect_rebuilds_device_and_restores_services(tmp_path: Path) -> None:
    """Reconnect opens a fresh connection and resumes the hub, monitor, and chat."""
    ctx = _make_ctx(tmp_path)
    try:
        await ctx.events.start()
        await ctx.monitor.start()  # start recording on the already-running hub
        await ctx.chat.start()
        original = await ctx.device()
        assert ctx.events.active and ctx.monitor.active and ctx.chat.active

        await ctx.reconnect()

        # A brand-new connection replaced the dead one, and everything that was running
        # before the drop is running again.
        rebuilt = await ctx.device()
        assert rebuilt is not original
        assert ctx.events.active
        assert ctx.monitor.active
        assert ctx.chat.active
    finally:
        await ctx.aclose()


async def test_ble_profile_opens_bluetooth_transport(tmp_path: Path, monkeypatch) -> None:
    """A BLE profile makes ``device()`` build a Bluetooth connection by address."""
    from meshterm.core import connection as conn
    from meshterm.core.config import DeviceProfile

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "ble.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        profile=DeviceProfile(name="handheld", transport="ble", address="AA:BB:CC:DD:EE:FF"),
    )

    built: dict = {}

    class _FakeBle:
        transport = "ble"

        def __init__(self, **kw):
            built.update(kw)
            self._port = None
            self._address = kw.get("address")

        async def connect(self):
            pass

        async def get_self_info(self):
            return {"name": "Handheld"}

    def fake_make_device(**kw):
        assert kw["transport"] == "ble"
        return _FakeBle(**kw)

    monkeypatch.setattr(conn, "make_device", fake_make_device)
    # context imported make_device by name, so patch the reference it actually calls.
    import meshterm.context as context_mod

    monkeypatch.setattr(context_mod, "make_device", fake_make_device)
    try:
        device = await ctx.device()
        assert device.transport == "ble"
        assert built["address"] == "AA:BB:CC:DD:EE:FF"
        assert ctx.active_transport == "ble"
        assert ctx.active_port is None  # BLE has no serial port to watch
    finally:
        ctx.repo.close()


async def test_tcp_profile_opens_network_transport(tmp_path: Path, monkeypatch) -> None:
    """A TCP profile makes ``device()`` build a network connection by host:port."""
    from meshterm.core import connection as conn
    from meshterm.core.config import DeviceProfile

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "tcp.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        profile=DeviceProfile(name="wifi", transport="tcp", host="192.168.1.50", tcp_port=5000),
    )

    built: dict = {}

    class _FakeTcp:
        transport = "tcp"

        def __init__(self, **kw):
            built.update(kw)
            self._port = None
            self._address = None
            self.endpoint = f"{kw.get('host')}:{kw.get('tcp_port')}"

        async def connect(self):
            pass

        async def get_self_info(self):
            return {"name": "WifiNode"}

    def fake_make_device(**kw):
        assert kw["transport"] == "tcp"
        return _FakeTcp(**kw)

    monkeypatch.setattr(conn, "make_device", fake_make_device)
    import meshterm.context as context_mod

    monkeypatch.setattr(context_mod, "make_device", fake_make_device)
    try:
        device = await ctx.device()
        assert device.transport == "tcp"
        assert built["host"] == "192.168.1.50" and built["tcp_port"] == 5000
        assert ctx.active_transport == "tcp"
        assert ctx.active_endpoint == "192.168.1.50:5000"
        assert ctx.active_port is None and ctx.active_address is None
    finally:
        ctx.repo.close()


async def test_reconnect_leaves_idle_services_idle(tmp_path: Path) -> None:
    """Reconnect only restores what was live: idle services stay idle afterward."""
    ctx = _make_ctx(tmp_path)
    try:
        original = await ctx.device()  # a tool opened the radio lazily; nothing subscribed

        await ctx.reconnect()

        rebuilt = await ctx.device()
        assert rebuilt is not original
        assert not ctx.events.active
        assert not ctx.monitor.active
        assert not ctx.chat.active
    finally:
        await ctx.aclose()


async def test_reconnect_restores_services_after_failed_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    """Resume intent survives retries: services restore even if early reconnects fail.

    Reproduces the real-hardware bug where the first (failed) reconnect attempt tore the
    services down, so later attempts read the now-idle flags and restored nothing. The
    mock never fails ``device()``, so we force the first two ``device()`` calls to raise —
    as an absent port does — before letting the third (and the restore calls) succeed.
    """
    ctx = _make_ctx(tmp_path)
    try:
        await ctx.events.start()
        await ctx.monitor.start()
        await ctx.chat.start()
        await ctx.device()
        assert ctx.events.active and ctx.monitor.active and ctx.chat.active

        real_device = AppContext.device
        attempts = {"n": 0}

        async def flaky_device(self: AppContext):
            attempts["n"] += 1
            if attempts["n"] <= 2:  # the device isn't back yet on the first two tries
                raise _SerialException("could not open port 'COM_TEST'")
            return await real_device(self)

        monkeypatch.setattr(AppContext, "device", flaky_device)

        # Two failed reconnects (device still absent), then a success — like a slow replug.
        for _ in range(2):
            try:
                await ctx.reconnect()
            except _SerialException:
                pass
        await ctx.reconnect()  # the third device() call succeeds; services restore

        assert ctx.events.active
        assert ctx.monitor.active
        assert ctx.chat.active
    finally:
        monkeypatch.undo()
        await ctx.aclose()


def test_serial_port_present_reflects_os_enumeration(monkeypatch) -> None:
    """A port is 'present' iff it appears in the OS enumeration; the primary unplug signal."""
    pytest.importorskip("serial")
    from serial.tools import list_ports

    monkeypatch.setattr(
        list_ports, "comports", lambda: [SimpleNamespace(device="COM11")]
    )
    assert connection.serial_port_present("COM11")
    assert not connection.serial_port_present("COM99")


def test_serial_port_present_assumes_up_on_enumeration_error(monkeypatch) -> None:
    """A failed port query must never fake a disconnect — it reports 'present'."""
    pytest.importorskip("serial")
    from serial.tools import list_ports

    def boom():
        raise OSError("enumeration failed")

    monkeypatch.setattr(list_ports, "comports", boom)
    assert connection.serial_port_present("COM11")


def test_serial_port_present_platform_uart_by_path(monkeypatch) -> None:
    """A soldered platform UART (e.g. ``/dev/ttyS1`` on the Luckfox Lyra) is invisible to
    pyserial's ``comports()`` but its ``/dev`` char-device node persists — so an existing
    ``/dev`` character device reads as present, while a vanished node (a real USB unplug of
    ``/dev/ttyUSB*``) still reads as absent."""
    pytest.importorskip("serial")
    import os as _os
    import stat as _stat

    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", list)  # platform UARTs aren't enumerated -> []
    monkeypatch.setattr(_os.path, "exists", lambda p: p == "/dev/ttyS1")
    monkeypatch.setattr(_os, "stat", lambda p: SimpleNamespace(st_mode=_stat.S_IFCHR))

    assert connection.serial_port_present("/dev/ttyS1")  # existing char device -> present
    assert not connection.serial_port_present("/dev/ttyUSB9")  # node gone -> absent (unplug)


async def test_wait_for_disconnect_fires_when_port_vanishes(
    tmp_path: Path, monkeypatch
) -> None:
    """The liveness watcher resolves once the connected device's port leaves enumeration."""
    ctx = _make_ctx(tmp_path)
    # Pose as a live real-hardware session on COM_TEST (the mock can't be unplugged).
    ctx.mock = False
    ctx._device = _FakeSerialDevice("COM_TEST")  # connected device; is_connected -> True
    ctx._active_transport = "serial"
    ctx._active_port = "COM_TEST"

    checks = {"n": 0}

    def fake_present(port: str) -> bool:
        checks["n"] += 1
        return checks["n"] < 2  # present on the first poll, gone thereafter

    monkeypatch.setattr(connection, "serial_port_present", fake_present)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "_LIVENESS_CONFIRM_S", 0.0)
    try:
        await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=2.0)
    finally:
        ctx.repo.close()


async def test_wait_for_disconnect_ignores_the_simulator(tmp_path: Path) -> None:
    """The watcher never fires for --mock: the simulator has no port to lose."""
    ctx = _make_ctx(tmp_path)  # mock=True
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=0.2)
    finally:
        ctx.repo.close()


class _FakeBleDevice:
    """A minimal stand-in for a connected BLE :class:`Device` in liveness tests.

    Its :meth:`link_present` reads an ``is_connected`` flag, mirroring how the real BLE
    device reads the meshcore client's connection state.
    """

    transport = "ble"

    def __init__(self) -> None:
        self.is_connected = True

    async def link_present(self) -> bool:
        return self.is_connected


async def test_wait_for_disconnect_fires_when_ble_link_drops(
    tmp_path: Path, monkeypatch
) -> None:
    """The same watcher fires for BLE once the peripheral's connection flag flips false."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    device = _FakeBleDevice()
    ctx._device = device
    ctx._active_transport = "ble"
    ctx._active_address = "AA:BB:CC:DD:EE:FF"

    async def drop_soon() -> None:
        device.is_connected = False  # the peripheral goes out of range

    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "_LIVENESS_CONFIRM_S", 0.0)
    try:
        await drop_soon()
        await asyncio.wait_for(menu._wait_for_disconnect(ctx), timeout=2.0)
    finally:
        ctx.repo.close()


class _FakeSession:
    """A minimal stand-in for :class:`TuiSession` recording pushes/pops for dialog tests."""

    def __init__(self) -> None:
        self.stack: list = []

    def push(self, screen) -> None:
        self.stack.append(screen)

    def pop(self, screen=None) -> None:
        if self.stack:
            self.stack.pop()

    def invalidate(self) -> None:
        pass


async def test_handle_disconnect_auto_reconnects_when_port_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """The popup dismisses itself (no keypress) once the device's port re-appears."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()  # a connected stand-in
    ctx._active_port = "COM_TEST"

    polls = {"n": 0}

    def fake_present(port: str) -> bool:
        polls["n"] += 1
        return polls["n"] >= 2  # absent on the first poll, back thereafter

    async def fake_reconnect(self: AppContext) -> None:
        self._device = object()  # a fresh connection

    monkeypatch.setattr(connection, "serial_port_present", fake_present)
    monkeypatch.setattr(AppContext, "reconnect", fake_reconnect)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()
    try:
        quit_chosen = await asyncio.wait_for(
            menu._handle_disconnect(ctx, session), timeout=2.0
        )
        assert quit_chosen is False  # reconnected, not quit
        assert session.stack == []  # the popup was cleaned up
    finally:
        ctx.repo.close()


async def test_handle_disconnect_quits_when_user_presses_quit(
    tmp_path: Path, monkeypatch
) -> None:
    """Pressing Enter (the Abort button) leaves, even while the device is still gone."""
    ctx = _make_ctx(tmp_path)
    ctx.mock = False
    ctx._device = object()
    ctx._active_port = "COM_TEST"

    # The port never comes back, so the only way out is the Quit button.
    monkeypatch.setattr(connection, "serial_port_present", lambda port: False)
    monkeypatch.setattr(menu, "_LIVENESS_POLL_S", 0.0)
    monkeypatch.setattr(menu, "spinner_interval", lambda: 0.0)

    session = _FakeSession()

    async def press_quit() -> None:
        # Once the dialog is on the stack, deliver Enter to its Abort button.
        while not session.stack:
            await asyncio.sleep(0)
        session.stack[-1].handle("enter")

    try:
        _, quit_chosen = await asyncio.wait_for(
            asyncio.gather(press_quit(), menu._handle_disconnect(ctx, session)),
            timeout=2.0,
        )
        assert quit_chosen is True
        assert session.stack == []
    finally:
        ctx.repo.close()


async def test_reconnect_dialog_aborts_on_enter_and_ignores_escape() -> None:
    """Enter aborts (resolves ``"quit"``); Esc is inert so a stray keypress can't drop us."""
    from meshterm.ui.tui import ReconnectDialog

    dialog = ReconnectDialog("Waiting…")
    dialog.future = asyncio.get_running_loop().create_future()

    dialog.handle("escape")
    assert not dialog.future.done()  # Esc does nothing

    dialog.handle("enter")
    assert dialog.future.result() == "quit"


async def _never() -> None:
    """An awaitable that blocks forever (a stand-in for an idle worker/watcher)."""
    await asyncio.Event().wait()


async def test_session_loop_quits_without_touching_the_disconnect_path(monkeypatch) -> None:
    """When the menu loop returns (user quit), the watcher is stopped and no dialog shows."""
    handled = {"n": 0}

    async def fake_menu(ctx, session):
        return None  # user quit at the menu

    async def fake_handle(ctx, session):
        handled["n"] += 1
        return True

    monkeypatch.setattr(menu, "_menu_loop", fake_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", lambda ctx: _never())
    monkeypatch.setattr(menu, "_handle_disconnect", fake_handle)

    session = SimpleNamespace(reset=lambda: None)
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert handled["n"] == 0  # the disconnect path was never entered


async def test_session_loop_cancels_worker_and_prompts_on_disconnect(monkeypatch) -> None:
    """A disconnect while the menu is busy cancels the worker, clears the stack, and prompts."""
    resets = {"n": 0}
    cancelled = {"seen": False}

    async def busy_menu(ctx, session):
        try:
            await asyncio.Event().wait()  # a tool is mid-flight; it must be cancelled
        except asyncio.CancelledError:
            cancelled["seen"] = True
            raise

    async def fires_now(ctx):
        return None  # the port vanished

    async def quit_at_dialog(ctx, session):
        return True

    monkeypatch.setattr(menu, "_menu_loop", busy_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", fires_now)
    monkeypatch.setattr(menu, "_handle_disconnect", quit_at_dialog)

    session = SimpleNamespace(reset=lambda: resets.__setitem__("n", resets["n"] + 1))
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert cancelled["seen"]  # the in-flight worker was cancelled
    assert resets["n"] == 1  # the stack was unwound before the dialog


async def test_session_loop_resumes_a_fresh_menu_after_reconnect(monkeypatch) -> None:
    """After a reconnect the loop starts a new menu (and re-arms the watcher)."""
    state = {"menu": 0, "watch": 0, "handle": 0}

    async def flaky_menu(ctx, session):
        state["menu"] += 1
        if state["menu"] == 1:
            await asyncio.Event().wait()  # first pass: interrupted by the disconnect
        return None  # second pass: user quits

    async def watch(ctx):
        state["watch"] += 1
        if state["watch"] == 1:
            return None  # fire once
        await asyncio.Event().wait()  # never fire again

    async def reconnected(ctx, session):
        state["handle"] += 1
        return False  # the device came back

    monkeypatch.setattr(menu, "_menu_loop", flaky_menu)
    monkeypatch.setattr(menu, "_wait_for_disconnect", watch)
    monkeypatch.setattr(menu, "_handle_disconnect", reconnected)

    session = SimpleNamespace(reset=lambda: None)
    await asyncio.wait_for(menu._session_loop(object(), session), timeout=2)
    assert state["handle"] == 1  # one disconnect handled
    assert state["menu"] == 2  # a fresh menu ran after reconnect


class _WedgedMeshCore:
    """A fake meshcore client whose graceful ``disconnect()`` never returns.

    Reproduces the library's dispatcher-stop deadlock (``queue.join()`` with events still
    queued after the processor task exited), which used to hang MeshTerm's exit until the
    watchdog force-killed the process. Records whether the forced path ran.
    """

    def __init__(self) -> None:
        self.force_stopped = False
        self.connection_manager = SimpleNamespace(connection=self)
        self.raw_closed = False

    async def disconnect(self) -> None:
        # Called both as the graceful teardown (via the manager-less attribute lookup on
        # the client) and as the raw transport close. The graceful call wedges; the raw
        # close is distinguished by the force-stop having run first.
        if not self.force_stopped:
            await asyncio.Event().wait()  # the dispatcher deadlock: never returns
        self.raw_closed = True

    def stop(self) -> None:
        self.force_stopped = True


async def test_disconnect_bounds_a_wedged_client_teardown(monkeypatch) -> None:
    """A deadlocked graceful teardown is abandoned and the transport force-closed instead."""
    monkeypatch.setattr(connection, "_DISCONNECT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(connection, "_FORCE_DISCONNECT_TIMEOUT_S", 0.5)

    dev = connection.MeshCoreDevice(port="COM_TEST")
    wedged = _WedgedMeshCore()
    dev._mc = wedged

    await asyncio.wait_for(dev.disconnect(), timeout=2.0)  # must not hang

    assert wedged.force_stopped  # the dispatcher task was cancelled synchronously
    assert wedged.raw_closed  # the port/link was still released
    assert dev._mc is None  # idempotent: a second disconnect is a no-op


async def test_disconnect_graceful_path_needs_no_force(monkeypatch) -> None:
    """A healthy teardown completes gracefully; the forced path is never entered."""

    class _HealthyMeshCore:
        def __init__(self) -> None:
            self.disconnected = False
            self.force_stopped = False
            self.connection_manager = SimpleNamespace(connection=self)

        async def disconnect(self) -> None:
            self.disconnected = True

        def stop(self) -> None:
            self.force_stopped = True

    dev = connection.MeshCoreDevice(port="COM_TEST")
    healthy = _HealthyMeshCore()
    dev._mc = healthy

    await dev.disconnect()

    assert healthy.disconnected
    assert not healthy.force_stopped
    assert dev._mc is None
