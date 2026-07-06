"""Tests for device discovery, the remembered-device store, and selection.

All run without hardware: serial enumeration is monkeypatched, and the store/selection
logic is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from meshtools.core.config import DeviceProfile
from meshtools.core.device_store import DeviceStore
from meshtools.core.discovery import DiscoveredDevice, discover_devices
from meshtools.core.selection import DeviceSelectionError, resolve_device


@dataclass
class FakePortInfo:
    """Stand-in for ``serial.tools.list_ports_common.ListPortInfo``."""

    device: str
    description: Optional[str] = None
    hwid: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: Optional[str] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None


def _patch_ports(monkeypatch: pytest.MonkeyPatch, ports: list[FakePortInfo]) -> None:
    """Make ``discover_devices`` see exactly ``ports``."""
    from serial.tools import list_ports

    monkeypatch.setattr(list_ports, "comports", lambda: list(ports))


# -- discovery -----------------------------------------------------------------


def test_discover_maps_fields_and_flags_lora(monkeypatch: pytest.MonkeyPatch) -> None:
    """USB metadata is mapped through and known vendors are flagged as likely LoRa."""
    _patch_ports(
        monkeypatch,
        [
            FakePortInfo(
                device="COM5",
                description="USB Serial",
                vid=0x303A,  # Espressif → likely LoRa
                pid=0x1001,
                serial_number="ABC123",
                product="Wio SX1262",
            )
        ],
    )
    [dev] = discover_devices()
    assert dev.port == "COM5"
    assert dev.product == "Wio SX1262"
    assert dev.is_likely_lora
    assert dev.vendor_label == "Espressif"
    assert "COM5" in dev.label


def test_discover_sorts_likely_lora_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Likely-LoRa devices sort ahead of unrecognized adapters."""
    _patch_ports(
        monkeypatch,
        [
            FakePortInfo(device="COM9", vid=0x1234, pid=0x0001),  # unknown vendor
            FakePortInfo(device="COM3", vid=0x10C4, pid=0xEA60),  # Silabs → LoRa
        ],
    )
    devices = discover_devices()
    assert [d.port for d in devices] == ["COM3", "COM9"]
    assert devices[0].is_likely_lora and not devices[1].is_likely_lora


def test_confidence_tiers_distinguish_boards_from_bridges() -> None:
    """A native-USB board is 'board'; a bare UART bridge is only 'bridge'; else 'unknown'."""
    board = DiscoveredDevice("COM5", vid=0x303A, pid=0x1001)  # Espressif native USB
    bridge = DiscoveredDevice("COM6", vid=0x10C4, pid=0xEA60)  # CP210x UART bridge
    unknown = DiscoveredDevice("COM7", vid=0x1234, pid=0x0001)
    assert board.confidence == "board" and board.is_likely_lora
    assert bridge.confidence == "bridge" and bridge.is_likely_lora
    assert unknown.confidence == "unknown" and not unknown.is_likely_lora


