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
from typing import Callable, Optional

from .models import Contact, Hop, Observation, TraceResult

#: Callback invoked with each :class:`Observation` as passive monitoring captures it.
ObservationCallback = Callable[[Observation], None]

TX_POWER_MIN = 1
TX_POWER_MAX = 22

#: Default range explored when tuning a *remote* repeater's transmit power. Remote nodes
#: (e.g. high-gain repeaters) typically run hotter than the local companion, so this band
#: differs from the local ``TX_POWER_MIN``/``TX_POWER_MAX`` clamp. Both bounds are
#: user-configurable (see :class:`~meshtools.core.config.Settings`).
REMOTE_TX_MIN = 12
REMOTE_TX_MAX = 28


class DeviceCommandError(RuntimeError):
    """A device command failed in a recoverable, user-facing way.

    Raised for conditions worth reporting cleanly (no traceback) — chiefly the
    companion's intermittent failure to answer a query in time. Callers may retry.
    """


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

    # -- remote administration (tuning a node we have admin rights on) -----------

    @abstractmethod
    async def admin_login(self, node: Contact, password: str) -> bool:
        """Authenticate as administrator on a remote node.

        Args:
            node: The contact to log in to. Its ``public_key`` addresses the node.
            password: The node's admin password.

        Returns:
            ``True`` if the node accepted the login, ``False`` otherwise (e.g. a wrong
            password or no response).
        """

    @abstractmethod
    async def get_remote_tx_power(self, node: Contact) -> Optional[int]:
        """Read a remote (admin) node's current transmit power.

        Args:
            node: The contact to query (must already be logged in).

        Returns:
            The node's TX power in dBm, or ``None`` if it could not be read.
        """

    @abstractmethod
    async def set_remote_tx_power(self, node: Contact, value: int) -> None:
        """Set a remote (admin) node's transmit power.

        Args:
            node: The contact to adjust (must already be logged in).
            value: TX power in dBm.

        Raises:
            DeviceCommandError: If the node rejected the command.
        """

    @abstractmethod
    async def run_trace(
        self,
        target: str,
        *,
        path: Optional[str] = None,
        timeout: float = 10.0,
    ) -> TraceResult:
        """Run a single path trace to ``target`` and return per-hop SNR.

        Args:
            target: Name or key prefix of the destination node.
            path: Optional explicit path to force, as a comma-separated string of
                single-byte hex key prefixes (e.g. ``"3d,f2,3d"``). When ``None``
                the device chooses the path.
            timeout: Seconds to wait for the trace reply.

        Returns:
            A :class:`TraceResult`; ``success`` is ``False`` on timeout.
        """

    # -- passive monitoring ------------------------------------------------------

    @abstractmethod
    async def listen(
        self,
        duration_s: float,
        *,
        on_observation: Optional["ObservationCallback"] = None,
    ) -> list[Observation]:
        """Passively capture packets the companion overhears for ``duration_s`` seconds.

        Adverts (and, where the firmware surfaces them, telemetry frames) heard on the
        air are returned as :class:`~meshtools.core.models.Observation` records with
        whatever SNR/RSSI/location the radio reported. The radio is not asked to transmit;
        it simply listens.

        Args:
            duration_s: How long to listen, in seconds.
            on_observation: Optional callback invoked with each observation as it arrives
                (e.g. to advance a live progress display).

        Returns:
            Every observation captured during the window, in arrival order.
        """

    # -- configuration: extra reads ---------------------------------------------

    @abstractmethod
    async def get_tuning(self) -> dict:
        """Return radio tuning parameters.

        Returns:
            A dict with ``rx_delay`` and ``airtime_factor`` (both ints).
        """

    @abstractmethod
    async def get_path_hash_mode(self) -> int:
        """Return the current path-hash mode (experimental routing flag)."""

    @abstractmethod
    async def get_custom_vars(self) -> dict[str, str]:
        """Return the device's experimental custom key/value variables."""

    @abstractmethod
    async def get_channel(self, index: int) -> Optional[dict]:
        """Return one channel's configuration, or ``None`` if unset.

        Args:
            index: Zero-based channel slot.

        Returns:
            A dict with ``channel_idx``, ``channel_name`` and ``channel_secret``
            (16 raw bytes), or ``None`` when the slot is empty.
        """

    # -- configuration: settable values -----------------------------------------

    @abstractmethod
    async def set_name(self, name: str) -> None:
        """Set the node's advertised name."""

    @abstractmethod
    async def set_coords(self, lat: float, lon: float) -> None:
        """Set the node's advertised latitude/longitude (decimal degrees)."""

    @abstractmethod
    async def set_device_pin(self, pin: int) -> None:
        """Set the device's BLE pairing PIN."""

    @abstractmethod
    async def set_radio(self, freq: float, bw: float, sf: int, cr: int) -> None:
        """Set the core radio parameters.

        Args:
            freq: Frequency in MHz.
            bw: Bandwidth in kHz.
            sf: Spreading factor.
            cr: Coding rate denominator (``5``-``8`` for 4/5-4/8).
        """

    @abstractmethod
    async def set_tuning(self, rx_delay: int, airtime_factor: int) -> None:
        """Set radio tuning parameters (RX delay and airtime budgeting factor)."""

    @abstractmethod
    async def set_manual_add_contacts(self, enabled: bool) -> None:
        """Set whether contacts must be added manually rather than automatically."""

    @abstractmethod
    async def set_adv_loc_policy(self, policy: int) -> None:
        """Set the advert location-sharing policy."""

    @abstractmethod
    async def set_multi_acks(self, value: int) -> None:
        """Set the multi-ack behavior flag."""

    @abstractmethod
    async def set_telemetry_modes(self, base: int, loc: int, env: int) -> None:
        """Set the three telemetry mode fields together (each ``0``-``3``)."""

    @abstractmethod
    async def set_path_hash_mode(self, mode: int) -> None:
        """Set the experimental path-hash routing mode."""

    @abstractmethod
    async def set_custom_var(self, key: str, value: str) -> None:
        """Set an experimental custom variable."""

    @abstractmethod
    async def set_channel(self, index: int, name: str, secret: Optional[bytes]) -> None:
        """Configure a channel slot.

        Args:
            index: Zero-based channel slot.
            name: Channel name (a leading ``#`` derives the secret from the name).
            secret: 16-byte shared secret, or ``None`` to derive it from ``name``.
        """

    # -- configuration: actions -------------------------------------------------

    @abstractmethod
    async def set_time(self, epoch: int) -> None:
        """Set the device clock to a UNIX epoch timestamp."""

    @abstractmethod
    async def send_advert(self, flood: bool = False) -> None:
        """Broadcast an advertisement (``flood`` propagates it across the mesh)."""

    @abstractmethod
    async def reboot(self) -> None:
        """Reboot the device."""

    @abstractmethod
    async def export_private_key(self) -> str:
        """Export the device's private key as a hex string (sensitive)."""

    @abstractmethod
    async def import_private_key(self, key_hex: str) -> None:
        """Import a private key from a hex string (overwrites the device identity)."""

    @abstractmethod
    async def factory_reset(self) -> None:
        """Erase all device data and restore factory defaults (destructive)."""

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

    async def _contacts_payload(
        self, mc, *, retries: int = 3, delay: float = 0.5
    ) -> dict:  # noqa: ANN001
        """Fetch the raw contacts map, retrying the transient "no event" blip.

        The companion intermittently fails to emit the contacts event in time and
        returns an error event instead of data (``no event received during contacts
        retrieval``). That's a recoverable timing hiccup, so retry a few times with a
        short backoff before surfacing a clean, actionable error.

        Args:
            mc: The connected ``MeshCore`` client.
            retries: Number of extra attempts after the first.
            delay: Seconds to wait between attempts.

        Returns:
            The contacts payload mapping (possibly empty).

        Raises:
            DeviceCommandError: If every attempt fails to retrieve contacts.
        """
        reason = ""
        for attempt in range(retries + 1):
            event = await mc.commands.get_contacts()
            if event is None or not getattr(event, "is_error", lambda: False)():
                return dict(getattr(event, "payload", {}) or {})
            payload = getattr(event, "payload", {}) or {}
            reason = str(payload.get("reason", payload))
            if attempt < retries:
                await asyncio.sleep(delay)
        raise DeviceCommandError(
            f"could not read contacts from the radio ({reason}). "
            "The companion didn't respond in time — this is usually transient; "
            "retry, or power-cycle/reconnect the radio if it persists."
        )

    async def get_contacts(self) -> list[Contact]:  # noqa: D102 - inherited docstring
        mc = self._require()
        payload = await self._contacts_payload(mc)
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

    @staticmethod
    def _node_pubkey(node: Contact) -> str:
        """Return a contact's full public key for remote addressing.

        Args:
            node: The contact to address.

        Returns:
            The lowercased hex public key (``0x`` stripped).

        Raises:
            DeviceCommandError: If the contact carries no public key, so it cannot be
                addressed for login/admin commands.
        """
        pub = (node.public_key or "").lower().removeprefix("0x")
        if not pub:
            raise DeviceCommandError(
                f"contact {node.name!r} has no public key on this device, so it can't be "
                "logged in to for admin commands. Receive an advert from it first."
            )
        return pub

    async def admin_login(self, node: Contact, password: str) -> bool:  # noqa: D102
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(node)
        event = await mc.commands.send_login_sync(pub, password)
        # ``send_login_sync`` returns the LOGIN_SUCCESS event, or ``None``/an ERROR or
        # LOGIN_FAILED event when the node refused (typically a wrong password).
        if event is None:
            return False
        etype = getattr(event, "type", None)
        if etype in (EventType.ERROR, EventType.LOGIN_FAILED):
            return False
        return True

    async def _send_admin_cmd(self, node: Contact, cmd: str, *, timeout: float = 8.0):
        """Send a CLI command to a logged-in remote node and await its reply.

        The companion acknowledges the send immediately (``MSG_SENT``); the node's
        textual reply arrives later as a ``CONTACT_MSG_RECV`` event. We return that
        reply text (or ``None`` if none arrived before ``timeout``).

        Args:
            node: The remote contact (must already be logged in).
            cmd: The repeater CLI command, e.g. ``"set tx 20"``.
            timeout: Seconds to wait for the node's reply.

        Returns:
            The reply text, or ``None`` if the node did not answer in time.

        Raises:
            DeviceCommandError: If the companion rejected the send outright.
        """
        from meshcore import EventType

        mc = self._require()
        pub = self._node_pubkey(node)
        sent = await mc.commands.send_cmd(pub, cmd)
        if sent is not None and getattr(sent, "is_error", lambda: False)():
            raise DeviceCommandError(
                f"failed to send admin command {cmd!r} to {node.name!r}: "
                f"{getattr(sent, 'payload', {})}"
            )
        reply = await mc.wait_for_event(EventType.CONTACT_MSG_RECV, timeout=timeout)
        if reply is None:
            return None
        payload = getattr(reply, "payload", {}) or {}
        return str(payload.get("text", payload.get("msg", "")))

    async def get_remote_tx_power(self, node: Contact) -> Optional[int]:  # noqa: D102
        reply = await self._send_admin_cmd(node, "get tx")
        return _parse_tx_reply(reply)

    async def set_remote_tx_power(self, node: Contact, value: int) -> None:  # noqa: D102
        # The reply ("ok"/echoed value) is best-effort confirmation; absence isn't fatal
        # since some firmware answers tersely or drops the ack under duty-cycle limits.
        await self._send_admin_cmd(node, f"set tx {value}")

    async def run_trace(  # noqa: D102 - inherited docstring
        self,
        target: str,
        *,
        path: Optional[str] = None,
        timeout: float = 10.0,
    ) -> TraceResult:
        from meshcore import EventType  # local import keeps mock path dependency-free

        from ..services.trace_runner import path_hash_flags

        mc = self._require()
        tag = random.randint(0, 0xFFFFFFFF)
        started = asyncio.get_event_loop().time()

        # A trace packet has no destination field — it walks an explicit path of
        # repeater hops. Send the path as raw bytes so any uniform hash width
        # transmits; ``flags`` carries the path-hash mode (size - 1).
        path_bytes: Optional[bytes] = None
        flags = 0
        if path:
            hops = [h.strip() for h in path.split(",") if h.strip()]
            path_bytes = bytes.fromhex("".join(hops))
            flags = path_hash_flags(len(bytes.fromhex(hops[0]))) or 0
        else:
            # No forced path: build a path that ends at the target's own hash
            # (prepending any learned ``out_path`` repeaters), since a trace only
            # replies when its destination is the final hop. ``None`` only when the
            # contact is unknown, leaving the trace to run path-less.
            resolved = await self._trace_path_to_contact(mc, target)
            if resolved is not None:
                path_bytes, flags = resolved
        await mc.commands.send_trace(auth_code=0, tag=tag, flags=flags, path=path_bytes)
        event = await mc.wait_for_event(
            EventType.TRACE_DATA,
            attribute_filters={"tag": tag},
            timeout=timeout,
        )
        elapsed_ms = (asyncio.get_event_loop().time() - started) * 1000.0
        # The firmware addresses each hop by a hash of ``1 << flags`` bytes; record it
        # so the summary can show node hashes at the width the command actually used.
        hash_bytes = 1 << flags
        if event is None:
            return TraceResult(
                target=target, success=False, round_trip_ms=None, path_hash_bytes=hash_bytes
            )

        payload = getattr(event, "payload", {}) or {}
        hops = parse_trace_hops(payload)
        return TraceResult(
            target=target,
            success=True,
            hops=hops,
            round_trip_ms=elapsed_ms,
            tx_power=await self.get_tx_power(),
            path_hash_bytes=hash_bytes,
            raw=payload,
        )

    async def _trace_path_to_contact(
        self, mc, target: str
    ) -> Optional[tuple[bytes, int]]:  # noqa: ANN001
        """Resolve a target contact into a trace ``(path_bytes, flags)``.

        A trace reply only comes back when the *destination's own hash* is the final
        hop in the path — an empty/destination-less path is silently dropped (verified
        on hardware: a direct neighbor answers a single-hop trace to its own hash but
        not a path-less one). So we always end the path at the contact's key prefix,
        prepending any learned repeater hops (``out_path``) ahead of it:

        * direct neighbor / no learned route → just ``[destination]``;
        * learned multi-hop route → ``[repeater…, destination]``.

        Each hash is re-encoded at the trace's own width (``1 << flags``, only 1/2/4/8
        bytes), collapsing a region's routing width (e.g. 3) to the widest representable
        value (2). The firmware matches by hash prefix, so a narrower prefix still
        addresses the same node.

        Args:
            mc: The connected ``MeshCore`` client.
            target: Contact name (case-insensitive) or public-key prefix.

        Returns:
            ``(path_bytes, flags)`` to walk, or ``None`` only when the contact is
            unknown or carries no public key to address.
        """
        from ..services.trace_runner import path_hash_flags

        payload = await self._contacts_payload(mc)
        needle = target.casefold()
        for name, info in payload.items():
            info = info or {}
            adv = info.get("adv_name", name)
            pub = (info.get("public_key", "") or "").lower().removeprefix("0x")
            if adv.casefold() != needle and not pub.startswith(needle):
                continue
            if not pub:
                return None  # no key to address the trace's destination hop

            # Routing hash width (bytes): the contact's stored mode, or — when it has
            # no learned route (mode == -1) — our region's mode.
            mode = int(info.get("out_path_hash_mode", -1))
            if mode < 0:
                try:
                    mode = int(await mc.commands.get_path_hash_mode())
                except Exception:
                    mode = 2  # region default: 3-byte hashes
            size = max(mode + 1, 1)
            trace_size = max(s for s in (1, 2, 4, 8) if s <= size)

            hops: list[bytes] = []
            out_path = (info.get("out_path") or "").strip().lower().removeprefix("0x")
            out_path_len = int(info.get("out_path_len", -1))
            if 1 <= out_path_len <= 254 and out_path:
                # Learned multi-hop route: walk each repeater, collapsed to trace width.
                route = bytes.fromhex(out_path)[: out_path_len * size]
                hops += [route[i * size : i * size + trace_size] for i in range(out_path_len)]
            # Always finish at the destination's own hash so the target recognizes the
            # trace and replies; for a direct neighbor this single hop is the whole path.
            hops.append(bytes.fromhex(pub)[:trace_size])
            return b"".join(hops), path_hash_flags(trace_size) or 0
        return None

    async def listen(  # noqa: D102 - inherited docstring
        self,
        duration_s: float,
        *,
        on_observation: Optional[ObservationCallback] = None,
    ) -> list[Observation]:
        from meshcore import EventType

        mc = self._require()
        subscribe = getattr(mc, "subscribe", None)
        if subscribe is None:  # pragma: no cover - depends on installed meshcore build
            raise DeviceCommandError(
                "this meshcore build doesn't expose event subscription, so passive "
                "monitoring isn't available. Upgrade the 'meshcore' library."
            )

        observations: list[Observation] = []

        def make_handler(kind: str):  # type: ignore[no-untyped-def]
            def handler(event) -> None:  # noqa: ANN001
                obs = observation_from_event(event, kind)
                if obs is None:
                    return
                observations.append(obs)
                if on_observation is not None:
                    on_observation(obs)

            return handler

        # Subscribe to whichever advert/telemetry event types this firmware/library build
        # exposes. The event payload field names below are best-effort and, like the trace
        # mapping, should be validated against your firmware's event schema.
        subs = []
        for attr, kind in (
            ("ADVERTISEMENT", "advert"),
            ("ADVERT", "advert"),
            ("NEW_CONTACT", "advert"),
            ("TELEMETRY_RESPONSE", "telemetry"),
        ):
            etype = getattr(EventType, attr, None)
            if etype is not None:
                subs.append(subscribe(etype, make_handler(kind)))
        try:
            await asyncio.sleep(duration_s)
        finally:
            for sub in subs:
                unsub = getattr(sub, "unsubscribe", None)
                if unsub is not None:
                    try:
                        unsub()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
        return observations

    @staticmethod
    def _ok(event):  # type: ignore[no-untyped-def]
        """Return ``event`` if it succeeded, else raise its error payload.

        Args:
            event: The :class:`meshcore.events.Event` returned by a command.

        Returns:
            The same event, for convenient chaining.

        Raises:
            RuntimeError: If the device reported an error.
        """
        if event is not None and getattr(event, "is_error", lambda: False)():
            raise RuntimeError(f"device rejected command: {getattr(event, 'payload', {})}")
        return event

    async def get_tuning(self) -> dict:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.get_tuning())
        payload = getattr(event, "payload", {}) or {}
        return {
            "rx_delay": int(payload.get("rx_delay", 0)),
            "airtime_factor": int(payload.get("airtime_factor", 0)),
        }

    async def get_path_hash_mode(self) -> int:  # noqa: D102 - inherited docstring
        return int(await self._require().commands.get_path_hash_mode())

    async def get_custom_vars(self) -> dict[str, str]:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.get_custom_vars())
        return dict(getattr(event, "payload", {}) or {})

    async def get_channel(self, index: int) -> Optional[dict]:  # noqa: D102
        event = self._ok(await self._require().commands.get_channel(index))
        payload = getattr(event, "payload", {}) or {}
        if not payload.get("channel_name"):
            return None
        return payload

    async def set_name(self, name: str) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_name(name))

    async def set_coords(self, lat: float, lon: float) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_coords(lat, lon))

    async def set_device_pin(self, pin: int) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_devicepin(pin))

    async def set_radio(self, freq: float, bw: float, sf: int, cr: int) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_radio(freq, bw, sf, cr))

    async def set_tuning(self, rx_delay: int, airtime_factor: int) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_tuning(rx_delay, airtime_factor))

    async def set_manual_add_contacts(self, enabled: bool) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_manual_add_contacts(enabled))

    async def set_adv_loc_policy(self, policy: int) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_advert_loc_policy(policy))

    async def set_multi_acks(self, value: int) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_multi_acks(value))

    async def set_telemetry_modes(self, base: int, loc: int, env: int) -> None:  # noqa: D102
        mc = self._require()
        result = self._ok(await mc.commands.send_appstart())
        infos = dict(getattr(result, "payload", {}) or {})
        infos["telemetry_mode_base"] = base
        infos["telemetry_mode_loc"] = loc
        infos["telemetry_mode_env"] = env
        self._ok(await mc.commands.set_other_params_from_infos(infos))

    async def set_path_hash_mode(self, mode: int) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_path_hash_mode(mode))

    async def set_custom_var(self, key: str, value: str) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_custom_var(key, value))

    async def set_channel(self, index: int, name: str, secret: Optional[bytes]) -> None:  # noqa: D102
        self._ok(await self._require().commands.set_channel(index, name, secret))

    async def set_time(self, epoch: int) -> None:  # noqa: D102 - inherited docstring
        self._ok(await self._require().commands.set_time(epoch))

    async def send_advert(self, flood: bool = False) -> None:  # noqa: D102
        self._ok(await self._require().commands.send_advert(flood))

    async def reboot(self) -> None:  # noqa: D102 - inherited docstring
        await self._require().commands.reboot()  # device reboots; no OK reply expected

    async def export_private_key(self) -> str:  # noqa: D102 - inherited docstring
        event = self._ok(await self._require().commands.export_private_key())
        payload = getattr(event, "payload", {}) or {}
        key = payload.get("private_key")
        if key is None:
            raise RuntimeError("device did not return a private key (export may be disabled)")
        return key.hex() if isinstance(key, (bytes, bytearray)) else str(key)

    async def import_private_key(self, key_hex: str) -> None:  # noqa: D102
        self._ok(await self._require().commands.import_private_key(bytes.fromhex(key_hex)))

    async def factory_reset(self) -> None:  # noqa: D102 - inherited docstring
        mc = self._require()
        token = await mc.commands.request_factory_reset()
        self._ok(await mc.commands.confirm_factory_reset(token))


