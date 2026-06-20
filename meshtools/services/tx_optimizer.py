"""TX-power optimization.

The transmit-power response is non-monotonic: too low and the signal sits in the noise
floor, too high and the receiver saturates. The optimum is therefore an interior peak,
and trace measurements are noisy, so this module uses a robust two-phase search:

1. A **coarse sweep** across the TX range at a fixed step, taking several traces per
   level and scoring each level by a reliability-weighted median bottleneck SNR.
2. A **local refine** that fills in every integer level around the coarse winner, so the
   reported optimum is exact within the bracket without paying for a full fine sweep.

The device's original TX power is always restored when the sweep finishes; writing the
winner back is the caller's decision.
"""

from __future__ import annotations

from typing import Callable, Optional

from ..core.connection import Device, clamp_tx_power
from ..core.models import TraceStats, TxLevelResult, TxOptResult
from . import trace_runner

#: How many dB of SNR a fully-unreliable link is penalized, blending reliability into
#: the scalar objective so a strong-but-flaky level cannot beat a solid one.
RELIABILITY_PENALTY_DB = 10.0

LevelCallback = Callable[[int, int, TxLevelResult], None]
PersistLevel = Callable[[TraceStats], None]


def score_level(stats: TraceStats) -> float:
    """Score a measured TX level; higher is better.

    Combines the robust bottleneck SNR with the success rate so an intermittently
    failing level cannot outrank a reliable one.

    Args:
        stats: Aggregated trace statistics for the level.

    Returns:
        The scalar objective value, or negative infinity if nothing got through.
    """
    if stats.successes == 0 or stats.median_min_snr is None:
        return float("-inf")
    return stats.median_min_snr - (1.0 - stats.success_rate) * RELIABILITY_PENALTY_DB


async def optimize_tx_power(
    device: Device,
    target: str,
    *,
    tx_min: int = 1,
    tx_max: int = 22,
    coarse_step: int = 3,
    samples_per_level: int = 5,
    refine: bool = True,
    cooldown_s: float = 1.0,
    on_level: Optional[LevelCallback] = None,
    persist_level: Optional[PersistLevel] = None,
    persist_trace: Optional[Callable] = None,
) -> TxOptResult:
    """Search for the TX power that maximizes the reliability-weighted SNR to ``target``.

    Args:
        device: Connected device to tune.
        target: Destination node name or key prefix.
        tx_min: Lowest TX power to consider (clamped to the device range).
        tx_max: Highest TX power to consider (clamped to the device range).
        coarse_step: Step between coarse-sweep levels.
        samples_per_level: Traces averaged at each level.
        refine: Whether to fill in integer levels around the coarse winner.
        cooldown_s: Delay between individual traces (duty-cycle safety).
        on_level: Optional progress callback ``(completed, total, level_result)``.
        persist_level: Optional callback to store each level's aggregated stats.
        persist_trace: Optional callback to store each individual trace.

    Returns:
        A :class:`TxOptResult`. The device is left at its original TX power.

    Raises:
        ValueError: If the resolved TX range is empty.
    """
    tx_min = clamp_tx_power(tx_min)
    tx_max = clamp_tx_power(tx_max)
    if tx_min > tx_max:
        raise ValueError(f"Empty TX range: {tx_min}..{tx_max}")

    original_tx = await device.get_tx_power()
    measured: dict[int, TxLevelResult] = {}

    # Phase 1 + 2 share this planner so progress totals stay accurate.
    coarse = _coarse_levels(tx_min, tx_max, coarse_step)
    total_estimate = len(coarse) + (2 * coarse_step if refine else 0)
    completed = 0

    async def measure_level(tx: int) -> TxLevelResult:
        nonlocal completed
        await device.set_tx_power(tx)
        stats = await trace_runner.measure(
            device,
            target,
            samples=samples_per_level,
            cooldown_s=cooldown_s,
            persist=persist_trace,
        )
        level = TxLevelResult(tx_power=tx, stats=stats, score=score_level(stats))
        measured[tx] = level
        if persist_level is not None:
            persist_level(stats)
        completed += 1
        if on_level is not None:
            on_level(completed, max(total_estimate, completed), level)
        return level

    try:
        for tx in coarse:
            await measure_level(tx)

        best_tx = max(measured, key=lambda t: measured[t].score)

        if refine:
            lo = clamp_tx_power(best_tx - coarse_step + 1)
            hi = clamp_tx_power(best_tx + coarse_step - 1)
            for tx in range(lo, hi + 1):
                if tx not in measured:
                    await measure_level(tx)
            best_tx = max(measured, key=lambda t: measured[t].score)
    finally:
        if original_tx is not None:
            await device.set_tx_power(original_tx)

    return TxOptResult(
        target=target,
        original_tx=original_tx,
        best_tx=best_tx,
        best_score=measured[best_tx].score,
        applied=False,
        levels=list(measured.values()),
    )


def _coarse_levels(tx_min: int, tx_max: int, step: int) -> list[int]:
    """Build the coarse sweep grid, always including both endpoints.

    Args:
        tx_min: Lowest TX power.
        tx_max: Highest TX power.
        step: Grid spacing.

    Returns:
        The ascending list of TX levels to sample in the coarse phase.
    """
    levels = list(range(tx_min, tx_max + 1, max(1, step)))
    if levels[-1] != tx_max:
        levels.append(tx_max)
    return levels