def test_sort_orders_boards_then_bridges_then_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery sorts LoRa boards ahead of bare serial bridges, and both ahead of unknown."""
    _patch_ports(
        monkeypatch,
        [
            FakePortInfo(device="COM9", vid=0x1234, pid=0x0001),  # unknown
            FakePortInfo(device="COM3", vid=0x10C4, pid=0xEA60),  # bridge
            FakePortInfo(device="COM1", vid=0x303A, pid=0x1001),  # board
        ],
    )
    assert [d.port for d in discover_devices()] == ["COM1", "COM3", "COM9"]


def test_label_does_not_repeat_the_port() -> None:
    """A Windows description already ending in '(COM11)' is not suffixed with it again."""
    dev = DiscoveredDevice("COM11", description="USB Serial Device (COM11)")
    assert dev.label == "USB Serial Device (COM11)"
    # A product name without the port still gets one appended.
    assert DiscoveredDevice("COM5", product="Wio SX1262").label == "Wio SX1262 (COM5)"


def test_stable_id_precedence() -> None:
    """stable_id prefers serial number, then vid:pid, then the port name."""
    assert DiscoveredDevice("COM5", serial_number="SN1", vid=1, pid=2).stable_id == "sn:SN1"
    assert DiscoveredDevice("COM5", vid=0x303A, pid=0x1001).stable_id == "vidpid:303a:1001"
    assert DiscoveredDevice("COM5").stable_id == "port:COM5"


# -- device store --------------------------------------------------------------


def test_device_store_round_trip(tmp_path: Path) -> None:
    """A remembered device writes and reads back, matched by stable_id."""
    store = DeviceStore(tmp_path / "devices.json")
    assert store.load() is None  # nothing remembered yet

    dev = DiscoveredDevice("COM5", serial_number="SN1", product="Wio SX1262")
    store.remember(dev, node_name="BaseStation")

    loaded = store.load()
    assert loaded is not None
    assert loaded.stable_id == "sn:SN1"
    assert loaded.node_name == "BaseStation"  # the mesh name learned on connect
    assert loaded.matches(dev)
    assert not loaded.matches(DiscoveredDevice("COM6", serial_number="OTHER"))


def test_remember_keeps_known_node_name_when_none_supplied(tmp_path: Path) -> None:
    """A later connect without a node name preserves the previously remembered one."""
    store = DeviceStore(tmp_path / "devices.json")
    dev = DiscoveredDevice("COM5", serial_number="SN1")
    store.remember(dev, node_name="BaseStation")

    store.remember(dev)  # e.g. the identity probe failed this time
    loaded = store.load()
    assert loaded is not None and loaded.node_name == "BaseStation"


def test_device_store_tolerates_corrupt_file(tmp_path: Path) -> None:
    """A corrupt state file is treated as 'nothing remembered'."""
    path = tmp_path / "devices.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert DeviceStore(path).load() is None


# -- selection -----------------------------------------------------------------


def test_resolve_prefers_explicit_port() -> None:
    """An explicit --port wins over everything else."""
    devices = [DiscoveredDevice("COM3", serial_number="SN1")]
    res = resolve_device(devices, None, explicit_port="COM9")
    assert res.port == "COM9" and res.source == "port"


def test_resolve_uses_profile_port() -> None:
    """A profile with a port is used when no --port is given."""
    profile = DeviceProfile(name="yagi", port="COM6")
    res = resolve_device([], None, profile=profile)
    assert res.port == "COM6" and res.source == "profile"


def test_resolve_uses_remembered_when_present() -> None:
    """The remembered default is used when that device is still attached."""
    dev = DiscoveredDevice("COM3", serial_number="SN1")
    store_dev = DiscoveredDevice("COM7", serial_number="SN1")  # same hardware, new port
    store = _remember(store_dev)
    res = resolve_device([dev], store, profile=None)
    assert res.port == "COM3" and res.source == "remembered"
    assert res.device is dev


def test_resolve_single_device_auto() -> None:
    """With exactly one device and no default, it is chosen automatically."""
    dev = DiscoveredDevice("COM3", serial_number="SN1")
    res = resolve_device([dev], None)
    assert res.port == "COM3" and res.source == "only"


def test_resolve_ambiguous_raises(tmp_path: Path) -> None:
    """Multiple devices with no usable default raise a guidance error."""
    devices = [
        DiscoveredDevice("COM3", serial_number="SN1"),
        DiscoveredDevice("COM4", serial_number="SN2"),
    ]
    with pytest.raises(DeviceSelectionError, match="Multiple serial devices"):
        resolve_device(devices, None)


def test_resolve_no_devices_raises() -> None:
    """No devices at all raises a clear error mentioning --mock."""
    with pytest.raises(DeviceSelectionError, match="No serial devices"):
        resolve_device([], None)


def _remember(device: DiscoveredDevice):
    """Build a RememberedDevice for ``device`` without touching disk."""
    from meshtools.core.device_store import RememberedDevice

    return RememberedDevice(
        stable_id=device.stable_id, port=device.port, label=device.label, last_connected=""
    )
