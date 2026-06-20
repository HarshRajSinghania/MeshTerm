"""Domain models shared across services, tools, persistence, and visualizations.

These are plain dataclasses with no I/O dependencies so they can be constructed by the
real device, the simulator, or rehydrated from the database identically.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Returns:
        The current time in UTC.
    """
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Contact:
    """A node known to the connected companion device.

    Attributes:
        name: Human-friendly name advertised by the node.
        public_key: Full public key hex string, if known.
        key_prefix: Short key prefix used to address the node.
        last_seen: When the node was last heard, if known.
    """

    name: str
    public_key: str = ""
    key_prefix: str = ""
    last_seen: Optional[datetime] = None


@dataclass(slots=True)
class Hop:
    """A single hop in a path trace.

    Attributes:
        index: Zero-based position of the hop along the path.
        node: Identifier of the relaying node (name or key prefix), if resolved.
        snr: Signal-to-noise ratio in dB recorded at this hop.
    """

    index: int
    node: Optional[str]
    snr: float


@dataclass(slots=True)
class TraceResult:
    """The aggregated outcome of a single path trace to a target.

    Attributes:
        target: Name or key prefix of the trace destination.
        success: Whether a trace reply was received before timeout.
        hops: Per-hop SNR readings, ordered from source to destination.
        round_trip_ms: Round-trip time of the trace in milliseconds, if measured.
        tx_power: TX power level in effect when the trace ran, if known.
        timestamp: When the trace completed.
        raw: Optional raw event payload for debugging/replay.
    """

    target: str
    success: bool
    hops: list[Hop] = field(default_factory=list)
    round_trip_ms: Optional[float] = None
    tx_power: Optional[int] = None
    timestamp: datetime = field(default_factory=utcnow)
    raw: Optional[dict] = None

    @property
    def hop_count(self) -> int:
        """Number of hops recorded in the trace."""
        return len(self.hops)

    @property
    def min_snr(self) -> Optional[float]:
        """The bottleneck (weakest) SNR along the path, or ``None`` if no hops."""
        if not self.hops:
            return None
        return min(h.snr for h in self.hops)

    @property
    def path_str(self) -> str:
        """A compact ``a -> b -> c`` rendering of the resolved path."""
        if not self.hops:
            return self.target
        nodes = [h.node or f"hop{h.index}" for h in self.hops]
        return " -> ".join(nodes)


@dataclass(slots=True)
class TraceStats:
    """Robust statistics aggregated over several traces to the same target.

    Robust (median-based) metrics are used throughout because mesh SNR readings are
    noisy and prone to outliers.

    Attributes:
        target: The trace destination these statistics describe.
        samples: Number of traces attempted.
        successes: Number of traces that returned a reply.
        median_min_snr: Median of each trace's bottleneck SNR, the headline metric.
        median_rtt_ms: Median round-trip time, if measured.
        tx_power: TX power level in effect for these samples, if fixed.
    """

    target: str
    samples: int
    successes: int
    median_min_snr: Optional[float]
    median_rtt_ms: Optional[float]
    tx_power: Optional[int] = None

    @property
    def success_rate(self) -> float:
        """Fraction of traces that returned a reply, in the range ``[0, 1]``."""
        return self.successes / self.samples if self.samples else 0.0

    @classmethod
    def from_traces(cls, target: str, traces: list[TraceResult]) -> "TraceStats":
        """Aggregate a list of traces into robust statistics.

        Args:
            target: The trace destination.
            traces: The individual trace results to aggregate.

        Returns:
            A :class:`TraceStats` summarizing the supplied traces.
        """
        successes = [t for t in traces if t.success]
        min_snrs = [t.min_snr for t in successes if t.min_snr is not None]
        rtts = [t.round_trip_ms for t in successes if t.round_trip_ms is not None]
        tx_powers = {t.tx_power for t in traces if t.tx_power is not None}
        return cls(
            target=target,
            samples=len(traces),
            successes=len(successes),
            median_min_snr=statistics.median(min_snrs) if min_snrs else None,
            median_rtt_ms=statistics.median(rtts) if rtts else None,
            tx_power=next(iter(tx_powers)) if len(tx_powers) == 1 else None,
        )


@dataclass(slots=True)
class TxLevelResult:
    """Robust measurement of one TX power level during an optimization sweep.

    Attributes:
        tx_power: The transmit power level tested.
        stats: Aggregated trace statistics at this level.
        score: Scalar objective value (higher is better) combining SNR and reliability.
    """

    tx_power: int
    stats: TraceStats
    score: float


@dataclass(slots=True)
class TxOptResult:
    """The outcome of a TX-power optimization run.

    Attributes:
        target: Node the optimization was tuned against.
        original_tx: TX power in effect before the sweep (restored afterward).
        best_tx: TX power with the highest score.
        best_score: The winning score.
        applied: Whether ``best_tx`` was written back to the device.
        levels: Every level measured, in the order tested.
    """

    target: str
    original_tx: Optional[int]
    best_tx: int
    best_score: float
    applied: bool
    levels: list[TxLevelResult] = field(default_factory=list)

    @property
    def best_level(self) -> Optional[TxLevelResult]:
        """The :class:`TxLevelResult` for ``best_tx``, if present."""
        return next((lv for lv in self.levels if lv.tx_power == self.best_tx), None)

    def sorted_by_tx(self) -> list[TxLevelResult]:
        """Return measured levels sorted ascending by TX power.

        Returns:
            The levels ordered by TX power, suitable for plotting.
        """
        return sorted(self.levels, key=lambda lv: lv.tx_power)
