"""Multi-path probing: measure candidate routes to a target and rank what actually works.

The topology graph (:mod:`~meshterm.services.topology`) proposes routes from *received*
evidence; this module puts them to the test. Each candidate outbound path is traced once
(by default — repeaters penalize, and can blacklist, nodes that burst traffic), every
trace is persisted exactly like a normal trace run, the per-candidate aggregate is
recorded as a ``path_candidates`` row, and the outcomes come back ranked the way the TX
optimizer ranks its levels: reliability first, bottleneck SNR as the tie-breaker, then
round-trip time. The caller (the live trace screen) shows the ranking and offers to adopt
the winner — measurement proposes, the user disposes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..core.connection import Device
from ..core.models import TraceResult, TraceStats

ProbeProgress = Callable[[int, int, TraceResult], None]
PersistTrace = Callable[[TraceResult], None]
PersistCandidate = Callable[["ProbeOutcome"], None]


@dataclass(slots=True)
class ProbeCandidate:
    """One route to measure.

    Attributes:
        label: Short description of where the route came from (shown in the results).
        spec: The forced-path spec to trace — comma-separated hex hashes, outbound
            only, ending at the target's own hash (the reply retraces it in reverse).
    """

    label: str
    spec: str


@dataclass(slots=True)
class ProbeOutcome:
    """One candidate's measured result.

    Attributes:
        candidate: The route that was measured.
        stats: The aggregated trace statistics over its traces.
    """

    candidate: ProbeCandidate
    stats: TraceStats

    @property
    def sort_key(self) -> tuple:
        """A ranking key that sorts the best path first.

        Reliability, then bottleneck SNR, then RTT — the same priority order the TX
        optimizer applies.
        """
        snr = self.stats.median_min_snr
        rtt = self.stats.median_rtt_ms
        return (
            -self.stats.success_rate,
            -(snr if snr is not None else float("-inf")),
            rtt if rtt is not None else float("inf"),
        )


async def probe_paths(
    device: Device,
    target: str,
    candidates: list[ProbeCandidate],
    *,
    samples: int = 1,
    cooldown_s: float = 1.0,
    on_result: ProbeProgress | None = None,
    persist_trace: Callable[[TraceResult], Awaitable[None] | None] | None = None,
    persist_candidate: PersistCandidate | None = None,
) -> list[ProbeOutcome]:
    """Trace every candidate path and return the outcomes ranked best-first.

    Runs the candidates sequentially (one radio, duty cycle applies), a single trace
    each by default. Persistence is delegated through callbacks so this stays pure
    measurement: the caller owns the run row and decides where traces and candidate
    aggregates go. Cancelling the surrounding task stops mid-candidate; everything
    persisted so far stays persisted.

    Args:
        device: The connected device to trace through.
        target: The destination (for :class:`TraceStats` labelling).
        candidates: The routes to measure, in the order to try them.
        samples: Traces per candidate. The default of one is deliberate — repeaters
            can blacklist nodes that burst traffic; raise it only when the airtime
            budget clearly allows.
        cooldown_s: Pause between traces (and between candidates).
        on_result: Optional callback ``(candidate_index, done_in_candidate, result)``
            invoked as each trace lands, e.g. to advance the probe dialog.
        persist_trace: Optional per-trace persistence callback (sync or async).
        persist_candidate: Optional callback invoked with each candidate's finished
            :class:`ProbeOutcome` (e.g. to record a ``path_candidates`` row).

    Returns:
        One :class:`ProbeOutcome` per candidate, ranked by :attr:`ProbeOutcome.sort_key`.
    """
    from . import trace_runner

    outcomes: list[ProbeOutcome] = []
    for index, candidate in enumerate(candidates):
        if index and cooldown_s > 0:
            # ``run_traces`` paces *between* its own samples and never after the last
            # one, so at the default of one trace per candidate it paces nothing at
            # all: without this the sweep walks every candidate back to back and the
            # repeaters see a burst — the traffic pattern that gets a node ignored,
            # after which every later trace comes home empty.
            await asyncio.sleep(cooldown_s)
        results = await trace_runner.run_traces(
            device,
            target,
            samples=samples,
            path=candidate.spec,
            cooldown_s=cooldown_s,
            on_result=(
                (lambda done, _total, result, i=index: on_result(i, done, result))
                if on_result is not None
                else None
            ),
            persist=persist_trace,
        )
        outcome = ProbeOutcome(
            candidate=candidate, stats=TraceStats.from_traces(target, results)
        )
        outcomes.append(outcome)
        if persist_candidate is not None:
            persist_candidate(outcome)
    return sorted(outcomes, key=lambda o: o.sort_key)
