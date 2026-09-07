"""Tests for settings that share one device command, and for the PIN's readback.

Both were found restoring a reflashed companion from its own backup.

Latitude and longitude go to the firmware together, and so do the four radio parameters,
so changing one means re-sending its siblings unchanged. Those siblings come from the
caller's snapshot — read once, before the first write. A restore that differed in both
coordinates therefore applied ``adv_lat`` (preserving the stale longitude), then
``adv_lon`` (preserving the stale *latitude*), and the second write silently undid the
first. On the real device that left latitude at 0.0 and the frequency on the factory
default while every op reported success.
"""

from __future__ import annotations

import asyncio

import pytest

from meshterm.core.device_config import (
    DEVICE_SETTINGS,
    build_snapshot,
    get_spec,
    parse_value,
)


class _RecordingDevice:
    """Captures the coupled commands, and models the firmware: last write wins."""

    def __init__(self, **state) -> None:
        self.state = dict(state)
        self.radio_calls: list[tuple] = []
        self.coord_calls: list[tuple] = []
        self.tuning_calls: list[tuple] = []

    async def set_radio(self, freq, bw, sf, cr) -> None:
        self.radio_calls.append((freq, bw, sf, cr))
        self.state.update(radio_freq=freq, radio_bw=bw, radio_sf=sf, radio_cr=cr)

    async def set_coords(self, lat, lon) -> None:
        self.coord_calls.append((lat, lon))
        self.state.update(adv_lat=lat, adv_lon=lon)

    async def set_tuning(self, rx_delay, airtime_factor) -> None:
        self.tuning_calls.append((rx_delay, airtime_factor))
        self.state.update(rx_delay=rx_delay, airtime_factor=airtime_factor)


async def _apply_all(device, snapshot, changes):
    """Apply each change the way a restore or a staged-edit batch does: in sequence."""
    for key, raw in changes:
        spec = get_spec(key)
        await spec.apply(device, parse_value(spec, raw, snapshot), snapshot)


def test_both_coordinates_survive_being_applied_together() -> None:
    """THE bug: the second coordinate must not re-send the first one's stale value.

    Restoring a backup onto a factory-fresh node changes both at once. Before the fix the
    device ended at ``(0.0, -73.708724)`` — longitude right, latitude silently reverted.
    """
    device = _RecordingDevice(adv_lat=0.0, adv_lon=0.0)
    snapshot = dict(device.state)

    asyncio.run(
        _apply_all(
            device,
            snapshot,
            [
                ("adv_lat", "45.535445"),
                ("adv_lon", "-73.708724"),
            ],
        )
    )

    assert device.state["adv_lat"] == pytest.approx(45.535445)
    assert device.state["adv_lon"] == pytest.approx(-73.708724)
    # The last write must carry both, not one real value and one stale zero.
    assert device.coord_calls[-1] == pytest.approx((45.535445, -73.708724))


def test_the_order_the_coordinates_are_applied_in_does_not_matter() -> None:
    """Neither ordering may lose a value — a plan's op order is not something to rely on."""
    for changes in (
        [("adv_lat", "45.5"), ("adv_lon", "-73.7")],
        [("adv_lon", "-73.7"), ("adv_lat", "45.5")],
    ):
        device = _RecordingDevice(adv_lat=0.0, adv_lon=0.0)
        asyncio.run(_apply_all(device, dict(device.state), changes))
        assert (device.state["adv_lat"], device.state["adv_lon"]) == pytest.approx((45.5, -73.7)), (
            changes
        )


def test_two_radio_fields_applied_together_both_stick() -> None:
    """The frequency reverted to the factory default when the spreading factor followed it."""
    device = _RecordingDevice(radio_freq=869.618, radio_bw=62.5, radio_sf=8, radio_cr=5)
    snapshot = dict(device.state)

    asyncio.run(
        _apply_all(
            device,
            snapshot,
            [
                ("radio_freq", "910.525"),
                ("radio_sf", "7"),
            ],
        )
    )

    assert device.state["radio_freq"] == pytest.approx(910.525)
    assert device.state["radio_sf"] == 7
    assert device.state["radio_bw"] == pytest.approx(62.5)  # untouched siblings preserved
    assert device.state["radio_cr"] == 5


