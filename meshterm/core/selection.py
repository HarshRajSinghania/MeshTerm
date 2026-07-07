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
from .discovery import DiscoveredDevice


class DeviceSelectionError(ValueError):
    """Raised when no device can be chosen unambiguously.

    The message is already formatted for display to the user (it lists the discovered
    devices and how to disambiguate), so callers can print ``str(exc)`` directly.
    """


@dataclass(slots=True)
class Resolution:
    """The outcome of resolving which device to use.

    Attributes:
        port: The chosen serial port.
        device: The matching discovered device, when enumeration knows it (so the caller
            can remember it on a successful connection). ``None`` for an explicit
            ``--port``/profile port that is not currently enumerable.
        source: Where the choice came from (``"port"``, ``"profile"``, ``"remembered"``,
            or ``"only"``), for logging and messaging.
    """

    port: str
    device: Optional[DiscoveredDevice]
    source: str


def _find_by_port(devices: list[DiscoveredDevice], port: str) -> Optional[DiscoveredDevice]:
    """Return the discovered device on ``port``, if enumeration found one."""
    return next((d for d in devices if d.port == port), None)


def _format_device_list(devices: list[DiscoveredDevice]) -> str:
    """Render discovered devices as an indented, human-readable bullet list."""
    if not devices:
        return "  (no serial devices detected)"
    lines = []
    for d in devices:
        flag = " [likely LoRa]" if d.is_likely_lora else ""
        lines.append(f"  • {d.port} — {d.product or d.description or 'serial device'}{flag}")
    return "\n".join(lines)


def resolve_device(
    devices: list[DiscoveredDevice],
    remembered: Optional[RememberedDevice],
    *,
    explicit_port: Optional[str] = None,
    profile: Optional[DeviceProfile] = None,
) -> Resolution:
    """Decide which serial port to use without prompting.

    Resolution priority:

    1. ``explicit_port`` (an explicit ``--port``).
    2. ``profile.port`` (an explicitly chosen profile with a port).
    3. The remembered "last known good" device, if it is currently attached.
    4. The single attached device, if exactly one is present.

    Otherwise a :class:`DeviceSelectionError` is raised listing the candidates.

    Args:
        devices: Currently discovered devices (see ``discover_devices``).
        remembered: The remembered default, if any.
        explicit_port: A serial port supplied on the command line.
        profile: A device profile supplied on the command line.

    Returns:
        A :class:`Resolution` naming the chosen port.

    Raises:
        DeviceSelectionError: If no port can be chosen unambiguously.
    """
    if explicit_port:
        return Resolution(explicit_port, _find_by_port(devices, explicit_port), "port")

    if profile is not None and profile.port:
        return Resolution(profile.port, _find_by_port(devices, profile.port), "profile")

    if remembered is not None:
        match = next((d for d in devices if remembered.matches(d)), None)
        if match is not None:
            return Resolution(match.port, match, "remembered")

    if len(devices) == 1:
        return Resolution(devices[0].port, devices[0], "only")

    if not devices:
        raise DeviceSelectionError(
            "No serial devices detected. Connect a companion device, or pass --port "
            "explicitly, or use --mock for the simulator."
        )

    raise DeviceSelectionError(
        "Multiple serial devices detected and no default to fall back on.\n"
        f"{_format_device_list(devices)}\n"
        "Choose one with --port <PORT> (or run 'meshterm devices' to inspect them). "
        "The chosen device is remembered as the default after it connects."
    )
