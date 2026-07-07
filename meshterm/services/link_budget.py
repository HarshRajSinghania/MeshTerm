"""Offline LoRa link-budget, time-on-air, and duty-cycle math.

This is the *predictive* counterpart to the empirical TX optimizer: given a radio
configuration (frequency, bandwidth, spreading factor, coding rate) and a link's
geometry (TX power, antenna gains, path-loss model) it computes — with no radio attached —

* **time-on-air** and bitrate for a packet, the basis for duty-cycle planning;
* **receiver sensitivity** from the thermal noise floor and the SF's demodulator limit;
* the **maximum tolerable path loss** and a rough **range estimate** from a log-distance
  model;
* **duty-cycle / dwell-time headroom** against a regional limit.

Everything here is pure arithmetic over plain dataclasses, so it is fully unit-testable
and needs no hardware. The formulas follow Semtech's LoRa modem design guide (AN1200.13);
range and sensitivity are first-order estimates meant for *comparing* configurations, not
for certifying a deployment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

#: Demodulator SNR floor (dB) per spreading factor — the SNR below which a LoRa symbol
#: can no longer be decoded. From Semtech's published figures; used to derive receiver
#: sensitivity. SF7..SF12 are the values MeshCore radios actually use.
SNR_LIMIT_DB: dict[int, float] = {
    6: -5.0,
    7: -7.5,
    8: -10.0,
    9: -12.5,
    10: -15.0,
    11: -17.5,
    12: -20.0,
}

#: Receiver noise figure (dB) assumed for a typical SX126x front end.
DEFAULT_NOISE_FIGURE_DB = 6.0

#: Path-loss exponent presets. Free space is the optimistic ceiling; the others are
#: common log-distance values for increasingly cluttered environments.
PATH_LOSS_EXPONENTS: dict[str, float] = {
    "free-space": 2.0,
    "rural": 2.7,
    "suburban": 3.0,
    "urban": 3.5,
}


@dataclass(slots=True)
class RadioConfig:
    """A LoRa physical-layer configuration.

    Attributes:
        freq_mhz: Carrier frequency in MHz.
        bw_khz: Bandwidth in kHz (e.g. ``250``).
        sf: Spreading factor (6-12).
        cr_denom: Coding-rate denominator (5-8, i.e. 4/5 .. 4/8).
        preamble_symbols: Preamble length in symbols.
        explicit_header: Whether the explicit LoRa header is used.
        crc: Whether the payload CRC is enabled.
        low_data_rate: Low-data-rate optimization. ``None`` auto-enables it when the
            symbol time exceeds 16 ms (the conventional threshold).
    """

    freq_mhz: float
    bw_khz: float
    sf: int
    cr_denom: int = 5
    preamble_symbols: int = 8
    explicit_header: bool = True
    crc: bool = True
    low_data_rate: Optional[bool] = None

    def symbol_time_ms(self) -> float:
        """Return the LoRa symbol duration in milliseconds (``2**SF / BW``)."""
        return (2**self.sf) / (self.bw_khz * 1000.0) * 1000.0

    def low_data_rate_enabled(self) -> bool:
        """Return whether low-data-rate optimization applies for this config."""
        if self.low_data_rate is not None:
            return self.low_data_rate
        return self.symbol_time_ms() > 16.0


@dataclass(slots=True)
class TimeOnAir:
    """A packet's airtime breakdown.

    Attributes:
        payload_bytes: Application payload size the figures were computed for.
        total_ms: Total time-on-air in milliseconds.
        preamble_ms: Preamble portion in milliseconds.
        payload_ms: Payload portion in milliseconds.
        payload_symbols: Number of symbols carrying the payload.
        bitrate_bps: Effective LoRa bitrate in bits per second.
    """

    payload_bytes: int
    total_ms: float
    preamble_ms: float
    payload_ms: float
    payload_symbols: int
    bitrate_bps: float


@dataclass(slots=True)
class DutyCycle:
    """Duty-cycle and dwell-time headroom for a configuration.

    Attributes:
        limit_percent: The regional duty-cycle ceiling applied (e.g. ``1.0`` for 1%).
        airtime_ms: Time-on-air of one packet.
        max_packets_per_hour: Packets/hour that fit under ``limit_percent``.
        min_interval_s: Minimum spacing between packets to stay legal.
        max_dwell_ms: Regional max dwell time, if any (e.g. 400 ms for US915 FHSS).
        dwell_ok: Whether one packet fits within ``max_dwell_ms`` (``None`` if no limit).
    """

    limit_percent: float
    airtime_ms: float
    max_packets_per_hour: float
    min_interval_s: float
    max_dwell_ms: Optional[float]
    dwell_ok: Optional[bool]


@dataclass(slots=True)
class LinkBudget:
    """A complete link-budget estimate for one radio config and geometry.

    Attributes:
        config: The radio configuration evaluated.
        toa: The packet airtime breakdown.
        sensitivity_dbm: Estimated receiver sensitivity in dBm.
        tx_power_dbm: Transmit power used in the budget.
        tx_gain_dbi: Transmit antenna gain.
        rx_gain_dbi: Receive antenna gain.
        max_path_loss_db: Largest path loss the link can tolerate (EIRP-to-sensitivity).
        range_km: Estimated range from the log-distance model.
        path_loss_exponent: The exponent used for the range estimate.
        link_margin_db: Margin at ``reference_path_loss_db`` when one was supplied.
        duty: Duty-cycle headroom for the configuration.
    """

    config: RadioConfig
    toa: TimeOnAir
    sensitivity_dbm: float
    tx_power_dbm: float
    tx_gain_dbi: float
    rx_gain_dbi: float
    max_path_loss_db: float
    range_km: float
    path_loss_exponent: float
    link_margin_db: Optional[float]
    duty: DutyCycle


def time_on_air(config: RadioConfig, payload_bytes: int) -> TimeOnAir:
    """Compute the LoRa time-on-air for a payload.

    Implements the airtime formula from Semtech AN1200.13: a preamble of
    ``(n + 4.25)`` symbols followed by a payload whose symbol count depends on SF, the
    coding rate, header mode, CRC, and low-data-rate optimization.

    Args:
        config: The radio configuration.
        payload_bytes: Application payload length in bytes.

    Returns:
        A :class:`TimeOnAir` with the airtime breakdown and effective bitrate.
    """
    sf = config.sf
    tsym_ms = config.symbol_time_ms()
    de = 1 if config.low_data_rate_enabled() else 0
    ih = 0 if config.explicit_header else 1
    crc = 1 if config.crc else 0

    preamble_symbols = config.preamble_symbols + 4.25
    # Semtech payload-symbol formula; the numerator can go negative for tiny payloads,
    # so the ceiling is floored at zero before the fixed 8-symbol header overhead.
    numerator = 8 * payload_bytes - 4 * sf + 28 + 16 * crc - 20 * ih
    denominator = 4 * (sf - 2 * de)
    payload_symbols = 8 + max(math.ceil(numerator / denominator) * config.cr_denom, 0)

    preamble_ms = preamble_symbols * tsym_ms
    payload_ms = payload_symbols * tsym_ms
    total_ms = preamble_ms + payload_ms
    bitrate_bps = (payload_bytes * 8) / (total_ms / 1000.0) if total_ms > 0 else 0.0

    return TimeOnAir(
        payload_bytes=payload_bytes,
        total_ms=total_ms,
        preamble_ms=preamble_ms,
        payload_ms=payload_ms,
        payload_symbols=payload_symbols,
        bitrate_bps=bitrate_bps,
    )


def receiver_sensitivity(
    config: RadioConfig, noise_figure_db: float = DEFAULT_NOISE_FIGURE_DB
) -> float:
    """Estimate receiver sensitivity in dBm.

    Sensitivity is the thermal noise floor in the channel plus the receiver noise figure
    plus the spreading factor's demodulator SNR limit:
    ``-174 + 10*log10(BW_Hz) + NF + SNR_limit(SF)``.

    Args:
        config: The radio configuration.
        noise_figure_db: Receiver noise figure in dB.

    Returns:
        The estimated sensitivity in dBm (more negative is better).
    """
    bw_hz = config.bw_khz * 1000.0
    snr_limit = SNR_LIMIT_DB.get(config.sf, SNR_LIMIT_DB[12])
    return -174.0 + 10.0 * math.log10(bw_hz) + noise_figure_db + snr_limit


def free_space_range_km(max_path_loss_db: float, freq_mhz: float, exponent: float) -> float:
    """Invert a log-distance path-loss model to estimate range.

    Uses free-space path loss as the 1 km reference and scales by ``exponent``:
    ``PL(d) = FSPL(1km) + 10*n*log10(d_km)``. With ``n = 2`` this is pure free space;
    higher exponents model clutter. The result is an optimistic line-of-sight figure for
    *comparing* configurations, not a guaranteed coverage radius.

    Args:
        max_path_loss_db: The largest path loss the link can tolerate.
        freq_mhz: Carrier frequency in MHz.
        exponent: Path-loss exponent (2 = free space).

    Returns:
        The estimated range in kilometres (never negative).
    """
    # Free-space path loss at the 1 km reference distance.
    fspl_1km = 32.45 + 20.0 * math.log10(freq_mhz) + 20.0 * math.log10(1.0)
    excess = max_path_loss_db - fspl_1km
    if excess <= 0:
        return 0.0
    return 10.0 ** (excess / (10.0 * exponent))


def duty_cycle(
    airtime_ms: float,
    *,
    limit_percent: float = 1.0,
    max_dwell_ms: Optional[float] = None,
) -> DutyCycle:
    """Compute duty-cycle headroom for a packet airtime.

    Args:
        airtime_ms: Time-on-air of one packet in milliseconds.
        limit_percent: Regional duty-cycle ceiling as a percent (e.g. ``1.0`` = 1%).
        max_dwell_ms: Regional maximum dwell time per transmission, if any.

    Returns:
        A :class:`DutyCycle` summary.
    """
    fraction = limit_percent / 100.0
    airtime_s = airtime_ms / 1000.0
    if airtime_s > 0:
        max_per_hour = (fraction * 3600.0) / airtime_s
        min_interval_s = airtime_s / fraction
    else:
        max_per_hour = math.inf
        min_interval_s = 0.0
    dwell_ok = None if max_dwell_ms is None else airtime_ms <= max_dwell_ms
    return DutyCycle(
        limit_percent=limit_percent,
        airtime_ms=airtime_ms,
        max_packets_per_hour=max_per_hour,
        min_interval_s=min_interval_s,
        max_dwell_ms=max_dwell_ms,
        dwell_ok=dwell_ok,
    )


def compute_link_budget(
    config: RadioConfig,
    *,
    payload_bytes: int = 32,
    tx_power_dbm: float = 22.0,
    tx_gain_dbi: float = 2.0,
    rx_gain_dbi: float = 2.0,
    noise_figure_db: float = DEFAULT_NOISE_FIGURE_DB,
    path_loss_exponent: float = PATH_LOSS_EXPONENTS["suburban"],
    reference_path_loss_db: Optional[float] = None,
    duty_limit_percent: float = 1.0,
    max_dwell_ms: Optional[float] = None,
) -> LinkBudget:
    """Assemble a full link budget for a radio configuration and link geometry.

    Args:
        config: The radio configuration.
        payload_bytes: Payload size for airtime/bitrate figures.
        tx_power_dbm: Transmit power in dBm.
        tx_gain_dbi: Transmit antenna gain in dBi.
        rx_gain_dbi: Receive antenna gain in dBi.
        noise_figure_db: Receiver noise figure in dB.
        path_loss_exponent: Exponent for the range estimate (2 = free space).
        reference_path_loss_db: A known/expected path loss to report link margin at.
        duty_limit_percent: Regional duty-cycle ceiling (percent).
        max_dwell_ms: Regional max dwell time per transmission, if any.

    Returns:
        A populated :class:`LinkBudget`.
    """
    toa = time_on_air(config, payload_bytes)
    sensitivity = receiver_sensitivity(config, noise_figure_db)
    # EIRP minus sensitivity is the most loss the path can absorb and still close.
    max_path_loss = tx_power_dbm + tx_gain_dbi + rx_gain_dbi - sensitivity
    range_km = free_space_range_km(max_path_loss, config.freq_mhz, path_loss_exponent)
    margin = (
        None if reference_path_loss_db is None else max_path_loss - reference_path_loss_db
    )
    return LinkBudget(
        config=config,
        toa=toa,
        sensitivity_dbm=sensitivity,
        tx_power_dbm=tx_power_dbm,
        tx_gain_dbi=tx_gain_dbi,
        rx_gain_dbi=rx_gain_dbi,
        max_path_loss_db=max_path_loss,
        range_km=range_km,
        path_loss_exponent=path_loss_exponent,
        link_margin_db=margin,
        duty=duty_cycle(
            toa.total_ms, limit_percent=duty_limit_percent, max_dwell_ms=max_dwell_ms
        ),
    )
