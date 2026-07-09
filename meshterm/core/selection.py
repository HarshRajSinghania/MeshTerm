"""Non-interactive device selection: turn discovery + memory into a chosen port.

This is the logic used on the scripted CLI path (and as the fallback when the interactive
picker is unavailable). It is pure and UI-free: it takes the currently discovered devices,
the remembered default, and any explicit overrides, and returns a :class:`Resolution` — or
raises :class:`DeviceSelectionError` with a ready-to-print, user-facing message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .config import DeviceProfile
from .device_store import RememberedDevice
from .discovery import TRANSPORT_BLE, TRANSPORT_SERIAL, DiscoveredDevice


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
        source: Where the choice came from (``"port"``, ``"ble"``, ``"profile"``,
            ``"remembered"``, or ``"only"``), for logging and messaging.
        transport: ``"serial"`` or ``"ble"`` — which connection layer opens the device.
    """

    port: str
    device: Optional[DiscoveredDevice]
    source: str
    transport: str = TRANSPORT_SERIAL

    @property
    def target(self) -> str:
        """The connection target (serial port or BLE address); alias for :attr:`port`."""
        return self.port


def _find_by_target(
    devices: list[DiscoveredDevice], target: str
) -> Optional[DiscoveredDevice]:
    """Return the discovered device whose port or BLE address equals ``target``."""
    return next((d for d in devices if d.target == target or d.port == target), None)


def _format_device_list(devices: list[DiscoveredDevice]) -> str:
    """Render discovered devices as an indented, human-readable bullet list."""
    if not devices:
        return "  (no serial devices detected)"
    lines = []
    for d in devices:
        flag = " [likely LoRa]" if d.is_likely_lora else ""
        kind = "BLE" if d.is_ble else "serial"
        lines.append(
            f"  • {d.target} ({kind}) — {d.product or d.description or 'device'}{flag}"
        )
    return "\n".join(lines)


def resolve_device(
    devices: list[DiscoveredDevice],
    remembered: Optional[RememberedDevice],
    *,
    explicit_port: Optional[str] = None,
    explicit_ble: Optional[str] = None,
    profile: Optional[DeviceProfile] = None,
) -> Resolution:
    """Decide which companion to connect to without prompting.

    Resolution priority:

    1. ``explicit_ble`` (an explicit ``--ble`` Bluetooth address).
    2. ``explicit_port`` (an explicit ``--port``).
    3. ``profile.port`` (an explicitly chosen profile with a port).
    4. The remembered "last known good" device, if it is currently attached/in range.
    5. The single attached device, if exactly one is present.

    Otherwise a :class:`DeviceSelectionError` is raised listing the candidates.

    Args:
        devices: Currently discovered devices (serial and/or BLE).
        remembered: The remembered default, if any.
        explicit_port: A serial port supplied on the command line.
        explicit_ble: A Bluetooth address supplied on the command line.
        profile: A device profile supplied on the command line.

    Returns:
        A :class:`Resolution` naming the chosen target and transport.

    Raises:
        DeviceSelectionError: If no device can be chosen unambiguously.
    """
    if explicit_ble:
        return Resolution(
            explicit_ble, _find_by_target(devices, explicit_ble), "ble", TRANSPORT_BLE
        )

    if explicit_port:
        match = _find_by_target(devices, explicit_port)
        return Resolution(explicit_port, match, "port", TRANSPORT_SERIAL)

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
