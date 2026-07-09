"""Companion-device discovery across transports (serial + Bluetooth LE).

Discovery enumerates two kinds of companion:

* **Serial**: USB serial ports via ``pyserial``, flagging the ones whose USB vendor ID
  matches hardware commonly used for LoRa companion devices (ESP32/nRF boards and their
  USB-UART bridges). Nothing is hidden — unrecognized adapters still appear, just sorted
  after the likely candidates.
* **Bluetooth LE**: nearby devices advertising a ``MeshCore*`` name, scanned via ``bleak``.
  Unlike a bare serial adapter, a BLE advert that names itself MeshCore is a strong signal,
  so scanned devices are always treated as likely companions.

This module has no UI and no persistent state; it only reads what is currently attached or
in range. The companion connection itself still goes through
:class:`~meshterm.core.connection.MeshCoreDevice`, which the ``transport`` field selects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)

#: Transport discriminators for a :class:`DiscoveredDevice` (also stored/remembered as-is).
TRANSPORT_SERIAL = "serial"
TRANSPORT_BLE = "ble"

#: BLE advertisements from a MeshCore companion begin with this name prefix; the scan filters
#: to it so unrelated Bluetooth gadgets (headphones, watches, beacons) never clutter the list.
BLE_NAME_PREFIX = "MeshCore"

#: Default seconds to scan for BLE companions. Long enough to hear a nearby device's periodic
#: advertisement, short enough not to stall the startup splash noticeably.
BLE_SCAN_TIMEOUT_S = 5.0

# USB vendor IDs frequently seen on MeshCore/Meshtastic companion hardware and the
# USB-UART bridges they ship with. Used only to name, flag, and sort likely devices.
KNOWN_LORA_VIDS: dict[int, str] = {
    0x303A: "Espressif",        # ESP32-S3 / native USB (e.g. XIAO ESP32-S3)
    0x10C4: "Silicon Labs",     # CP210x UART bridge
    0x1A86: "QinHeng",          # CH340 / CH9102 UART bridge
    0x0403: "FTDI",             # FT232 UART bridge
    0x239A: "Adafruit",         # nRF52840 boards
    0x2886: "Seeed Studio",     # XIAO / Wio boards
    0x1915: "Nordic",           # nRF52 native USB
}

# Native-USB VIDs of LoRa companion dev boards — the chip *is* the board, so seeing one is
# a strong signal the port is companion hardware.
NATIVE_LORA_VIDS: frozenset[int] = frozenset({0x303A, 0x239A, 0x2886, 0x1915})

# Generic USB-UART bridge chips. LoRa boards often use them, but so do countless unrelated
# gadgets (Arduinos, GPS pucks, 3D printers…), so their presence is only a weak hint.
UART_BRIDGE_VIDS: frozenset[int] = frozenset({0x10C4, 0x1A86, 0x0403})

# Sort rank per confidence tier: boards first, then bare serial bridges, then everything else.
_CONFIDENCE_RANK: dict[str, int] = {"board": 0, "bridge": 1, "unknown": 2}


@dataclass(slots=True)
class DiscoveredDevice:
    """A companion device found on the system, with transport metadata.

    A serial device carries its USB metadata; a Bluetooth LE device carries its BLE
    ``address`` and advertised ``name`` instead (its ``port`` is left blank). The
    ``transport`` field says which, and :attr:`target` returns the identifier used to
    open a connection regardless of kind.

    Attributes:
        port: Serial port path (e.g. ``COM5`` or ``/dev/ttyUSB0``); ``""`` for BLE.
        description: Human-readable description reported by the OS/driver/advert.
        hwid: Raw hardware id string from ``pyserial`` (VID/PID/serial blob); serial only.
        vid: USB vendor ID, if the port is a USB device.
        pid: USB product ID, if the port is a USB device.
        serial_number: USB serial number, if exposed by the device.
        manufacturer: USB manufacturer string, if available.
        product: USB product string, if available.
        transport: ``"serial"`` or ``"ble"`` — which connection layer opens this device.
        address: Bluetooth address for a BLE device (e.g. ``AA:BB:CC:DD:EE:FF``).
        name: Advertised BLE local name (e.g. ``"MeshCore-Basestation"``); BLE only.
    """

    port: str = ""
    description: str = ""
    hwid: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    transport: str = TRANSPORT_SERIAL
    address: Optional[str] = None
    name: Optional[str] = None

    @property
    def is_ble(self) -> bool:
        """Whether this device is reached over Bluetooth LE rather than a serial port."""
        return self.transport == TRANSPORT_BLE

    @property
    def target(self) -> str:
        """The identifier a connection opens on: the BLE address, else the serial port."""
        return self.address or self.port if self.is_ble else self.port

    @property
    def stable_id(self) -> str:
        """Return an identifier stable across replug/reboot.

        For BLE, the Bluetooth address (stable per adapter pairing). For serial, the USB
        serial number (survives a changed COM number), then the ``vid:pid`` pair, and
        finally the port name as a last resort.

        Returns:
            A non-empty identifier string used to recognize this device later.
        """
        if self.is_ble:
            return f"ble:{(self.address or self.name or '').lower()}"
        if self.serial_number:
            return f"sn:{self.serial_number}"
        if self.vid is not None and self.pid is not None:
            return f"vidpid:{self.vid:04x}:{self.pid:04x}"
        return f"port:{self.port}"

    @property
    def confidence(self) -> str:
        """How likely this device is a LoRa companion.

        Returns:
            ``"board"`` for a BLE device that named itself MeshCore or a LoRa dev board's
            native USB (both strong signals), ``"bridge"`` for a generic USB-UART chip (a
            weak hint — could be anything), or ``"unknown"`` for an unrecognized adapter.
        """
        if self.is_ble:
            return "board"  # a MeshCore-named BLE advert is a confident companion signal
        if self.vid in NATIVE_LORA_VIDS:
            return "board"
        if self.vid in UART_BRIDGE_VIDS:
            return "bridge"
        return "unknown"

    @property
    def is_likely_lora(self) -> bool:
        """Whether this looks like a companion: a MeshCore BLE advert or a known-VID board."""
        return self.confidence != "unknown"

    @property
    def vendor_label(self) -> str:
        """A friendly vendor/transport name for display.

        ``"Bluetooth"`` for a BLE device, else a known-VID vendor name or the USB
        manufacturer string.
        """
        if self.is_ble:
            return "Bluetooth"
        if self.vid in KNOWN_LORA_VIDS:
            return KNOWN_LORA_VIDS[self.vid]
        return self.manufacturer or ""

    @property
    def label(self) -> str:
        """A concise human-friendly label, e.g. ``"Wio SX1262 (COM5)"`` or ``"…(BLE)"``.

        The OS description often already ends with the port (Windows reports
        ``"USB Serial Device (COM11)"``), so the identifier is not appended a second time.
        """
        if self.is_ble:
            name = self.name or self.product or self.description or "Bluetooth device"
            return f"{name} (BLE)"
        name = self.product or self.description or self.vendor_label or "Serial device"
        suffix = f"({self.port})"
        if name.endswith(suffix):  # avoid "… (COM11) (COM11)"
            name = name[: -len(suffix)].rstrip()
        return f"{name} ({self.port})"


def _sort_key(device: DiscoveredDevice) -> tuple[int, str]:
    """Sort likely companions first, then bridges, then unknown; ties by target id."""
    return (_CONFIDENCE_RANK[device.confidence], device.target)


def discover_devices() -> list[DiscoveredDevice]:
    """Enumerate serial ports currently attached to the system.

    Likely LoRa companion devices (by known USB vendor ID) are returned first. If
    ``pyserial`` is unavailable the function returns an empty list rather than raising,
    so callers can fall back to ``--port``/``--mock`` with a friendly message.

    Returns:
        Discovered serial devices, likely candidates first, then sorted by port name.
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


