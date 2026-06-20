"""Repeated-trace execution with robust aggregation and duty-cycle pacing.

This is the measurement primitive both the trace tool and the TX/path optimizers build
on: run a trace N times, optionally reporting progress, and aggregate into robust
statistics while persisting every individual trace.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from ..core.connection import Device
from ..core.models import TraceResult, TraceStats

ProgressCallback = Callable[[int, int, TraceResult], None]


async def run_traces(
    device: Device,
    target: str,
    *,
    samples: int = 5,
    path: Optional[list[str]] = None,
    cooldown_s: float = 1.0,
    timeout: float = 10.0,
    on_result: Optional[ProgressCallback] = None,
    persist: Optional[Callable[[TraceResult], Awaitable[None] | None]] = None,
) -> list[TraceResult]:
    """Run ``samples`` traces to ``target`` with pacing between transmissions.

    Args:
        device: The connected device to trace through.
        target: Destination node name or key prefix.
        samples: Number of traces to run.
        path: Optional explicit path to force on every trace.
        cooldown_s: Delay between traces to respect radio duty cycle.
        timeout: Per-trace reply timeout in seconds.
        on_result: Optional callback invoked as ``(completed, total, result)`` after
            each trace, e.g. to advance a progress bar.
        persist: Optional callback to store each trace (sync or async).

    Returns:
        The list of individual :class:`TraceResult` objects, in order.
    """
    results: list[TraceResult] = []
    for i in range(samples):
        result = await device.run_trace(target, path=path, timeout=timeout)
        results.append(result)

        if persist is not None:
            maybe = persist(result)
            if asyncio.iscoroutine(maybe):
                await maybe
        if on_result is not None:
            on_result(i + 1, samples, result)

        if i < samples - 1 and cooldown_s > 0:
            await asyncio.sleep(cooldown_s)
    return results


async def measure(
    device: Device,
    target: str,
    *,
    samples: int = 5,
    path: Optional[list[str]] = None,
    cooldown_s: float = 1.0,
    on_result: Optional[ProgressCallback] = None,
    persist: Optional[Callable[[TraceResult], Awaitable[None] | None]] = None,
) -> TraceStats:
    """Run traces and return their robust aggregate statistics.

    Args:
        device: The connected device to trace through.
        target: Destination node name or key prefix.
        samples: Number of traces to run.
        path: Optional explicit path to force.
        cooldown_s: Delay between traces.
        on_result: Optional per-trace progress callback.
        persist: Optional per-trace persistence callback.

    Returns:
        A :class:`TraceStats` summarizing the run.
    """
    results = await run_traces(
        device,
        target,
        samples=samples,
        path=path,
        cooldown_s=cooldown_s,
        on_result=on_result,
        persist=persist,
    )
    return TraceStats.from_traces(target, results)
