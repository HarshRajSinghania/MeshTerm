"""Domain models shared across services, tools, persistence, and visualizations.

These are plain dataclasses with no I/O dependencies so they can be constructed by the
real device, the simulator, or rehydrated from the database identically.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

#: Label used for our own (local) device when framing a trace path's endpoints.
LOCAL_DEVICE_LABEL = "us"


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
class Observation:
    """A single over-the-air reception heard while passively monitoring the mesh.

    Adverts, telemetry frames, and other packets the companion overhears are captured as
    observations and logged longitudinally, so the mesh's behavior can be reviewed over
    time rather than only at the instant a command runs.

    Attributes:
        node: Key prefix / hash identifying the transmitting node, if known.
        name: Friendly name advertised by the node, if carried.
        kind: Packet class, e.g. ``advert`` or ``telemetry``.
        snr: Signal-to-noise ratio (dB) the companion measured, if reported.
        rssi: Received signal strength (dBm), if reported.
        lat: Advertised latitude (decimal degrees), when the node shares location.
        lon: Advertised longitude (decimal degrees), when the node shares location.
        observed_at: When the packet was heard.
        raw: Optional raw event payload for debugging/replay.
    """

    node: Optional[str]
    name: Optional[str] = None
    kind: str = "advert"
    snr: Optional[float] = None
    rssi: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    observed_at: datetime = field(default_factory=utcnow)
    raw: Optional[dict] = None


@dataclass(slots=True)
class Message:
    """An inbound text message the companion received (direct or on a channel).

    Direct messages carry a :attr:`sender` key prefix; channel messages carry a
    :attr:`channel` index instead. Both are surfaced by the always-on event hub as
    :attr:`~meshtools.core.events.EventKind.MESSAGE` events, so client features (an inbox,
    notifications) can react to them without polling.

    Attributes:
        text: The decoded message body.
        sender: Key prefix of the sending contact (direct messages), if known.
        channel: Channel index the message arrived on (channel messages), if applicable.
        is_channel: Whether this is a channel message rather than a direct one.
        sender_timestamp: The sender's own timestamp for the message, if carried.
        snr: Signal-to-noise ratio (dB) of the reception, if reported.
        received_at: When the companion delivered the message to us.
        raw: Optional raw event payload for debugging/replay.
    """

    text: str
    sender: Optional[str] = None
    channel: Optional[int] = None
    is_channel: bool = False
    sender_timestamp: Optional[datetime] = None
    snr: Optional[float] = None
    received_at: datetime = field(default_factory=utcnow)
    raw: Optional[dict] = None


@dataclass(slots=True)
class Ack:
    """A delivery acknowledgement for a message the companion sent.

    Attributes:
        code: The ACK correlation code (hex), matching it to the sent message, if present.
        received_at: When the acknowledgement arrived.
        raw: Optional raw event payload for debugging/replay.
    """

    code: Optional[str] = None
    received_at: datetime = field(default_factory=utcnow)
    raw: Optional[dict] = None


def conversation_key(
    is_channel: bool, channel_id: Optional[str], peer: Optional[str]
) -> str:
    """Return a stable key identifying a chat conversation.

    A conversation is either a channel or a direct exchange with one contact; this key
    is how history, unread counts, and the live screen all agree on which thread a message
    belongs to. It is deliberately keyed on something *intrinsic* to the conversation — a
    channel's identity (derived from its secret; see
    :func:`~meshtools.core.channels.channel_identity`) and a contact's key prefix — never on
    a slot index or list position, so reordering channels never re-points history.

    Args:
        is_channel: Whether the conversation is a channel.
        channel_id: The channel's slot-independent identity (for channel conversations).
        peer: The contact's key prefix (for direct conversations).

    Returns:
        ``"chan:<identity>"`` for a channel or ``"dm:<peer>"`` (lowercased) for a direct
        exchange.
    """
    if is_channel:
        return f"chan:{channel_id}"
    return f"dm:{(peer or '').lower()}"


@dataclass(slots=True)
class ChatMessage:
    """A chat message, sent or received, on a channel or with a contact.

    This is the persisted, display-oriented view of a message: it unifies the outbound
    messages we send with the inbound :class:`Message` events the hub delivers, so a
    conversation transcript is a single ordered list of these. Each belongs to exactly one
    conversation — a channel (:attr:`is_channel` with :attr:`channel_idx`) or a direct
    exchange with a contact (:attr:`peer` holding the contact's key prefix).

    Attributes:
        text: The message body.
        outbound: ``True`` if we sent it, ``False`` if we received it.
        is_channel: Whether it belongs to a channel rather than a direct exchange.
        channel_id: The channel's slot-independent identity, for channel messages. This is
            what the message is keyed to, so its history follows the channel across slot
            reorders (see :func:`~meshtools.core.channels.channel_identity`).
        channel_idx: The channel slot the message went out on / arrived on. Retained only
            for reference and legacy backfill — the conversation key never uses it.
        peer: The other party's key prefix, for direct messages.
        peer_name: A friendly name for the peer/channel, snapshotted for display.
        snr: Signal-to-noise ratio (dB) of an inbound reception, if reported.
        acked: Delivery state of an outbound direct message: ``True`` acknowledged, ``False``
            sent but not acknowledged (retryable), ``None`` either still awaiting the ack
            or not applicable (a channel broadcast or an inbound message).
        created_at: When the message was sent or received.
        row_id: The ``messages`` table primary key once persisted, used to update an
            outbound message's delivery state in place on retry; ``None`` until stored.
    """

    text: str
    outbound: bool = False
    is_channel: bool = False
    channel_id: Optional[str] = None
    channel_idx: Optional[int] = None
    peer: Optional[str] = None
    peer_name: Optional[str] = None
    snr: Optional[float] = None
    acked: Optional[bool] = None
    created_at: datetime = field(default_factory=utcnow)
    row_id: Optional[int] = None

    @property
    def key(self) -> str:
        """The key of the conversation this message belongs to."""
        return conversation_key(self.is_channel, self.channel_id, self.peer)

    @classmethod
    def from_message(
        cls,
        message: "Message",
        *,
        peer_name: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> "ChatMessage":
        """Build an inbound :class:`ChatMessage` from a received :class:`Message`.

        Args:
            message: The inbound message delivered by the event hub.
            peer_name: A friendly name for the sender, resolved from contacts if known.
            channel_id: The channel's resolved identity (channel messages only). The wire
                carries only a slot index, so the caller resolves it to the channel's
                intrinsic identity before recording.

        Returns:
            The equivalent inbound :class:`ChatMessage`.
        """
        return cls(
            text=message.text,
            outbound=False,
            is_channel=message.is_channel,
            channel_id=channel_id if message.is_channel else None,
            channel_idx=message.channel,
            peer=None if message.is_channel else (message.sender or None),
            peer_name=peer_name,
            snr=message.snr,
            # Prefer the sender's own timestamp (the actual moment the message was
            # composed) over ``received_at`` (when we happened to pull it off the radio),
            # so the transcript reflects message time rather than retrieval time. Falls
            # back to the receive time when the sender carried no timestamp.
            created_at=message.sender_timestamp or message.received_at,
        )


@dataclass(slots=True)
class Conversation:
    """A selectable chat thread: a channel or a direct exchange with a contact.

    Attributes:
        label: Display name (e.g. ``#general`` or ``Alice``).
        is_channel: Whether this is a channel rather than a direct conversation.
        channel_idx: The channel slot to address for sending / live matching, for channels.
        channel_id: The channel's slot-independent identity, used to key its history (see
            :func:`~meshtools.core.channels.channel_identity`).
        secret: The channel's 16-byte secret, for channels — used only to show its
            public/private openness marker; ``None`` when unknown.
        contact: The contact, for direct conversations.
    """

    label: str
    is_channel: bool
    channel_idx: Optional[int] = None
    channel_id: Optional[str] = None
    secret: Optional[bytes] = None
    contact: Optional[Contact] = None

    @property
    def peer(self) -> Optional[str]:
        """The peer key prefix for a direct conversation, else ``None``."""
        if self.is_channel or self.contact is None:
            return None
        return self.contact.key_prefix or self.contact.public_key[:12] or None

    @property
    def key(self) -> str:
        """The conversation's stable key (see :func:`conversation_key`)."""
        return conversation_key(self.is_channel, self.channel_id, self.peer)


@dataclass(slots=True)
class HeardNode:
    """Aggregated reception statistics for one node across many observations.

    Attributes:
        node: Key prefix / hash of the node (``None`` only if never identified).
        name: Most recent friendly name seen for the node, if any.
        count: Number of observations aggregated.
        median_snr: Median SNR (dB) across observations that reported one.
        best_snr: Strongest SNR (dB) seen, if any.
        last_rssi: Most recent RSSI (dBm), if any.
        last_seen: Timestamp of the most recent observation.
        lat: Most recent advertised latitude, if the node shared one.
        lon: Most recent advertised longitude, if the node shared one.
    """

    node: Optional[str]
    name: Optional[str]
    count: int
    median_snr: Optional[float]
    best_snr: Optional[float]
    last_rssi: Optional[float]
    last_seen: datetime
    lat: Optional[float] = None
    lon: Optional[float] = None

    @property
    def has_location(self) -> bool:
        """Whether this node reported a usable latitude/longitude."""
        return self.lat is not None and self.lon is not None

    @classmethod
    def from_observations(
        cls, node: Optional[str], observations: list["Observation"]
    ) -> "HeardNode":
        """Aggregate one node's observations into reception statistics.

        Args:
            node: The node identifier these observations belong to.
            observations: The observations for ``node`` (must be non-empty).

        Returns:
            A :class:`HeardNode` summarizing them. The most recent observation supplies
            the name, RSSI, and location; SNR is summarized robustly (median + best).
        """
        ordered = sorted(observations, key=lambda o: o.observed_at)
        latest = ordered[-1]
        snrs = [o.snr for o in ordered if o.snr is not None]
        located = next(
            (o for o in reversed(ordered) if o.lat is not None and o.lon is not None), None
        )
        name = next((o.name for o in reversed(ordered) if o.name), None)
        return cls(
            node=node,
            name=name,
            count=len(ordered),
            median_snr=statistics.median(snrs) if snrs else None,
            best_snr=max(snrs) if snrs else None,
            last_rssi=latest.rssi,
            last_seen=latest.observed_at,
            lat=located.lat if located else None,
            lon=located.lon if located else None,
        )


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
        path_hash_bytes: Per-hop path-hash width (bytes) used by the trace command, so
            node hashes can be displayed at the same width the command addressed them.
        timestamp: When the trace completed.
        raw: Optional raw event payload for debugging/replay.
    """

    target: str
    success: bool
    hops: list[Hop] = field(default_factory=list)
    round_trip_ms: Optional[float] = None
    tx_power: Optional[int] = None
    path_hash_bytes: Optional[int] = None
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

    def edges(self, device_label: str = LOCAL_DEVICE_LABEL) -> list["HopEdge"]:
        """Frame the per-hop SNR as directed ``origin -> destination`` edges.

        Each hop's SNR is the signal measured arriving at that node, so an edge runs
        from the previous node to this one. The first edge therefore originates at our
        own device, and (because firmware records the reply returning to us as a final
        hash-less hop) the last edge's destination is our device too.

        Args:
            device_label: Name to show for our own device at the path's endpoints.

        Returns:
            One :class:`HopEdge` per hop, in path order.
        """
        edges: list[HopEdge] = []
        origin = device_label
        for hop in self.hops:
            destination = hop.node or device_label
            edges.append(HopEdge(index=hop.index, origin=origin, destination=destination, snr=hop.snr))
            origin = destination
        return edges


@dataclass(slots=True)
class HopEdge:
    """A directed link in a trace path: a hop framed as ``origin -> destination``.

    Attributes:
        index: Zero-based position of the hop along the path.
        origin: Identifier of the transmitting node (our device for the first edge).
        destination: Identifier of the receiving node (our device for the last edge).
        snr: Signal-to-noise ratio in dB measured at ``destination``.
    """

    index: int
    origin: str
    destination: str
    snr: float


@dataclass(slots=True)
class HopAggregate:
    """Median SNR for one hop position aggregated across several traces.

    ``origin`` and ``destination`` hold raw node identifiers; ``None`` means our own
    device, so the display label can be applied at render time.

    Attributes:
        index: Zero-based hop position along the path.
        origin: Representative transmitting node at this position (``None`` = us).
        destination: Representative receiving node at this position (``None`` = us).
        median_snr: Median SNR in dB measured at ``destination`` across the samples.
        samples: Number of traces that reported this hop.
    """

    index: int
    origin: Optional[str]
    destination: Optional[str]
    median_snr: float
    samples: int


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
        hop_snrs: Per-hop median SNR across the successful traces, in path order.
    """

    target: str
    samples: int
    successes: int
    median_min_snr: Optional[float]
    median_rtt_ms: Optional[float]
    tx_power: Optional[int] = None
    hop_snrs: list[HopAggregate] = field(default_factory=list)

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
            hop_snrs=cls._aggregate_hops(successes),
        )

    @staticmethod
    def _aggregate_hops(successes: list["TraceResult"]) -> list[HopAggregate]:
        """Compute the median SNR per hop position across successful traces.

        Hops are grouped by their path position; for each position the SNR median is
        taken and the most common origin/destination nodes are used so the aggregate
        path reads like a single representative trace. Node identities are kept raw
        (``None`` = our device) so a display label can be applied later.

        Args:
            successes: The successful traces to aggregate.

        Returns:
            One :class:`HopAggregate` per hop position, ordered along the path.
        """
        snrs_by_index: dict[int, list[float]] = {}
        origins_by_index: dict[int, list[Optional[str]]] = {}
        dests_by_index: dict[int, list[Optional[str]]] = {}
        for trace in successes:
            origin: Optional[str] = None  # the first hop originates at our device
            for hop in trace.hops:
                snrs_by_index.setdefault(hop.index, []).append(hop.snr)
                origins_by_index.setdefault(hop.index, []).append(origin)
                dests_by_index.setdefault(hop.index, []).append(hop.node)
                origin = hop.node

        aggregates: list[HopAggregate] = []
        for index in sorted(snrs_by_index):
            snrs = snrs_by_index[index]
            origin = Counter(origins_by_index[index]).most_common(1)[0][0]
            destination = Counter(dests_by_index[index]).most_common(1)[0][0]
            aggregates.append(
                HopAggregate(
                    index=index,
                    origin=origin,
                    destination=destination,
                    median_snr=statistics.median(snrs),
                    samples=len(snrs),
                )
            )
        return aggregates


