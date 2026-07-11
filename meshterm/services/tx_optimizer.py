"""Remote-admin TX-power optimization.

This tunes the transmit power of a *remote* node we have admin rights on — the node
sitting one hop before a chosen target — to maximize the signal the **target** receives
from it. We force a path (exactly like the trace tool), so every measurement exercises the
same admin-node→target link, and read the SNR the target reports at the end of that path.

Radio links are noisy and the real-world response is non-monotonic (too little power and
the target can't hear it; too much and its front end saturates), so the search is robust
by design:

1. A **coarse sweep** across the TX range, running several traces per level and scoring
   each level by its trace success rate first and a *median* target SNR second.
2. A **local refine** filling in integer levels around the coarse winner.
3. A **verify** pass that re-measures the leading candidate with extra samples so a single
   lucky (or unlucky) reading can't decide the optimum.

Selection is lexicographic — **reliability first, then SNR** — and on a near-tie in SNR it
prefers the *lower* TX power, since cranking power for a fraction of a dB only adds
interference and burns duty cycle. The chosen optimum is written back to the admin node
when ``apply`` is set (the whole point of the run is to leave it tuned).
"""

from __future__ import annotations

import statistics
from typing import Callable, Optional

from ..core.connection import REMOTE_TX_MAX, REMOTE_TX_MIN, Device
from ..core.models import Contact, TraceResult, TraceStats, TxLevelResult, TxOptResult
from . import trace_runner

LevelCallback = Callable[[int, int, TxLevelResult], None]
PersistLevel = Callable[[TxLevelResult], None]
PhaseCallback = Callable[[str], None]

#: The search phases, in order, as reported to ``on_phase``: the coarse grid sweep, the
#: integer fill-in around the coarse winner, and the extra-samples re-measure of the leader.
PHASES = ("coarse", "refine", "verify")

#: On a near-tie in target SNR (within this many dB of the best), the lower TX power wins.
DEFAULT_SNR_TOLERANCE_DB = 1.0


def trace_target_snr(trace: TraceResult, target_hash: str) -> Optional[float]:
    """Return the SNR the *target* received on a single (round-trip) trace.

    Because we trace out to the target and back (so a reachable node, not the far
    target, answers), the target is no longer the last hop — it's the turn-around point.
    We therefore locate it by matching its hash rather than by position. The matched
    hop's SNR is the signal the target heard from the admin node just before it, which is
    exactly the link being tuned.

    Args:
        trace: A single trace result.
        target_hash: The target node's path hash (a leading slice of its public key).

    Returns:
        The target's received SNR in dB, or ``None`` if the trace failed or the target
        hop wasn't present in the reply.
    """
    if not trace.success:
        return None
    needle = target_hash.lower().removeprefix("0x")
    for hop in trace.hops:
        if hop.node is None:
            continue
        node = hop.node.lower().removeprefix("0x")
        # Match either way: the firmware may report hashes at a different width than the
        # one we addressed the path with.
        if node.startswith(needle) or needle.startswith(node):
            return hop.snr
    return None


def build_level(
    tx: int, target: str, target_hash: str, traces: list[TraceResult]
) -> TxLevelResult:
    """Aggregate the traces measured at one TX level into a :class:`TxLevelResult`.

    Args:
        tx: The TX power these traces were taken at.
        target: The target node (for the embedded :class:`TraceStats`).
        target_hash: The target's path hash, used to find its hop in each trace.
        traces: Every trace run at this level.

    Returns:
        The level's robust aggregate. ``target_snr`` is the median across successful
        traces, so a single outlier reading barely moves it.
    """
    successes = [t for t in traces if t.success]
    snrs = [snr for t in successes if (snr := trace_target_snr(t, target_hash)) is not None]
    target_snr = statistics.median(snrs) if snrs else None
    return TxLevelResult(
        tx_power=tx,
        samples=len(traces),
        successes=len(successes),
        target_snr=target_snr,
        score=target_snr if target_snr is not None else float("-inf"),
        stats=TraceStats.from_traces(target, traces),
    )


