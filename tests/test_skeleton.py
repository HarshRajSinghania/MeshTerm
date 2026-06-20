"""Smoke tests for the skeleton: models, mock device, service, and persistence.

These run without hardware against the :class:`MockDevice` simulator.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshtools.core.connection import MockDevice, clamp_tx_power
from meshtools.core.models import TraceResult, TraceStats
from meshtools.persistence.repository import Repository
from meshtools.services import trace_runner


def test_clamp_tx_power() -> None:
    """TX power clamps to the supported range."""
    assert clamp_tx_power(-5) == 1
    assert clamp_tx_power(99) == 22
    assert clamp_tx_power(14) == 14


def test_trace_stats_aggregation() -> None:
    """Robust stats reflect successes and bottleneck SNR."""
    from meshtools.core.models import Hop

    traces = [
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 5.0), Hop(1, "b", -2.0)]),
        TraceResult(target="x", success=False),
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 7.0), Hop(1, "b", 0.0)]),
    ]
    stats = TraceStats.from_traces("x", traces)
    assert stats.samples == 3
    assert stats.successes == 2
    assert stats.success_rate == pytest.approx(2 / 3)
    assert stats.median_min_snr == pytest.approx(-1.0)  # median of [-2.0, 0.0]


async def test_mock_device_trace_is_unimodal_in_tx() -> None:
    """The simulator peaks near its optimal TX power (averaged over noise)."""
    device = MockDevice(optimal_tx=14)
    await device.connect()

    async def avg_snr(tx: int) -> float:
        await device.set_tx_power(tx)
        stats = await trace_runner.measure(device, "Alice", samples=15, cooldown_s=0)
        return stats.median_min_snr or -99.0

    low, peak, high = await avg_snr(2), await avg_snr(14), await avg_snr(22)
    assert peak > low
    assert peak > high


async def test_tx_optimizer_finds_simulator_peak(tmp_path: Path) -> None:
    """The optimizer converges near the simulator's known optimal TX power."""
    from meshtools.services import tx_optimizer

    device = MockDevice(optimal_tx=14)
    await device.connect()
    await device.set_tx_power(20)

    result = await tx_optimizer.optimize_tx_power(
        device, "Alice", samples_per_level=12, coarse_step=3, cooldown_s=0
    )
    assert abs(result.best_tx - 14) <= 2  # within a step of the true peak
    assert result.original_tx == 20
    assert await device.get_tx_power() == 20  # original restored, not the winner
    assert not result.applied


def test_tx_plot_writes_html(tmp_path: Path) -> None:
    """The Plotly renderer produces a self-contained HTML file."""
    import asyncio

    from meshtools.services import tx_optimizer
    from meshtools.viz.tx_plot import render_tx_optimization

    async def _build():
        device = MockDevice(optimal_tx=14)
        await device.connect()
        return await tx_optimizer.optimize_tx_power(
            device, "Alice", samples_per_level=4, coarse_step=4, refine=False, cooldown_s=0
        )

    result = asyncio.run(_build())
    path = render_tx_optimization(result, tmp_path)
    assert path.exists()
    assert path.suffix == ".html"
    assert path.stat().st_size > 1000  # plotly.js inlined → non-trivial size


async def test_repository_round_trip(tmp_path: Path) -> None:
    """A run and its traces persist and read back."""
    repo = Repository(tmp_path / "test.db")
    run_id = repo.start_run("trace", {"target": "Alice", "samples": 2})

    device = MockDevice()
    await device.connect()
    results = await trace_runner.run_traces(
        device, "Alice", samples=2, cooldown_s=0, persist=lambda t: repo.record_trace(run_id, t)
    )
    repo.finish_run(run_id, "ok", {"count": len(results)})

    runs = repo.list_runs()
    assert runs[0].tool == "trace"
    assert runs[0].status == "ok"
    repo.close()
