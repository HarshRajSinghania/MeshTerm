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
        await ctx.monitor.enable()  # start recording (also starts the hub)
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
        await ctx.monitor.enable()
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


async def test_wait_for_disconnect_fires_when_port_vanishes(
    tmp_path: Path, monkeypatch
) -> None:
    """The liveness watcher resolves once the connected device's port leaves enumeration."""
    ctx = _make_ctx(tmp_path)
    # Pose as a live real-hardware session on COM_TEST (the mock can't be unplugged).
    ctx.mock = False
    ctx._device = object()  # stand-in for a connected device; is_connected -> True
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
    monkeypatch.setattr(menu, "_RECONNECT_SPINNER_S", 0.0)

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
    monkeypatch.setattr(menu, "_RECONNECT_SPINNER_S", 0.0)

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