def test_all_four_radio_fields_at_once() -> None:
    """A full radio change is the worst case: three chances for a sibling to go stale."""
    device = _RecordingDevice(radio_freq=869.618, radio_bw=250.0, radio_sf=8, radio_cr=8)

    asyncio.run(
        _apply_all(
            device,
            dict(device.state),
            [
                ("radio_freq", "910.525"),
                ("radio_bw", "62.5"),
                ("radio_sf", "7"),
                ("radio_cr", "5"),
            ],
        )
    )

    assert device.radio_calls[-1] == pytest.approx((910.525, 62.5, 7, 5))


def test_both_tuning_fields_applied_together_both_stick() -> None:
    """rx_delay and airtime_factor share a command on the same terms."""
    device = _RecordingDevice(rx_delay=0.0, airtime_factor=0.0)

    asyncio.run(
        _apply_all(
            device,
            dict(device.state),
            [
                ("rx_delay", "1.5"),
                ("airtime_factor", "2.0"),
            ],
        )
    )

    assert device.tuning_calls[-1] == pytest.approx((1.5, 2.0))


def test_a_coupled_apply_leaves_the_snapshot_describing_the_device() -> None:
    """The write-back is the mechanism, so state it: the snapshot tracks what was set."""
    device = _RecordingDevice(adv_lat=0.0, adv_lon=0.0)
    snapshot = dict(device.state)

    asyncio.run(_apply_all(device, snapshot, [("adv_lat", "45.5")]))

    assert snapshot["adv_lat"] == pytest.approx(45.5)


# --- the Device PIN's readback ----------------------------------------------------


class _InfoDevice:
    """A device whose two info frames carry different keys, as real firmware's do."""

    def __init__(self, self_info: dict, device_info: dict) -> None:
        self._self_info = self_info
        self._device_info = device_info

    async def get_self_info(self) -> dict:
        return dict(self._self_info)

    async def get_device_info(self) -> dict:
        return dict(self._device_info)

    async def get_tuning(self):
        raise NotImplementedError

    async def get_path_hash_mode(self):
        raise NotImplementedError

    async def get_autoadd_config(self):
        raise NotImplementedError

    async def get_default_flood_scope(self):
        raise NotImplementedError


def test_the_snapshot_folds_in_the_device_query_frame() -> None:
    """``ble_pin`` lives only in the device-query payload, so the snapshot must read it."""
    device = _InfoDevice({"name": "Homestead"}, {"ble_pin": 701307, "model": "T1000-E"})

    snapshot = asyncio.run(build_snapshot(device))

    assert snapshot["ble_pin"] == 701307
    assert snapshot["model"] == "T1000-E"
    assert snapshot["name"] == "Homestead"


def test_self_info_wins_where_the_two_frames_overlap() -> None:
    """SELF_INFO is the authority on anything it reports; device-query only fills gaps."""
    device = _InfoDevice({"name": "from-self-info"}, {"name": "from-device-query"})

    assert asyncio.run(build_snapshot(device))["name"] == "from-self-info"


def test_the_device_pin_row_reads_the_firmware_s_own_name() -> None:
    """It rendered "?" on every real companion: written as device_pin, reported as ble_pin."""
    spec = get_spec("device_pin")

    assert spec.getter({"ble_pin": 701307}) == 701307
    assert spec.getter({"device_pin": 424242}) == 424242  # canonical key still honoured
    assert spec.getter({}) is None  # genuinely unknown stays unknown


def test_a_snapshot_read_failure_still_yields_the_rest() -> None:
    """The device-query frame is optional: older firmware simply contributes nothing."""

    class _NoQuery(_InfoDevice):
        async def get_device_info(self):
            raise RuntimeError("firmware predates the device query")

    snapshot = asyncio.run(build_snapshot(_NoQuery({"name": "Homestead"}, {})))
    assert snapshot["name"] == "Homestead"
    assert get_spec("device_pin").getter(snapshot) is None


def test_every_coupled_setting_is_covered_here() -> None:
    """A new setting sharing a coupled command must be added to these tests, not forgotten."""
    coupled = {
        "adv_lat",
        "adv_lon",
        "radio_freq",
        "radio_bw",
        "radio_sf",
        "radio_cr",
        "rx_delay",
        "airtime_factor",
    }
    known = {spec.key for spec in DEVICE_SETTINGS}
    assert coupled <= known, coupled - known
