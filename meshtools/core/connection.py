"""Device connection abstraction.

Services and tools depend only on the :class:`Device` interface, never on the
``meshcore`` library directly. This keeps the algorithms testable and lets the
:class:`MockDevice` simulator stand in for real hardware during development.

Two implementations are provided:

* :class:`MeshCoreDevice` - wraps the async ``meshcore`` companion-protocol client.
* :class:`MockDevice` - a deterministic simulator with a physically plausible
  SNR-vs-TX-power response, used by ``--mock`` and the test suite.
"""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from typing import Optional

from .models import Contact, Hop, TraceResult, utcnow

TX_POWER_MIN = 1
TX_POWER_MAX = 22


class Device(ABC):
    """Abstract companion device exposing the operations MeshTools needs.

    Implementations manage their own connection lifecycle and translate raw protocol
    events into the domain models in :mod:`meshtools.core.models`.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection to the device. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection and release resources. Idempotent."""

    @abstractmethod
    async def get_self_info(self) -> dict:
        """Return identity and radio configuration of the connected device.

        Returns:
            A dict with at least ``name`` and, when available, ``tx_power`` and radio
            parameters (``freq``, ``bw``, ``sf``, ``cr``).
        """

    @abstractmethod
    async def get_contacts(self) -> list[Contact]:
        """Return the device's known contacts.

        Returns:
            The list of :class:`Contact` records currently stored on the device.
        """

    @abstractmethod
    async def get_tx_power(self) -> Optional[int]:
        """Return the current TX power level, or ``None`` if unknown."""

    @abstractmethod
    async def set_tx_power(self, value: int) -> None:
        """Set the radio transmit power.

        Args:
            value: TX power level, clamped by the caller to the device's valid range.
        """

    @abstractmethod
    async def run_trace(
        self,
        target: str,
        *,
        path: Optional[list[str]] = None,
        timeout: float = 10.0,
    ) -> TraceResult:
        """Run a single path trace to ``target`` and return per-hop SNR.

        Args:
            target: Name or key prefix of the destination node.
            path: Optional explicit path to force (list of node identifiers). When
                ``None`` the device chooses the path.
            timeout: Seconds to wait for the trace reply.

        Returns:
            A :class:`TraceResult`; ``success`` is ``False`` on timeout.
        """

    async def __aenter__(self) -> "Device":
        """Enter the async context manager, connecting the device."""
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the async context manager, disconnecting the device."""
        await self.disconnect()


class MeshCoreDevice(Device):
    """A :class:`Device` backed by the ``meshcore`` serial companion client.

    Note:
        The trace mapping here follows the documented companion protocol (trace replies
        carry per-hop SNR encoded as ``SNR * 4``) but should be validated against your
        firmware version, as event payload field names vary between releases.
    """

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        """Initialize the device wrapper.

        Args:
            port: Serial port path (e.g. ``COM5`` or ``/dev/ttyUSB0``).
            baudrate: Serial baud rate.
        """
        self._port = port
        self._baudrate = baudrate
        self._mc = None  # type: ignore[var-annotated]  # meshcore.MeshCore

    async def connect(self) -> None:  # noqa: D102 - inherited docstring
        if self._mc is not None:
            return
        try:
            from meshcore import MeshCore  # lazy import so --mock needs no hardware deps
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise RuntimeError(
                "The 'meshcore' library is required to talk to real hardware but is not "
                "installed. Run `pip install -e .` (or `pip install meshcore`), or use "
                "--mock for the simulator."
            ) from exc

        self._mc = await MeshCore.create_serial(self._port, self._baudrate)

    async def disconnect(self) -> None:  # noqa: D102 - inherited docstring
        if self._mc is None:
            return
        disconnect = getattr(self._mc, "disconnect", None)
        if disconnect is not None:
            await disconnect()
        self._mc = None

    def _require(self):  # type: ignore[no-untyped-def]
        """Return the live client or raise if not connected."""
        if self._mc is None:
            raise RuntimeError("Device is not connected; call connect() first.")
        return self._mc

    async def get_self_info(self) -> dict:  # noqa: D102 - inherited docstring
        mc = self._require()
        result = await mc.commands.send_appstart()
        return dict(getattr(result, "payload", {}) or {})

    async def get_contacts(self) -> list[Contact]:  # noqa: D102 - inherited docstring
        mc = self._require()
        result = await mc.commands.get_contacts()
        payload = getattr(result, "payload", {}) or {}
        contacts: list[Contact] = []
        for name, info in payload.items():
            info = info or {}
            contacts.append(
                Contact(
                    name=info.get("adv_name", name),
                    public_key=info.get("public_key", ""),
                    key_prefix=info.get("public_key", "")[:12],
                )
            )
        return contacts

    async def get_tx_power(self) -> Optional[int]:  # noqa: D102 - inherited docstring
        info = await self.get_self_info()
        value = info.get("tx_power")
        return int(value) if value is not None else None

    async def set_tx_power(self, value: int) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        await mc.commands.set_tx_power(value)

    async def run_trace(  # noqa: D102 - inherited docstring
        self,
        target: str,
        *,
        path: Optional[list[str]] = None,
        timeout: float = 10.0,
    ) -> TraceResult:
        from meshcore import EventType  # local import keeps mock path dependency-free

        mc = self._require()
        tag = random.randint(0, 0xFFFFFFFF)
        started = asyncio.get_event_loop().time()
        await mc.commands.send_trace(auth_code=0, tag=tag, flags=0, path=path)
        event = await mc.wait_for_event(
            EventType.TRACE_DATA,
            attribute_filters={"tag": tag},
            timeout=timeout,
        )
        elapsed_ms = (asyncio.get_event_loop().time() - started) * 1000.0
        if event is None:
            return TraceResult(target=target, success=False, round_trip_ms=None)

        payload = getattr(event, "payload", {}) or {}
        raw_snrs = payload.get("path_snrs") or payload.get("snrs") or []
        nodes = payload.get("path") or []
        hops = [
            Hop(
                index=i,
                node=str(nodes[i]) if i < len(nodes) else None,
                snr=raw / 4.0,  # protocol encodes SNR as a signed byte of SNR * 4
            )
            for i, raw in enumerate(raw_snrs)
        ]
        return TraceResult(
            target=target,
            success=True,
            hops=hops,
            round_trip_ms=elapsed_ms,
            tx_power=await self.get_tx_power(),
            raw=payload,
        )


class MockDevice(Device):
    """A deterministic simulator implementing the full :class:`Device` interface.

    The simulated SNR follows an inverted-U response to TX power: too low and the signal
    sits in the noise floor, too high and the receiver saturates. This gives the TX
    optimizer a realistic, unimodal-with-noise curve to converge on without hardware.

    Attributes:
        optimal_tx: The TX power at which the simulated link peaks.
    """

    def __init__(self, seed: int = 1234, optimal_tx: int = 14) -> None:
        """Initialize the simulator.

        Args:
            seed: RNG seed for reproducible measurement noise.
            optimal_tx: TX power level at which simulated SNR is maximized.
        """
        self.optimal_tx = optimal_tx
        self._rng = random.Random(seed)
        self._tx_power = 20
        self._connected = False
        self._contacts = [
            Contact(name="Yagi-Repeater", key_prefix="a1b2c3d4"),
            Contact(name="Local-Repeater", key_prefix="b2c3d4e5"),
            Contact(name="Observer-Bot", key_prefix="c3d4e5f6"),
            Contact(name="Alice", key_prefix="d4e5f6a7"),
        ]

    async def connect(self) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)
        self._connected = True

    async def disconnect(self) -> None:  # noqa: D102 - inherited docstring
        self._connected = False

    async def get_self_info(self) -> dict:  # noqa: D102 - inherited docstring
        return {
            "name": "MockCompanion",
            "tx_power": self._tx_power,
            "freq": 869.525,
            "bw": 250,
            "sf": 11,
            "cr": 5,
            "simulated": True,
        }

    async def get_contacts(self) -> list[Contact]:  # noqa: D102 - inherited docstring
        return list(self._contacts)

    async def get_tx_power(self) -> Optional[int]:  # noqa: D102 - inherited docstring
        return self._tx_power

    async def set_tx_power(self, value: int) -> None:  # noqa: D102 - inherited docstring
        self._tx_power = value

    def _expected_snr(self, hop_index: int) -> float:
        """Model SNR for a hop as an inverted-U in TX power plus distance falloff.

        Args:
            hop_index: Zero-based hop position; deeper hops are weaker.

        Returns:
            The noise-free expected SNR in dB for the current TX power.
        """
        # Inverted parabola peaking at ``optimal_tx``; ~10 dB swing across the range.
        span = (TX_POWER_MAX - TX_POWER_MIN) / 2
        offset = (self._tx_power - self.optimal_tx) / span
        peak = 8.0 - 10.0 * (offset**2)
        return peak - 2.5 * hop_index

    async def run_trace(  # noqa: D102 - inherited docstring
        self,
        target: str,
        *,
        path: Optional[list[str]] = None,
        timeout: float = 10.0,
    ) -> TraceResult:
        await asyncio.sleep(0.05)  # mimic radio latency so progress bars are visible
        depth = len(path) if path else self._rng.randint(1, 3)
        hops: list[Hop] = []
        for i in range(depth):
            expected = self._expected_snr(i)
            snr = expected + self._rng.gauss(0, 1.2)  # measurement noise
            node = path[i] if path else f"hop{i}"
            hops.append(Hop(index=i, node=node, snr=round(snr, 1)))

        # Very weak links occasionally drop entirely.
        success = (hops[-1].snr if hops else -99) > -12 or self._rng.random() > 0.1
        return TraceResult(
            target=target,
            success=success,
            hops=hops if success else [],
            round_trip_ms=round(self._rng.uniform(120, 480), 1) if success else None,
            tx_power=self._tx_power,
        )


def clamp_tx_power(value: int) -> int:
    """Clamp a TX power value to the supported range.

    Args:
        value: Requested TX power level.

    Returns:
        ``value`` constrained to ``[TX_POWER_MIN, TX_POWER_MAX]``.
    """
    return max(TX_POWER_MIN, min(TX_POWER_MAX, value))


def make_device(
    *,
    mock: bool,
    port: Optional[str],
    baudrate: int = 115200,
    mock_optimal_tx: int = 14,
) -> Device:
    """Construct the appropriate :class:`Device` for the current invocation.

    Args:
        mock: When ``True`` return a :class:`MockDevice` simulator.
        port: Serial port for a real device. Required unless ``mock`` is set.
        baudrate: Serial baud rate for a real device.
        mock_optimal_tx: Peak TX power for the simulator.

    Returns:
        A connected-on-enter :class:`Device` instance.

    Raises:
        ValueError: If a real device is requested without a serial port.
    """
    if mock:
        return MockDevice(optimal_tx=mock_optimal_tx)
    if not port:
        raise ValueError(
            "No serial port configured. Pass --port, set a profile, or use --mock."
        )
    return MeshCoreDevice(port=port, baudrate=baudrate)