@dataclass(slots=True)
class TxLevelResult:
    """Robust measurement of one TX power level during an optimization sweep.

    The headline metric is ``target_snr`` — the SNR the *target* node reports receiving
    from the admin-tuned node just ahead of it — since that single link is what the
    remote-admin optimizer is tuning. ``success_rate`` is the primary objective
    (reliability first), with ``target_snr`` the tie-breaker.

    Attributes:
        tx_power: The transmit power level tested on the admin node.
        samples: Number of traces run at this level.
        successes: Number of traces that returned a reply.
        target_snr: Median SNR (dB) received at the target across successful traces,
            or ``None`` if every trace at this level failed.
        score: Scalar objective for display/plotting (the median target SNR, or
            negative infinity when nothing got through).
        stats: Aggregated trace statistics at this level (full path, for display).
    """

    tx_power: int
    samples: int
    successes: int
    target_snr: Optional[float]
    score: float
    stats: TraceStats

    @property
    def success_rate(self) -> float:
        """Fraction of traces that returned a reply, in the range ``[0, 1]``."""
        return self.successes / self.samples if self.samples else 0.0


@dataclass(slots=True)
class TxOptResult:
    """The outcome of a remote-admin TX-power optimization run.

    Attributes:
        target: Node the SNR was measured at.
        admin_node: Label of the node whose TX power was tuned (the hop before target).
        path: The forced path the traces walked (comma-separated hashes).
        original_tx: The admin node's TX power before the sweep, if it could be read.
        best_tx: The chosen optimal TX power.
        best_snr: Median target SNR (dB) at ``best_tx``.
        best_success_rate: Trace success rate at ``best_tx``.
        applied: Whether ``best_tx`` was written to the admin node.
        levels: Every level measured, in the order tested.
    """

    target: str
    admin_node: str
    path: str
    original_tx: Optional[int]
    best_tx: int
    best_snr: Optional[float]
    best_success_rate: float
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