class MockDevice(Device):
    """A deterministic simulator implementing the full :class:`Device` interface.

    The simulated SNR follows an inverted-U response to TX power: too low and the signal
    sits in the noise floor, too high and the receiver saturates. This gives the TX
    optimizer a realistic, unimodal-with-noise curve to converge on without hardware.

    Attributes:
        optimal_tx: The TX power at which the simulated link peaks.
    """

    def __init__(
        self,
        seed: int = 1234,
        optimal_tx: int = 14,
        optimal_remote_tx: int = 20,
        admin_password: str = "admin",
    ) -> None:
        """Initialize the simulator.

        Args:
            seed: RNG seed for reproducible measurement noise.
            optimal_tx: Local TX power at which the simulated *bottleneck* SNR peaks.
            optimal_remote_tx: Remote-node TX power at which the simulated SNR *at the
                target* peaks (what the remote-admin optimizer converges on).
            admin_password: Password the simulated remote nodes accept for admin login.
        """
        self.optimal_tx = optimal_tx
        self.optimal_remote_tx = optimal_remote_tx
        self._admin_password = admin_password
        self._rng = random.Random(seed)
        self._tx_power = 20
        self._connected = False
        self._contacts = [
            Contact(name="Yagi-Repeater", public_key=_mock_pub("a1b2c3d4"), key_prefix="a1b2c3d4"),
            Contact(name="Local-Repeater", public_key=_mock_pub("b2c3d4e5"), key_prefix="b2c3d4e5"),
            Contact(name="Observer-Bot", public_key=_mock_pub("c3d4e5f6"), key_prefix="c3d4e5f6"),
            Contact(name="Alice", public_key=_mock_pub("d4e5f6a7"), key_prefix="d4e5f6a7"),
        ]
        # Remote-admin simulation: which nodes we're "logged in" to, and each tuned
        # node's transmit power keyed by full public key. ``_default_remote_tx`` is the
        # assumed power before the optimizer first writes one.
        self._admin_sessions: set[str] = set()
        self._remote_tx: dict[str, int] = {}
        self._default_remote_tx = 20
        # Mutable configuration state, keyed exactly like the real SELF_INFO payload so the
        # settings registry behaves identically on the simulator and on hardware.
        self._info: dict = {
            "name": "MockCompanion",
            "adv_type": 1,
            "max_tx_power": 22,
            "public_key": "00" * 32,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
            "multi_acks": 0,
            "adv_loc_policy": 0,
            "telemetry_mode_base": 0,
            "telemetry_mode_loc": 0,
            "telemetry_mode_env": 0,
            "manual_add_contacts": False,
            "radio_freq": 869.525,
            "radio_bw": 250.0,
            "radio_sf": 11,
            "radio_cr": 5,
            "simulated": True,
        }
        self._tuning: dict = {"rx_delay": 0, "airtime_factor": 0}
        self._path_hash_mode = 0
        self._custom_vars: dict[str, str] = {}
        self._channels: dict[int, dict] = {}
        self._device_pin = 0
        self._private_key = "11" * 32

    async def connect(self) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)
        self._connected = True

    async def disconnect(self) -> None:  # noqa: D102 - inherited docstring
        self._connected = False

    async def get_self_info(self) -> dict:  # noqa: D102 - inherited docstring
        return {**self._info, "tx_power": self._tx_power}

    async def get_contacts(self) -> list[Contact]:  # noqa: D102 - inherited docstring
        return list(self._contacts)

    async def get_tx_power(self) -> Optional[int]:  # noqa: D102 - inherited docstring
        return self._tx_power

    async def set_tx_power(self, value: int) -> None:  # noqa: D102 - inherited docstring
        self._tx_power = value

    async def admin_login(self, node: Contact, password: str) -> bool:  # noqa: D102
        await asyncio.sleep(0)
        if password != self._admin_password:
            return False
        self._admin_sessions.add(self._mock_key(node))
        return True

    async def get_remote_tx_power(self, node: Contact) -> Optional[int]:  # noqa: D102
        key = self._mock_key(node)
        if key not in self._admin_sessions:
            return None
        return self._remote_tx.get(key, self._default_remote_tx)

    async def set_remote_tx_power(self, node: Contact, value: int) -> None:  # noqa: D102
        key = self._mock_key(node)
        if key not in self._admin_sessions:
            raise DeviceCommandError(
                f"not logged in to {node.name!r}; call admin_login first."
            )
        self._remote_tx[key] = value

    @staticmethod
    def _mock_key(node: Contact) -> str:
        """Return the lookup key for a remote node (its public key, else key prefix)."""
        return (node.public_key or node.key_prefix or node.name).lower().removeprefix("0x")

    def _remote_tx_for(self, hop_hex: str) -> Optional[int]:
        """Resolve the simulated remote TX power set on a forced-path hop, if any.

        The optimizer stores a node's power under its full public key; a trace addresses
        it by a shorter hash prefix, so match in either direction.

        Args:
            hop_hex: The forced-path hop hash (hex).

        Returns:
            The node's simulated TX power, or ``None`` if we never set one (i.e. this
            hop isn't a node the optimizer is tuning).
        """
        h = hop_hex.lower()
        for key, tx in self._remote_tx.items():
            if key.startswith(h) or h.startswith(key):
                return tx
        return None

    async def get_tuning(self) -> dict:  # noqa: D102 - inherited docstring
        return dict(self._tuning)

    async def get_path_hash_mode(self) -> int:  # noqa: D102 - inherited docstring
        return self._path_hash_mode

    async def get_custom_vars(self) -> dict[str, str]:  # noqa: D102 - inherited docstring
        return dict(self._custom_vars)

    async def get_channel(self, index: int) -> Optional[dict]:  # noqa: D102
        return self._channels.get(index)

    async def set_name(self, name: str) -> None:  # noqa: D102 - inherited docstring
        self._info["name"] = name

    async def set_coords(self, lat: float, lon: float) -> None:  # noqa: D102
        self._info["adv_lat"] = lat
        self._info["adv_lon"] = lon

    async def set_device_pin(self, pin: int) -> None:  # noqa: D102 - inherited docstring
        self._device_pin = pin

    async def set_radio(self, freq: float, bw: float, sf: int, cr: int) -> None:  # noqa: D102
        self._info.update(radio_freq=freq, radio_bw=bw, radio_sf=sf, radio_cr=cr)

    async def set_tuning(self, rx_delay: int, airtime_factor: int) -> None:  # noqa: D102
        self._tuning = {"rx_delay": rx_delay, "airtime_factor": airtime_factor}

    async def set_manual_add_contacts(self, enabled: bool) -> None:  # noqa: D102
        self._info["manual_add_contacts"] = enabled

    async def set_adv_loc_policy(self, policy: int) -> None:  # noqa: D102
        self._info["adv_loc_policy"] = policy

    async def set_multi_acks(self, value: int) -> None:  # noqa: D102 - inherited docstring
        self._info["multi_acks"] = value

    async def set_telemetry_modes(self, base: int, loc: int, env: int) -> None:  # noqa: D102
        self._info.update(
            telemetry_mode_base=base, telemetry_mode_loc=loc, telemetry_mode_env=env
        )

    async def set_path_hash_mode(self, mode: int) -> None:  # noqa: D102
        self._path_hash_mode = mode

    async def set_custom_var(self, key: str, value: str) -> None:  # noqa: D102
        self._custom_vars[key] = value

    async def set_channel(self, index: int, name: str, secret: Optional[bytes]) -> None:  # noqa: D102
        self._channels[index] = {
            "channel_idx": index,
            "channel_name": name,
            "channel_secret": secret or (b"\x00" * 16),
        }

    async def set_time(self, epoch: int) -> None:  # noqa: D102 - inherited docstring
        self._info["clock"] = epoch

    async def send_advert(self, flood: bool = False) -> None:  # noqa: D102
        await asyncio.sleep(0)

    async def reboot(self) -> None:  # noqa: D102 - inherited docstring
        await asyncio.sleep(0)

    async def export_private_key(self) -> str:  # noqa: D102 - inherited docstring
        return self._private_key

    async def import_private_key(self, key_hex: str) -> None:  # noqa: D102
        self._private_key = bytes.fromhex(key_hex).hex()  # validates hex, normalizes

    async def factory_reset(self) -> None:  # noqa: D102 - inherited docstring
        self._custom_vars.clear()
        self._channels.clear()

    async def listen(  # noqa: D102 - inherited docstring
        self,
        duration_s: float,
        *,
        on_observation: Optional[ObservationCallback] = None,
    ) -> list[Observation]:
        # Synthesize plausible adverts from the known contacts, cycling through them.
        # Two of them ("…Repeater") advertise a fixed location so location-aware features
        # (e.g. the coverage map) have data. The wall-clock duration is honored only
        # loosely — the simulator emits a count proportional to it and paces minimally so
        # tests stay fast.
        repeats = max(1, round(duration_s / 5.0))
        locations = {
            "Yagi-Repeater": (45.5019, -73.5674),
            "Local-Repeater": (45.4768, -73.5990),
        }
        observations: list[Observation] = []
        for r in range(repeats):
            for c in self._contacts:
                kind = "telemetry" if (r + len(observations)) % 4 == 3 else "advert"
                lat_lon = locations.get(c.name)
                obs = Observation(
                    node=c.key_prefix or c.public_key[:12],
                    name=c.name,
                    kind=kind,
                    snr=round(self._rng.gauss(6.0, 3.0), 1),
                    rssi=round(self._rng.gauss(-95.0, 8.0), 1),
                    lat=lat_lon[0] if lat_lon else None,
                    lon=lat_lon[1] if lat_lon else None,
                )
                observations.append(obs)
                if on_observation is not None:
                    on_observation(obs)
                await asyncio.sleep(0)
        return observations

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

    def _expected_remote_snr(self, remote_tx: int) -> float:
        """Model the SNR the target receives from the admin node it sits behind.

        An inverted-U in the admin node's TX power: too low and the target barely hears
        it, too high and the target's front end saturates. The peak sits at
        ``optimal_remote_tx`` so the remote-admin optimizer has a unimodal-with-noise
        curve to converge on.

        Args:
            remote_tx: The admin node's transmit power.

        Returns:
            The noise-free expected SNR in dB at the target.
        """
        # Curvature is steep enough that the band edges fall below the ~-12 dB drop
        # threshold, so traces start failing there — giving the optimizer a real
        # reliability gradient (not just an SNR one) to honor reliability-first.
        span = (REMOTE_TX_MAX - REMOTE_TX_MIN) / 2
        offset = (remote_tx - self.optimal_remote_tx) / span
        return 9.0 - 24.0 * (offset**2)

    async def run_trace(  # noqa: D102 - inherited docstring
        self,
        target: str,
        *,
        path: Optional[str] = None,
        timeout: float = 10.0,
    ) -> TraceResult:
        await asyncio.sleep(0.05)  # mimic radio latency so progress bars are visible
        forced = [h for h in path.split(",") if h.strip()] if path else None
        # Mirror the real device: the path-hash width is the byte length of a forced hop.
        hash_bytes = len(bytes.fromhex(forced[0])) if forced else None
        depth = len(forced) if forced else self._rng.randint(1, 3)
        hops: list[Hop] = []
        for i in range(depth):
            # Each hop's SNR reflects the node that transmitted *into* it (hop i-1). If
            # that node is one the optimizer has tuned a remote TX on, model the link from
            # its power — so the hop arriving at the target tracks the admin node we're
            # tuning, wherever the target sits in a there-and-back path. Otherwise fall
            # back to the local TX-power model.
            prev_tx = self._remote_tx_for(forced[i - 1]) if forced is not None and i >= 1 else None
            if prev_tx is not None:
                expected = self._expected_remote_snr(prev_tx)
            else:
                expected = self._expected_snr(i)
            snr = expected + self._rng.gauss(0, 1.2)  # measurement noise
            node = forced[i] if forced else f"hop{i}"
            hops.append(Hop(index=i, node=node, snr=round(snr, 1)))

        # Very weak links occasionally drop the whole trace, judged on the bottleneck hop.
        bottleneck = min((h.snr for h in hops), default=-99)
        success = bottleneck > -12 or self._rng.random() > 0.1

        # The trace reply returns to us: firmware appends the local device as a final
        # hash-less hop (``node=None``). Mirror that so the origin/destination framing
        # naturally has our device at both ends of the path. The return link is modeled
        # as symmetric to the first outbound hop, so it never alters the bottleneck SNR.
        if hops:
            hops.append(Hop(index=depth, node=None, snr=hops[0].snr))
        return TraceResult(
            target=target,
            success=success,
            hops=hops if success else [],
            round_trip_ms=round(self._rng.uniform(120, 480), 1) if success else None,
            tx_power=self._tx_power,
            path_hash_bytes=hash_bytes,
        )


