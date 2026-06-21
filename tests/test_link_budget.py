"""Unit tests for the offline LoRa link-budget calculator.

These pin the airtime/sensitivity math against textbook LoRa values and check the
monotonic relationships the tool relies on, all without any hardware.
"""

from __future__ import annotations

import pytest

from meshtools.services import link_budget
from meshtools.services.link_budget import RadioConfig


def test_symbol_time_and_low_data_rate() -> None:
    """Symbol time is 2**SF / BW; LDR auto-enables past the 16 ms threshold."""
    sf12 = RadioConfig(freq_mhz=915, bw_khz=125, sf=12)
    assert sf12.symbol_time_ms() == pytest.approx(32.768, abs=1e-3)
    assert sf12.low_data_rate_enabled() is True  # 32.77 ms > 16 ms

    sf7 = RadioConfig(freq_mhz=915, bw_khz=125, sf=7)
    assert sf7.symbol_time_ms() == pytest.approx(1.024, abs=1e-3)
    assert sf7.low_data_rate_enabled() is False


def test_time_on_air_matches_semtech_reference() -> None:
    """SF12/BW125/CR4-5/20-byte payload airtime matches the canonical ~1318.9 ms."""
    config = RadioConfig(freq_mhz=868, bw_khz=125, sf=12, cr_denom=5)
    toa = link_budget.time_on_air(config, payload_bytes=20)
    assert toa.total_ms == pytest.approx(1318.9, abs=1.0)
    assert toa.payload_symbols == 28
    assert toa.bitrate_bps > 0


def test_time_on_air_grows_with_spreading_factor() -> None:
    """Airtime increases monotonically with SF for a fixed payload."""
    durations = [
        link_budget.time_on_air(
            RadioConfig(freq_mhz=915, bw_khz=250, sf=sf), payload_bytes=32
        ).total_ms
        for sf in range(7, 13)
    ]
    assert durations == sorted(durations)
    assert all(b > a for a, b in zip(durations, durations[1:], strict=False))


def test_receiver_sensitivity_formula() -> None:
    """Sensitivity = -174 + 10log10(BW) + NF + SNR_limit(SF)."""
    config = RadioConfig(freq_mhz=915, bw_khz=125, sf=12)
    # -174 + 10*log10(125000) + 6 + (-20) ≈ -137.03 dBm
    assert link_budget.receiver_sensitivity(config) == pytest.approx(-137.0, abs=0.1)
    # A wider bandwidth raises (worsens) the noise floor.
    wide = RadioConfig(freq_mhz=915, bw_khz=500, sf=12)
    assert link_budget.receiver_sensitivity(wide) > link_budget.receiver_sensitivity(config)


def test_duty_cycle_headroom() -> None:
    """A 1 s packet at 1% duty allows 36 packets/hour and 100 s spacing."""
    duty = link_budget.duty_cycle(1000.0, limit_percent=1.0)
    assert duty.max_packets_per_hour == pytest.approx(36.0)
    assert duty.min_interval_s == pytest.approx(100.0)
    assert duty.dwell_ok is None  # no dwell limit supplied


def test_dwell_time_check() -> None:
    """The dwell check flags packets longer than the regional max dwell time."""
    assert link_budget.duty_cycle(500.0, max_dwell_ms=400.0).dwell_ok is False
    assert link_budget.duty_cycle(300.0, max_dwell_ms=400.0).dwell_ok is True


def test_range_estimate_responds_to_loss_and_exponent() -> None:
    """Range grows with tolerable path loss and shrinks in a clutter-heavy model."""
    near = link_budget.free_space_range_km(120.0, 915.0, exponent=2.0)
    far = link_budget.free_space_range_km(140.0, 915.0, exponent=2.0)
    assert far > near
    # A higher path-loss exponent (more clutter) yields a shorter range for the same loss.
    urban = link_budget.free_space_range_km(140.0, 915.0, exponent=3.5)
    assert urban < far
    # Path loss below the 1 km free-space reference yields zero range, never negative.
    assert link_budget.free_space_range_km(10.0, 915.0, exponent=2.0) == 0.0


def test_compute_link_budget_integration() -> None:
    """A full budget ties the pieces together with a positive margin at a known loss."""
    config = RadioConfig(freq_mhz=915, bw_khz=250, sf=10, cr_denom=5)
    budget = link_budget.compute_link_budget(
        config,
        payload_bytes=32,
        tx_power_dbm=22,
        tx_gain_dbi=3,
        rx_gain_dbi=3,
        reference_path_loss_db=120.0,
        duty_limit_percent=1.0,
        max_dwell_ms=400.0,
    )
    # max path loss = EIRP(25) + rx_gain(3) - sensitivity
    expected_mpl = 22 + 3 + 3 - budget.sensitivity_dbm
    assert budget.max_path_loss_db == pytest.approx(expected_mpl)
    assert budget.link_margin_db == pytest.approx(expected_mpl - 120.0)
    assert budget.range_km > 0
    assert budget.duty.max_packets_per_hour > 0