def select_best(
    levels: list[TxLevelResult], *, snr_tolerance: float = DEFAULT_SNR_TOLERANCE_DB
) -> TxLevelResult:
    """Pick the optimal level: reliability first, then SNR, then the lowest TX.

    Among the levels with the highest trace success rate, take those whose median target
    SNR is within ``snr_tolerance`` of the best, and return the one using the least
    power. The tolerance band is what makes the choice robust to measurement noise: a
    fractionally-higher SNR from a hotter level doesn't win if a cooler one is within
    spitting distance.

    Args:
        levels: The measured levels (must be non-empty).
        snr_tolerance: dB band within which SNRs are treated as tied.

    Returns:
        The chosen :class:`TxLevelResult`.
    """
    best_rate = max(lv.success_rate for lv in levels)
    contenders = [lv for lv in levels if lv.success_rate >= best_rate - 1e-9]
    best_snr = max(_snr_or_floor(lv) for lv in contenders)
    near = [lv for lv in contenders if _snr_or_floor(lv) >= best_snr - snr_tolerance]
    return min(near, key=lambda lv: lv.tx_power)


def _snr_or_floor(level: TxLevelResult) -> float:
    """Return a level's target SNR, or negative infinity if it never got through."""
    return level.target_snr if level.target_snr is not None else float("-inf")


