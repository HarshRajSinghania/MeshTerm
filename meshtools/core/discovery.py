"""Serial-port enumeration for companion-device discovery.

Discovery is protocol-agnostic: it lists USB serial ports via ``pyserial`` and flags the
ones whose USB vendor ID matches hardware commonly used for LoRa companion devices
(ESP32/nRF boards and their USB-UART bridges). Nothing is hidden — unrecognized adapters
still appear, just sorted after the likely candidates.

This module has no UI and no persistent state; it only reads what is currently plugged in.
The companion connection itself still goes through
:class:`~meshtools.core.connection.MeshCoreDevice`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# USB vendor IDs frequently seen on MeshCore/Meshtastic companion hardware and the
# USB-UART bridges they ship with. Used only to flag and sort likely devices.
KNOWN_LORA_VIDS: dict[int, str] = {
    0x303A: "Espressif",        # ESP32-S3 / native USB (e.g. XIAO ESP32-S3)
    0x10C4: "Silicon Labs",     # CP210x UART bridge
    0x1A86: "QinHeng",          # CH340 / CH9102 UART bridge
    0x0403: "FTDI",             # FT232 UART bridge
    0x239A: "Adafruit",         # nRF52840 boards
    0x2886: "Seeed Studio",     # XIAO / Wio boards
    0x1915: "Nordic",           # nRF52 native USB
}


@dataclass(slots=True)
class DiscoveredDevice:
    """A serial port found on the system, with USB metadata when available.

    Attributes:
        port: Serial port path (e.g. ``COM5`` or ``/dev/ttyUSB0``).
        description: Human-readable description reported by the OS/driver.
        hwid: Raw hardware id string from ``pyserial`` (VID/PID/serial blob).
        vid: USB vendor ID, if the port is a USB device.
        pid: USB product ID, if the port is a USB device.
        serial_number: USB serial number, if exposed by the device.
        manufacturer: USB manufacturer string, if available.
        product: USB product string, if available.
    """

    port: str
    description: str = ""
    hwid: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None

    @property
    def stable_id(self) -> str:
        """Return an identifier stable across replug/reboot.

        Prefers the USB serial number (survives a changed COM number), then the
        ``vid:pid`` pair, and finally the port name as a last resort.

        Returns:
            A non-empty identifier string used to recognize this device later.
        """
        if self.serial_number:
            return f"sn:{self.serial_number}"
        if self.vid is not None and self.pid is not None:
            return f"vidpid:{self.vid:04x}:{self.pid:04x}"
        return f"port:{self.port}"

    @property
    def is_likely_lora(self) -> bool:
        """Whether this device's USB vendor ID matches known companion hardware."""
        return self.vid in KNOWN_LORA_VIDS

    @property
    def vendor_label(self) -> str:
        """A friendly vendor name from the known-VID table, or the manufacturer string."""
        if self.vid in KNOWN_LORA_VIDS:
            return KNOWN_LORA_VIDS[self.vid]
        return self.manufacturer or ""

    @property
    def label(self) -> str:
        """A concise human-friendly label, e.g. ``"Wio SX1262 (COM5)"``."""
        name = self.product or self.description or self.vendor_label or "Serial device"
        return f"{name} ({self.port})"


def _sort_key(device: DiscoveredDevice) -> tuple[int, str]:
    """Sort likely-LoRa devices first, then by port name for stable ordering."""
    return (0 if device.is_likely_lora else 1, device.port)


def discover_devices() -> list[DiscoveredDevice]:
    """Enumerate serial ports currently attached to the system.

    Likely LoRa companion devices (by known USB vendor ID) are returned first. If
    ``pyserial`` is unavailable the function returns an empty list rather than raising,
    so callers can fall back to ``--port``/``--mock`` with a friendly message.

    Returns:
        Discovered devices, likely candidates first, then sorted by port name.
    """
    try:
        from serial.tools import list_ports
    except ImportError:  # pragma: no cover - pyserial is a declared dependency
        return []

    devices = [
        DiscoveredDevice(
            port=info.device,
            description=info.description or "",
            hwid=info.hwid or "",
            vid=info.vid,
            pid=info.pid,
            serial_number=info.serial_number,
            manufacturer=info.manufacturer,
            product=info.product,
        )
        for info in list_ports.comports()
    ]
    devices.sort(key=_sort_key)
    return devices
