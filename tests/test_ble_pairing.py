"""Tests for the BLE connect path's ownership of its meshcore client.

The bug these pin down: ``MeshCore.create_ble`` builds a client, calls ``connect()`` on it,
and returns it *only on success*. A connect that raises — which is exactly how a
PIN-protected companion answers an unbonded notify-subscribe — therefore left the client,
and the bleak link it had already opened, orphaned inside the library. MeshTerm's own
``disconnect`` was a no-op (``_mc`` was never assigned), Windows held the ACL link for the
life of the process, and the peripheral, still believing it had a peer, stopped advertising.
The PIN dialog that opened next then asked for a code it could no longer deliver.

Everything here runs against fakes standing in for ``meshcore.MeshCore`` and
``meshcore.BLEConnection``; no radio, no address, and no pairing code is involved.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from meshterm.core import connection as conn_mod
from meshterm.core.connection import MeshCoreDevice

# A stand-in address. Deliberately in the documentation-style ``00:`` space so no real
# companion is named here, and nothing in this file needs hardware to run.
_ADDR = "00:11:22:33:44:55"


class _FakeBLEConnection:
    """Stands in for ``meshcore.BLEConnection`` — records how it was constructed."""

    last: Optional["_FakeBLEConnection"] = None

    def __init__(self, address=None, device=None, pin=None) -> None:
        self.address = address
        self.device = device
        self.pin = pin
        _FakeBLEConnection.last = self


class _FakeMeshCore:
    """Stands in for the ``meshcore.MeshCore`` *class*, one instance per connect attempt.

    ``outcome`` drives what ``connect()`` does: ``"ok"`` returns a truthy handshake result,
    ``"none"`` returns ``None`` (transport up, no identity reply), and an exception instance
    is raised — the GATT authentication failure being the case that mattered.
    """

    #: Every instance built during a test, in order (a retry builds a second one).
    built: list["_FakeMeshCore"] = []
    #: What the next ``connect()`` should do; a list is consumed one entry per attempt.
    outcomes: list = []

    def __init__(self, cx, *, default_timeout=None, auto_reconnect=False, **kwargs) -> None:
        self.cx = cx
        self.default_timeout = default_timeout
        self.auto_reconnect = auto_reconnect
        self.connected = False
        self.disconnect_calls = 0
        self.commands = None
        type(self).built.append(self)

    async def connect(self):
        outcome = type(self).outcomes.pop(0)
        if isinstance(outcome, BaseException):
            # The link is up by the time the subscribe fails — that is the whole point:
            # something must close it, and only the holder of this object can.
            self.connected = True
            raise outcome
        if outcome == "slow":
            self.connected = True
            await asyncio.sleep(30)  # a handshake the caller's wait_for will cancel
        self.connected = True
        return None if outcome == "none" else {"ok": True}

    async def disconnect(self):
        self.disconnect_calls += 1
        self.connected = False

    @classmethod
    def reset(cls, *outcomes) -> None:
        cls.built = []
        cls.outcomes = list(outcomes)


@pytest.fixture(autouse=True)
def _fake_meshcore(monkeypatch: pytest.MonkeyPatch):
    """Make ``from meshcore import BLEConnection`` inside the connect path resolve to a fake."""
    import sys
    import types

    module = types.ModuleType("meshcore")
    module.BLEConnection = _FakeBLEConnection
    module.MeshCore = _FakeMeshCore
    monkeypatch.setitem(sys.modules, "meshcore", module)
    _FakeBLEConnection.last = None
    yield


def _device(pin: Optional[str] = None) -> MeshCoreDevice:
    return MeshCoreDevice(transport="ble", address=_ADDR, pin=pin, connect_timeout=5.0)


class _AuthError(Exception):
    """A stand-in for the GATT failure a PIN-protected companion answers with."""


def test_a_raising_connect_closes_the_client_it_leaves_behind() -> None:
    """THE bug: a connect that raises must not strand an open link.

    ``create_ble`` would have swallowed the reference here. Owning the client means the
    failure path can put the link down before the exception continues on its way — so the
    peripheral stops holding a phantom peer and goes on advertising for the PIN retry.
    """
    _FakeMeshCore.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == 1
    client = _FakeMeshCore.built[0]
    assert client.disconnect_calls == 1, "the half-open client was left open"
    assert client.connected is False


def test_an_unanswered_handshake_closes_the_client_too() -> None:
    """Transport up, no identity reply: reported as "not a companion", link still closed."""
    _FakeMeshCore.reset("none")
    dev = _device()

    assert asyncio.run(dev._connect_owned_ble(_FakeMeshCore)) is None
    assert _FakeMeshCore.built[0].disconnect_calls == 1


def test_a_good_connect_hands_the_client_over_still_open() -> None:
    """The success path must not close anything — the caller owns the live client."""
    _FakeMeshCore.reset("ok")
    dev = _device()

    client = asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    assert client is _FakeMeshCore.built[0]
    assert client.disconnect_calls == 0
    assert client.connected is True


def test_the_connection_is_built_from_this_device_s_own_endpoint() -> None:
    """Address, discovered ``BLEDevice`` and PIN all reach the connection we construct."""
    _FakeMeshCore.reset("ok")
    sentinel = object()
    dev = MeshCoreDevice(
        transport="ble", address=_ADDR, pin="000000", ble_device=sentinel,
        connect_timeout=7.5,
    )

    asyncio.run(dev._connect_owned_ble(_FakeMeshCore))

    cx = _FakeBLEConnection.last
    assert (cx.address, cx.device, cx.pin) == (_ADDR, sentinel, "000000")
    client = _FakeMeshCore.built[0]
    # auto_reconnect stays off: MeshTerm drives reconnection itself.
    assert (client.default_timeout, client.auto_reconnect) == (7.5, False)


def test_a_cancelled_handshake_still_closes_the_link() -> None:
    """A probe's ``wait_for`` expiring mid-handshake must not leak what it cancelled.

    This is the second way in, and the reason the teardown is shielded: a plain ``await``
    would itself be cancelled the moment it suspended, abandoning the close.
    """
    _FakeMeshCore.reset("slow")
    dev = _device()

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(dev._connect_owned_ble(_FakeMeshCore), timeout=0.1)
        # The shielded close runs on after the cancellation propagates; give it a tick.
        await asyncio.sleep(0.1)
        return _FakeMeshCore.built[0]

    client = asyncio.run(scenario())
    assert client.disconnect_calls == 1, "a cancelled handshake stranded its link"


def test_a_failing_disconnect_never_masks_the_real_error() -> None:
    """Teardown is best-effort: the connect's own failure is what the caller must see."""

    class _Stubborn(_FakeMeshCore):
        async def disconnect(self):
            self.disconnect_calls += 1
            raise RuntimeError("teardown exploded")

    _Stubborn.reset(_AuthError("Insufficient Authentication"))
    dev = _device()

    with pytest.raises(_AuthError):
        asyncio.run(dev._connect_owned_ble(_Stubborn))
    assert _Stubborn.built[0].disconnect_calls == 1


def test_the_retry_loop_never_reuses_a_torn_down_client() -> None:
    """A retried link-open builds a fresh client, and the abandoned one was closed."""
    _FakeMeshCore.reset(ConnectionError("Failed to connect to device"), "ok")
    dev = _device()

    async def scenario():
        return await dev._create_ble_with_retry(_FakeMeshCore)

    client = asyncio.run(scenario())

    assert len(_FakeMeshCore.built) == 2, "the retry did not build its own client"
    assert _FakeMeshCore.built[0].disconnect_calls == 1, "the failed attempt leaked"
    assert client is _FakeMeshCore.built[1]
    assert client.disconnect_calls == 0


def test_every_link_open_attempt_failing_raises_the_last_error() -> None:
    """Exhausting the retries surfaces the link failure — with nothing left open."""
    _FakeMeshCore.reset(
        ConnectionError("Failed to connect to device"),
        ConnectionError("Failed to connect to device"),
    )
    dev = _device()

    with pytest.raises(ConnectionError):
        asyncio.run(dev._create_ble_with_retry(_FakeMeshCore))

    assert len(_FakeMeshCore.built) == conn_mod._BLE_CONNECT_ATTEMPTS
    assert all(c.disconnect_calls == 1 for c in _FakeMeshCore.built)