async def optimize_tx_power(
    device: Device,
    target: str,
    admin_node: Contact,
    path: str,
    *,
    tx_min: int = REMOTE_TX_MIN,
    tx_max: int = REMOTE_TX_MAX,
    coarse_step: int = 3,
    samples_per_level: int = 3,
    refine: bool = True,
    verify: bool = True,
    apply: bool = True,
    cooldown_s: float = 1.0,
    snr_tolerance: float = DEFAULT_SNR_TOLERANCE_DB,
    on_level: Optional[LevelCallback] = None,
    on_phase: Optional[PhaseCallback] = None,
    persist_level: Optional[PersistLevel] = None,
    persist_trace: Optional[Callable] = None,
) -> TxOptResult:
    """Tune ``admin_node``'s TX power for the best signal at ``target``.

    The caller must already be logged in to ``admin_node`` (see
    :meth:`~meshterm.core.connection.Device.admin_login`).

    Args:
        device: The connected local device, used to trace and to drive the remote node.
        target: Node whose received SNR is being maximized (the last hop of ``path``).
        admin_node: The remote node whose TX power is tuned (the hop before ``target``).
        path: The one-way forced path out to ``target`` (comma-separated hashes, ending
            at the target). It is traced as a there-and-back round trip internally so a
            reachable node answers — the far target only has to forward the packet.
        tx_min: Lowest TX power to consider.
        tx_max: Highest TX power to consider.
        coarse_step: Step between coarse-sweep levels.
        samples_per_level: Traces averaged at each level (and added again on verify).
        refine: Whether to fill in integer levels around the coarse winner.
        verify: Whether to re-measure the leading candidate to reject an outlier.
        apply: Leave the winning TX power on the node when done (otherwise restore the
            power it had before the sweep, if that could be read).
        cooldown_s: Delay between individual traces (duty-cycle safety).
        snr_tolerance: dB band for the lower-power tie-break (see :func:`select_best`).
        on_level: Optional progress callback ``(completed, total, level_result)``.
        on_phase: Optional callback announcing each search phase as it begins (one of
            :data:`PHASES`), so a live view can say *what kind* of measuring is happening.
        persist_level: Optional callback to store each level's aggregated result.
        persist_trace: Optional callback to store each individual trace.

    Returns:
        A :class:`TxOptResult`.

    Raises:
        ValueError: If the resolved TX range is empty.
    """
    if tx_min > tx_max:
        raise ValueError(f"Empty TX range: {tx_min}..{tx_max}")

    original_tx = await device.get_remote_tx_power(admin_node)

    # Trace out to the target and back: the reply then originates at a node near us
    # (the first hop), not the far target, which only has to forward the packet. The
    # target's received SNR is read from its hop at the round trip's turn-around point.
    outbound = [h for h in path.split(",") if h]
    target_hash = outbound[-1] if outbound else path
    trace_path = _round_trip_path(outbound)

    # Accumulate raw traces per level so a verify pass can *add* samples to a level and
    # re-aggregate, rather than throwing away what we already measured.
    traces_by_tx: dict[int, list[TraceResult]] = {}
    levels: dict[int, TxLevelResult] = {}

    coarse = coarse_levels(tx_min, tx_max, coarse_step)
    total_estimate = len(coarse) + (2 * coarse_step if refine else 0) + (1 if verify else 0)
    completed = 0

    async def measure_level(tx: int) -> TxLevelResult:
        """Run a batch of traces at ``tx`` (accumulating) and refresh its aggregate."""
        nonlocal completed
        await device.set_remote_tx_power(admin_node, tx)
        batch = await trace_runner.run_traces(
            device,
            target,
            samples=samples_per_level,
            path=trace_path,
            cooldown_s=cooldown_s,
            persist=persist_trace,
        )
        traces_by_tx.setdefault(tx, []).extend(batch)
        level = build_level(tx, target, target_hash, traces_by_tx[tx])
        levels[tx] = level
        if persist_level is not None:
            persist_level(level)
        completed += 1
        if on_level is not None:
            on_level(completed, max(total_estimate, completed), level)
        return level

    try:
        if on_phase is not None:
            on_phase("coarse")
        for tx in coarse:
            await measure_level(tx)

        best = select_best(list(levels.values()), snr_tolerance=snr_tolerance)

        if refine:
            if on_phase is not None:
                on_phase("refine")
            lo = max(tx_min, best.tx_power - coarse_step + 1)
            hi = min(tx_max, best.tx_power + coarse_step - 1)
            for tx in range(lo, hi + 1):
                if tx not in levels:
                    await measure_level(tx)
            best = select_best(list(levels.values()), snr_tolerance=snr_tolerance)

        if verify:
            # Re-measure the leader with extra samples; if it was an outlier the larger
            # sample will pull it back and a steadier neighbor can take over.
            if on_phase is not None:
                on_phase("verify")
            await measure_level(best.tx_power)
            best = select_best(list(levels.values()), snr_tolerance=snr_tolerance)

        # "No result" = not one trace got through at any level (a meaningless winner).
        # In that case, and when apply is off, leave the node at the power it started at.
        got_result = best.successes > 0
        applied = apply and got_result
        if applied:
            final_tx = best.tx_power
        elif original_tx is not None:
            final_tx = original_tx
        else:
            final_tx = best.tx_power  # nothing to restore to; can't do better
        await device.set_remote_tx_power(admin_node, final_tx)
    except BaseException:
        # On any failure, try to leave the node at the power it started with.
        if original_tx is not None:
            try:
                await device.set_remote_tx_power(admin_node, original_tx)
            except Exception:  # noqa: BLE001 - best-effort restore; don't mask the cause
                pass
        raise

    return TxOptResult(
        target=target,
        admin_node=admin_node.name,
        path=path,
        original_tx=original_tx,
        best_tx=best.tx_power,
        best_snr=best.target_snr,
        best_success_rate=best.success_rate,
        applied=applied,
        levels=list(levels.values()),
    )


def _round_trip_path(outbound: list[str]) -> str:
    """Build a there-and-back trace path from a one-way path to the target.

    Tracing out to the target then back means the *final* hop is a node near us (the
    first outbound hop), which can reliably answer; the far target only has to forward
    the packet, never originate the reply. This mirrors the MeshCore ``A,B,A`` trace
    convention. The target's own hop (the turn-around point) still records the SNR it
    heard from the admin node, which is what we read.

    Args:
        outbound: The one-way path hops, ending at the target.

    Returns:
        The round-trip path as a comma-separated hash string (e.g. ``"3f,f2"`` becomes
        ``"3f,f2,3f"``). A single-hop path is returned unchanged.
    """
    if len(outbound) < 2:
        return ",".join(outbound)
    return ",".join(outbound + outbound[-2::-1])


def coarse_levels(tx_min: int, tx_max: int, step: int) -> list[int]:
    """Build the coarse sweep grid, always including both endpoints.

    Public so the live sweep screen can quote the worst-case transmission count
    of a commit before anything goes on the air.

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
