# SPDX-License-Identifier: Apache-2.0
"""Non-interactive device selection: turn discovery + memory into a chosen port.

This is the logic used on the scripted CLI path (and as the fallback when the interactive
picker is unavailable). It is pure and UI-free: it takes the currently discovered devices,
the remembered default, and any explicit overrides, and returns a :class:`Resolution` — or
raises :class:`DeviceSelectionError` with a ready-to-print, user-facing message.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import DeviceProfile
from .device_store import RememberedDevice
from .discovery import (
    TRANSPORT_BLE,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
    DiscoveredDevice,
    parse_tcp_endpoint,
    tcp_device,
)


class DeviceSelectionError(ValueError):
    """Raised when no device can be chosen unambiguously.

    The message is already formatted for display to the user (it lists the discovered
    devices and how to disambiguate), so callers can print ``str(exc)`` directly.
    """


@dataclass(slots=True)
class Resolution:
    """The outcome of resolving which device to use.

    Attributes:
        port: The chosen connection target — the serial port for a serial device, or the
            Bluetooth address for a BLE device. (Named ``port`` for historical reasons; use
            :attr:`target`.)
        device: The matching discovered device, when enumeration knows it (so the caller
            can remember it on a successful connection). ``None`` for an explicit
            target that is not currently enumerable.
        source: Where the choice came from (``"port"``, ``"ble"``, ``"tcp"``, ``"profile"``,
            ``"remembered"``, or ``"only"``), for logging and messaging.
        transport: ``"serial"``, ``"ble"``, or ``"tcp"`` — the connection layer for the device.
    """

    port: str
    device: DiscoveredDevice | None
    source: str
    transport: str = TRANSPORT_SERIAL

    @property
    def target(self) -> str:
        """The connection target (serial port or BLE address); alias for :attr:`port`."""
        return self.port


def _find_by_target(devices: list[DiscoveredDevice], target: str) -> DiscoveredDevice | None:
    """Return the discovered device whose port or BLE address equals ``target``."""
    return next((d for d in devices if d.target == target or d.port == target), None)


def _resolve_tcp(endpoint: str, devices: list[DiscoveredDevice], source: str) -> Resolution:
    """Build a TCP :class:`Resolution` from a ``host[:port]`` string.

    The endpoint is normalized (a bare host gains the default port) so the resulting target
    matches how a remembered TCP device stores itself. A TCP companion isn't discoverable, so
    any matching ``devices`` entry would only be a remembered one injected by the caller.

    Raises:
        DeviceSelectionError: If ``endpoint`` isn't a valid ``host[:port]``.
    """
    try:
        host, port = parse_tcp_endpoint(endpoint)
    except ValueError as exc:
        raise DeviceSelectionError(str(exc)) from exc
    target = f"{host}:{port}"
    match = _find_by_target(devices, target) or tcp_device(host, port)
    return Resolution(target, match, source, TRANSPORT_TCP)


def _format_device_list(devices: list[DiscoveredDevice]) -> str:
    """Render discovered devices as an indented, human-readable bullet list."""
    if not devices:
        return "  no serial devices detected"
    lines = []
    for d in devices:
        flag = " [likely LoRa]" if d.is_likely_lora else ""
        kind = "TCP" if d.is_tcp else "BLE" if d.is_ble else "serial"
        lines.append(f"  • {d.target} ({kind}) — {d.product or d.description or 'device'}{flag}")
    return "\n".join(lines)


def resolve_device(
    devices: list[DiscoveredDevice],
    remembered: RememberedDevice | None,
    *,
    explicit_port: str | None = None,
    explicit_ble: str | None = None,
    explicit_tcp: str | None = None,
    profile: DeviceProfile | None = None,
) -> Resolution:
    """Decide which companion to connect to without prompting.

    Resolution priority:

    1. ``explicit_tcp`` (an explicit ``--tcp`` network address).
    2. ``explicit_ble`` (an explicit ``--ble`` Bluetooth address).
    3. ``explicit_port`` (an explicit ``--port``).
    4. A TCP profile's ``host:port``, or a serial profile's ``port``.
    5. The remembered "last known good" device, if it is currently attached/in range.
    6. The single attached device, if exactly one is present.

    Otherwise a :class:`DeviceSelectionError` is raised listing the candidates.

    Args:
        devices: Currently discovered devices (serial and/or BLE).
        remembered: The remembered default, if any.
        explicit_port: A serial port supplied on the command line.
        explicit_ble: A Bluetooth address supplied on the command line.
        explicit_tcp: A network ``host[:port]`` supplied on the command line.
        profile: A device profile supplied on the command line.

    Returns:
        A :class:`Resolution` naming the chosen target and transport.

    Raises:
        DeviceSelectionError: If no device can be chosen unambiguously, or an explicit TCP
            endpoint could not be parsed.
    """
    if explicit_tcp:
        return _resolve_tcp(explicit_tcp, devices, "tcp")

    if explicit_ble:
        return Resolution(
            explicit_ble, _find_by_target(devices, explicit_ble), "ble", TRANSPORT_BLE
        )

    if explicit_port:
        match = _find_by_target(devices, explicit_port)
        return Resolution(explicit_port, match, "port", TRANSPORT_SERIAL)

    if profile is not None and profile.is_tcp and profile.tcp_endpoint:
        return _resolve_tcp(profile.tcp_endpoint, devices, "profile")

    if profile is not None and profile.port:
        match = _find_by_target(devices, profile.port)
        return Resolution(profile.port, match, "profile", TRANSPORT_SERIAL)

    if remembered is not None:
        match = next((d for d in devices if remembered.matches(d)), None)
        if match is not None:
            return Resolution(match.target, match, "remembered", match.transport)

    if len(devices) == 1:
        return Resolution(devices[0].target, devices[0], "only", devices[0].transport)

    if not devices:
        raise DeviceSelectionError(
            "No companion devices detected. Connect a companion device (USB or Bluetooth), "
            "or pass --port / --ble explicitly, or use --mock for the simulator."
        )

    raise DeviceSelectionError(
        "Multiple companion devices detected and no default to fall back on.\n"
        f"{_format_device_list(devices)}\n"
        "Choose one with --port <PORT> or --ble <ADDRESS> (or run 'meshterm devices' to "
        "inspect them). The chosen device is remembered as the default after it connects."
    )