def parse_trace_hops(payload: dict) -> list[Hop]:
    """Extract per-hop SNR from a ``TRACE_DATA`` event payload.

    meshcore parses a trace reply into ``payload["path"]`` — a list of nodes, each a
    dict with a repeater ``"hash"`` and its ``"snr"`` (already in dB, signed-byte / 4).
    The final node is the local device and carries an ``"snr"`` but no ``"hash"``.

    Args:
        payload: The ``TRACE_DATA`` event payload.

    Returns:
        The hops in path order; nodes without an SNR reading are skipped.
    """
    hops: list[Hop] = []
    for node in payload.get("path") or []:
        if not isinstance(node, dict) or node.get("snr") is None:
            continue
        hops.append(Hop(index=len(hops), node=node.get("hash"), snr=float(node["snr"])))
    return hops


def observation_from_event(event, kind: str) -> Optional[Observation]:  # noqa: ANN001
    """Map a meshcore advert/telemetry event into an :class:`Observation`.

    The companion reports a node identifier, optionally a name and shared location, and
    the SNR/RSSI of the reception. Field names vary across firmware and library versions,
    so several common spellings are tried for each value. Like the trace mapping this is
    best-effort and should be validated against your firmware's event schema.

    Args:
        event: A meshcore event (anything exposing a ``payload`` mapping).
        kind: The observation class to tag the record with (e.g. ``advert``).

    Returns:
        The parsed :class:`Observation`, or ``None`` if the payload carried no node id.
    """
    payload = dict(getattr(event, "payload", {}) or {})
    node = (
        payload.get("public_key")
        or payload.get("pubkey")
        or payload.get("hash")
        or payload.get("key_prefix")
    )
    if not node:
        return None
    node = str(node).lower().removeprefix("0x")[:12]
    lat = payload.get("adv_lat", payload.get("lat"))
    lon = payload.get("adv_lon", payload.get("lon"))
    return Observation(
        node=node,
        name=payload.get("adv_name") or payload.get("name"),
        kind=kind,
        snr=_as_float(payload.get("snr")),
        rssi=_as_float(payload.get("rssi")),
        lat=_as_float(lat) if lat else None,
        lon=_as_float(lon) if lon else None,
        raw=payload,
    )


def _as_float(value: object) -> Optional[float]:
    """Best-effort float conversion, returning ``None`` on missing/garbage values."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _mock_pub(prefix: str) -> str:
    """Build a 32-byte mock public key from a short hex prefix (simulator only).

    Args:
        prefix: Leading hex digits identifying the node.

    Returns:
        A 64-hex-character (32-byte) key beginning with ``prefix``.
    """
    return prefix + "0" * (64 - len(prefix))


def _parse_tx_reply(reply: Optional[str]) -> Optional[int]:
    """Extract a TX-power integer from a repeater's ``get tx`` reply text.

    Repeater firmware answers tersely and inconsistently across versions (e.g.
    ``"tx: 20"``, ``"TX power = 20 dBm"``, or just ``"20"``), so pull the first signed
    integer out of the reply rather than matching a fixed format.

    Args:
        reply: The node's reply text, or ``None`` if it did not answer.

    Returns:
        The parsed TX power, or ``None`` if the reply was empty or carried no number.
    """
    if not reply:
        return None
    import re

    match = re.search(r"-?\d+", reply)
    return int(match.group()) if match else None


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