async def discover_ble_devices(timeout: float = BLE_SCAN_TIMEOUT_S) -> list[DiscoveredDevice]:
    """Scan for nearby Bluetooth LE companions advertising a ``MeshCore*`` name.

    Uses ``bleak`` to listen for advertisements for ``timeout`` seconds and keeps only the
    ones whose advertised name marks them as a MeshCore companion. If ``bleak`` is
    unavailable, or the platform has no usable Bluetooth adapter, this returns an empty list
    rather than raising — BLE is an optional transport, and its absence must never block
    serial discovery or the simulator.

    Args:
        timeout: Seconds to scan for advertising devices.

    Returns:
        Discovered BLE companions (deduplicated by address), sorted by name.
    """
    try:
        from bleak import BleakScanner
    except ImportError:  # bleak not installed → BLE simply unavailable
        return []

    try:
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except Exception as exc:  # noqa: BLE001 - no adapter / OS Bluetooth off / driver hiccup
        _log.debug("BLE scan unavailable: %s", exc)
        return []

    devices: list[DiscoveredDevice] = []
    for dev, adv in found.values():
        name = (getattr(adv, "local_name", None) or getattr(dev, "name", None) or "").strip()
        if not name.startswith(BLE_NAME_PREFIX):
            continue
        devices.append(
            DiscoveredDevice(
                transport=TRANSPORT_BLE,
                address=dev.address,
                name=name,
                description=name,
                product=name,
            )
        )
    devices.sort(key=lambda d: (d.name or "").casefold())
    return devices


async def discover_all(
    *, ble: bool = True, ble_timeout: float = BLE_SCAN_TIMEOUT_S
) -> list[DiscoveredDevice]:
    """Enumerate every attached or in-range companion across all transports.

    Serial enumeration is instant; the BLE scan is what takes ``ble_timeout`` seconds. Serial
    devices are listed first (they're already attached and connect fastest), then BLE
    companions.

    Args:
        ble: Whether to include a Bluetooth LE scan (skip it to avoid the scan delay).
        ble_timeout: Seconds to spend scanning for BLE companions.

    Returns:
        All discovered devices: serial (likely-LoRa first), then BLE companions.
    """
    serial = discover_devices()
    if not ble:
        return serial
    # A device paired over both USB and BLE is vanishingly unlikely to collide by stable_id
    # (USB serial number vs BLE address), so no cross-transport dedup is needed here.
    ble_devices = await discover_ble_devices(ble_timeout)
    return serial + ble_devices
