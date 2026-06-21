"""Link-quality regression detection against a rolling baseline.

The trace and TX tools already persist every measurement, so a link's *history* is on
disk. This service turns that history into an answer to "is this link worse than it
usually is?" — it splits a target's traces into a recent window and an older baseline,
then flags end-to-end and per-hop metrics that have degraded beyond a threshold (e.g. the
yagi→local hop sitting 6 dB below its 30-day median, or the success rate falling off).

It is pure logic over :class:`~meshtools.core.models.TraceResult` lists, so it runs
identically on live data, replayed history, or the simulator.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.models import LOCAL_DEVICE_LABEL, TraceResult, TraceStats

#: Default SNR drop (dB) from baseline that counts as a regression.
DEFAULT_SNR_DROP_DB = 3.0
#: Default success-rate drop (fraction) from baseline that counts as a regression.
DEFAULT_SUCCESS_DROP = 0.2
#: Traces taken as the "recent" window (the rest form the baseline).
DEFAULT_RECENT_COUNT = 3
#: Minimum baseline traces required before a verdict is trustworthy.
DEFAULT_MIN_BASELINE = 3

NodeResolver = Callable[[Optional[str]], Optional[str]]


@dataclass(slots=True)
class MetricChange:
    """A single metric compared between the baseline and recent windows.

    Attributes:
        subject: What the metric describes (the target, or a ``a → b`` link).
        metric: Human-readable metric name (e.g. ``min SNR``).
        kind: ``target`` for an end-to-end metric, ``link`` for one hop.
        baseline: The baseline-window value.
        recent: The recent-window value.
        baseline_n: Number of baseline samples behind ``baseline``.
        recent_n: Number of recent samples behind ``recent``.
        threshold: The drop magnitude that marks a regression.
        regressed: Whether ``recent`` is worse than ``baseline`` past ``threshold``.
    """

    subject: str
    metric: str
    kind: str
    baseline: float
    recent: float
    baseline_n: int
    recent_n: int
    threshold: float
    regressed: bool

    @property
    def delta(self) -> float:
        """Signed change ``recent - baseline`` (negative means degraded)."""
        return self.recent - self.baseline

    @property
    def severity(self) -> str:
        """``critical`` when the drop is at least twice the threshold, else ``warn``/``ok``."""
        if not self.regressed:
            return "ok"
        return "critical" if -self.delta >= 2 * self.threshold else "warn"


@dataclass(slots=True)
class HealthReport:
    """The link-quality verdict for one target.

    Attributes:
        target: The trace destination analyzed.
        baseline_n: Traces in the baseline window.
        recent_n: Traces in the recent window.
        changes: Every metric evaluated (target-level and per-link).
    """

    target: str
    baseline_n: int
    recent_n: int
    changes: list[MetricChange] = field(default_factory=list)

    @property
    def regressions(self) -> list[MetricChange]:
        """The subset of ``changes`` that actually regressed, worst drop first."""
        return sorted(
            (c for c in self.changes if c.regressed), key=lambda c: c.delta
        )

    @property
    def status(self) -> str:
        """``insufficient-data``, ``ok``, ``degraded`` (warn), or ``critical``."""
        if not self.changes:
            return "insufficient-data"
        regs = self.regressions
        if not regs:
            return "ok"
        if any(c.severity == "critical" for c in regs):
            return "critical"
        return "degraded"


def analyze_target(
    target: str,
    traces: list[TraceResult],
    *,
    recent_count: int = DEFAULT_RECENT_COUNT,
    min_baseline: int = DEFAULT_MIN_BASELINE,
    snr_drop_db: float = DEFAULT_SNR_DROP_DB,
    success_drop: float = DEFAULT_SUCCESS_DROP,
    resolve: Optional[NodeResolver] = None,
    device_label: str = LOCAL_DEVICE_LABEL,
) -> HealthReport:
    """Detect link-quality regressions for ``target`` from its trace history.

    The traces are split by recency: the newest ``recent_count`` form the recent window,
    the remainder the baseline. End-to-end bottleneck SNR and success rate are compared,
    plus the median SNR of each per-hop link that appears in both windows.

    Args:
        target: The trace destination.
        traces: Its trace history (any order; partitioned here by timestamp).
        recent_count: Number of newest traces treated as the recent window.
        min_baseline: Minimum baseline traces required for a verdict.
        snr_drop_db: SNR drop (dB) from baseline that flags a regression.
        success_drop: Success-rate drop (fraction) that flags a regression.
        resolve: Optional resolver mapping a hop hash to a friendly node name.
        device_label: Label for our own device at the path endpoints.

    Returns:
        A :class:`HealthReport`. With too little history its ``changes`` is empty and
        ``status`` is ``insufficient-data``.
    """
    ordered = sorted(traces, key=lambda t: t.timestamp, reverse=True)
    recent = ordered[:recent_count]
    baseline = ordered[recent_count:]
    if len(recent) < 2 or len(baseline) < min_baseline:
        return HealthReport(target=target, baseline_n=len(baseline), recent_n=len(recent))

    base_stats = TraceStats.from_traces(target, baseline)
    recent_stats = TraceStats.from_traces(target, recent)
    changes: list[MetricChange] = []

    if base_stats.median_min_snr is not None and recent_stats.median_min_snr is not None:
        changes.append(
            _change(
                subject=target,
                metric="min SNR (dB)",
                kind="target",
                baseline=base_stats.median_min_snr,
                recent=recent_stats.median_min_snr,
                baseline_n=base_stats.successes,
                recent_n=recent_stats.successes,
                threshold=snr_drop_db,
            )
        )

    changes.append(
        _change(
            subject=target,
            metric="success rate",
            kind="target",
            baseline=base_stats.success_rate,
            recent=recent_stats.success_rate,
            baseline_n=base_stats.samples,
            recent_n=recent_stats.samples,
            threshold=success_drop,
        )
    )

    changes.extend(
        _link_changes(baseline, recent, snr_drop_db, resolve, device_label)
    )
    return HealthReport(
        target=target, baseline_n=len(baseline), recent_n=len(recent), changes=changes
    )


def _change(
    *,
    subject: str,
    metric: str,
    kind: str,
    baseline: float,
    recent: float,
    baseline_n: int,
    recent_n: int,
    threshold: float,
) -> MetricChange:
    """Build a :class:`MetricChange`, flagging it regressed on a drop past ``threshold``.

    Both metrics here are "higher is better" (SNR, success rate), so a regression is a
    decrease of at least ``threshold``.

    Args:
        subject: What the metric describes.
        metric: Metric name.
        kind: ``target`` or ``link``.
        baseline: Baseline value.
        recent: Recent value.
        baseline_n: Baseline sample count.
        recent_n: Recent sample count.
        threshold: Drop magnitude that marks a regression.

    Returns:
        The populated :class:`MetricChange`.
    """
    regressed = (baseline - recent) >= threshold
    return MetricChange(
        subject=subject,
        metric=metric,
        kind=kind,
        baseline=baseline,
        recent=recent,
        baseline_n=baseline_n,
        recent_n=recent_n,
        threshold=threshold,
        regressed=regressed,
    )


def _link_changes(
    baseline: list[TraceResult],
    recent: list[TraceResult],
    snr_drop_db: float,
    resolve: Optional[NodeResolver],
    device_label: str,
) -> list[MetricChange]:
    """Compare per-hop link SNR between the two windows.

    Only links present in *both* windows are compared, so a transient path that appears
    in just one window doesn't masquerade as a regression.

    Args:
        baseline: Baseline-window traces.
        recent: Recent-window traces.
        snr_drop_db: SNR drop (dB) that flags a link regression.
        resolve: Optional hop-hash → name resolver for the link label.
        device_label: Label for our own device.

    Returns:
        One :class:`MetricChange` per shared link, worst (most negative) drop first.
    """
    base_links = _link_snrs(baseline, resolve, device_label)
    recent_links = _link_snrs(recent, resolve, device_label)
    changes: list[MetricChange] = []
    for link in base_links.keys() & recent_links.keys():
        base_snrs = base_links[link]
        recent_snrs = recent_links[link]
        changes.append(
            _change(
                subject=link,
                metric="link SNR (dB)",
                kind="link",
                baseline=statistics.median(base_snrs),
                recent=statistics.median(recent_snrs),
                baseline_n=len(base_snrs),
                recent_n=len(recent_snrs),
                threshold=snr_drop_db,
            )
        )
    return sorted(changes, key=lambda c: c.delta)


def _link_snrs(
    traces: list[TraceResult], resolve: Optional[NodeResolver], device_label: str
) -> dict[str, list[float]]:
    """Collect per-link SNR samples keyed by an ``origin → destination`` label.

    Args:
        traces: The traces to read edges from.
        resolve: Optional hop-hash → name resolver.
        device_label: Label for our own device at the endpoints.

    Returns:
        A mapping of link label to the SNR readings seen on it.
    """
    out: dict[str, list[float]] = {}
    for trace in traces:
        if not trace.success:
            continue
        for edge in trace.edges(device_label):
            label = f"{_name(edge.origin, resolve)} → {_name(edge.destination, resolve)}"
            out.setdefault(label, []).append(edge.snr)
    return out


def _name(label: str, resolve: Optional[NodeResolver]) -> str:
    """Resolve a hop label to a friendly name when a resolver is supplied."""
    if resolve is None:
        return label
    return resolve(label) or label
