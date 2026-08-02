"""The passive-monitor history logger.

Passive monitoring records every advert/telemetry frame the companion overhears to the
database, building the longitudinal history the coverage map and link-quality alerting
read back. It is not a listener in its own right: the always-on
:class:`~meshterm.services.event_hub.EventHub` (``ctx.events``) does the listening, and
this service is simply one of its subscribers — the one that writes observations to the
database. Recording is always on: there is no user-facing switch. The subscription is
registered at session start without touching the device, so observations flow the moment
the hub is pumping — including when the hub itself opens lazily (``connect_on_start``
off, or no device selected yet).

The service is session-scoped state on the :class:`~meshterm.context.AppContext`
(``ctx.monitor``). It owns the hub subscription that records observations and the
counters shown live in the menu header.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Optional

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import Observation, utcnow
from ..core.nodetypes import register_node_type

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds per bucket of the all-packet activity histogram — one minute, one braille
#: dot column of the header indicator and the dashboard's activity chart.
ACTIVITY_BUCKET_S = 60

#: How many one-minute buckets the histogram retains — six hours, deep enough that the
#: header and the dashboard can both stretch their charts to fill any realistic terminal
#: width (two buckets per character cell) and still be drawing history, not padding.
ACTIVITY_BUCKETS = 360


class MonitorService:
    """Records overheard packets to history as a subscriber of the always-on event hub.

    Attributes are private; interact through the properties and the async lifecycle
    methods (:meth:`start`, :meth:`stop`, :meth:`aclose`).
    """

    def __init__(self, ctx: "AppContext") -> None:
        """Initialize the (idle) service.

        Args:
            ctx: The shared application context (device, repository, logger).
        """
        self._ctx = ctx
        self._unsubscribe: Optional[Unsubscribe] = None  # hub subscription, when recording
        self._count_unsubscribe: Optional[Unsubscribe] = None  # the all-packet counter's
        self._run_id: Optional[int] = None
        self._session_count = 0
        self._run_start_count = 0
        # Observations queue off the event-loop callback onto this worker (see start()),
        # so a burst of overheard packets writes to disk between repaints instead of
        # blocking them — the same shape as ChatService's inbound queue.
        self._queue: Optional["asyncio.Queue[Observation]"] = None
        self._worker: Optional["asyncio.Future"] = None
        # All-packet activity, bucketed by wall-clock minute (epoch // span → count).
        # Every hub event counts — observations, messages, acks — because the header's
        # indicator answers "is the mesh alive?", not "any mail?". Pruned as it rolls,
        # so it never holds more than the six-hour window plus one closing bucket.
        self._activity: dict[int, int] = {}
        # Tallies by packet class (advert/telemetry/packet/message/ack): seeded from
        # stored history below, then fed live by the kind-unfiltered subscription —
        # the dashboard's traffic panel, persistent across sessions.
        self._kind_counts: dict[str, int] = {}
        # Housekeeping: age out observations past the retention window once per
        # session, so an always-recording database stays bounded (0 = keep forever).
        days = getattr(getattr(ctx, "settings", None), "history_days", 0) or 0
        if days:
            try:
                pruned = ctx.repo.prune_observations(utcnow() - timedelta(days=days))
                if pruned:
                    ctx.log.info("history housekeeping: pruned %s observations", pruned)
            except Exception as exc:  # noqa: BLE001 - housekeeping must never block startup
                ctx.log.debug("history housekeeping failed: %s", exc)
        # Total observations already in the database when the session began; the live
        # "total" is this plus what we capture this session (this process is the only
        # writer during an interactive session), avoiding a DB count on every repaint.
        self._start_total = ctx.repo.observation_count()
        # The minute this session began: buckets at or after it hold live traffic,
        # older ones the seeded history — the boundary the activity charts colour by.
        self._start_bucket = int(time.time() // ACTIVITY_BUCKET_S)
        self._seed_from_history()

    def _seed_from_history(self) -> None:
        """Warm the activity buckets and kind tallies from stored observations.

        The dashboard's persistence: a fresh session opens mid-story — the activity
        chart already showing the trailing six hours and the traffic panel its
        all-history tallies — instead of an empty chart that only fills while the app
        happens to be running. Live events then stack on top (they are *new* rows, so
        nothing double-counts). Stored history holds observations only; messages and
        acks resume counting from zero each session.
        """
        try:
            window = timedelta(seconds=ACTIVITY_BUCKET_S * ACTIVITY_BUCKETS)
            for obs in self._ctx.repo.recent_observations(since=utcnow() - window):
                bucket = int(obs.observed_at.timestamp() // ACTIVITY_BUCKET_S)
                self._activity[bucket] = self._activity.get(bucket, 0) + 1
            self._kind_counts = dict(self._ctx.repo.kind_counts())
        except Exception as exc:  # noqa: BLE001 - a cold start is worse than a blank chart
            self._ctx.log.debug("monitor: history seed failed: %s", exc)

    @property
    def active(self) -> bool:
        """Whether observations are currently being recorded to history."""
        return self._unsubscribe is not None

    @property
    def session_count(self) -> int:
        """Observations captured since this process started."""
        return self._session_count

    def total_count(self) -> int:
        """Return the total observations logged, all time (including this session)."""
        return self._start_total + self._session_count

    def activity_histogram(self) -> tuple[int, ...]:
        """All-packet counts per one-minute bucket over the trailing six hours.

        Newest first — index 0 is the current minute — the order the chart widgets
        expect (they draw "now" at the right edge). Seeded from stored observations at
        session start and fed live by everything the hub fans out, so the header's
        pulse and the dashboard's chart open mid-story after a restart. Consumers
        slice however much of the window fits their chart and treat the rest as
        history in reserve.

        Returns:
            :data:`ACTIVITY_BUCKETS` bucket counts.
        """
        bucket = int(time.time() // ACTIVITY_BUCKET_S)
        return tuple(self._activity.get(bucket - i, 0) for i in range(ACTIVITY_BUCKETS))

    def activity_session_flags(self) -> tuple[bool, ...]:
        """Whether each histogram bucket holds this session's own traffic.

        Aligned with :meth:`activity_histogram` (newest first): ``True`` for buckets
        at or after the session's first minute, ``False`` for the seeded history —
        the split the activity charts use to draw live traffic green and a previous
        session's grey.

        Returns:
            :data:`ACTIVITY_BUCKETS` flags, newest first.
        """
        bucket = int(time.time() // ACTIVITY_BUCKET_S)
        return tuple(bucket - i >= self._start_bucket for i in range(ACTIVITY_BUCKETS))

    def kind_counts(self) -> dict[str, int]:
        """Tallies by packet class: advert/telemetry/packet buckets, message, ack.

        Seeded from stored history at session start and grown live from there, so the
        numbers describe everything the recorder retains, not just this session.
        Observation classes come from the packet itself (``Observation.kind``) — a raw
        ``packet`` frame with a parsed payload class buckets as ``packet:<TYPENAME>``,
        matching :meth:`~meshterm.persistence.repository.Repository.kind_counts`'s
        stored seed — and messages and acks are their own classes. A copy, safe to
        mutate.
        """
        return dict(self._kind_counts)

    def _count_packet(self, event: MeshEvent) -> None:
        """Land one packet in the current activity bucket (and prune scrolled-off ones)."""
        bucket = int(time.time() // ACTIVITY_BUCKET_S)
        self._activity[bucket] = self._activity.get(bucket, 0) + 1
        obs = event.observation
        kind = obs.kind if obs is not None else event.kind.value
        if obs is not None and kind == "packet":
            # Bucket a classed raw frame by its payload class (the stored seed's shape).
            typename = (obs.raw or {}).get("payload_typename")
            if typename:
                kind = f"packet:{typename}"
        self._kind_counts[kind] = self._kind_counts.get(kind, 0) + 1
        if len(self._activity) > ACTIVITY_BUCKETS + 1:
            cutoff = bucket - ACTIVITY_BUCKETS
            for stale in [b for b in self._activity if b < cutoff]:
                del self._activity[stale]

    async def start(self) -> None:
        """Begin recording overheard observations to history. Idempotent.

        Registers the recording subscription on the event hub without touching the
        device, so it is safe (and cheap) to call before any connection exists;
        observations flow as soon as the hub is pumping. The backing ``monitor`` run row
        is opened lazily on the first observation, so a session that hears nothing
        leaves no empty run in history.
        """
        if self.active:
            return
        self._run_start_count = self._session_count
        self._queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._process_observations())

        def on_event(event: MeshEvent) -> None:
            # Runs on the event loop as packets arrive; keep it cheap and non-blocking. It
            # only hands the observation to the worker queue — opening the run row and the
            # database write happen off the worker, so a burst of overheard packets (every
            # advert/telemetry/RX-log frame the mesh produces) can never stall the render
            # and input loop the way a synchronous commit per packet would.
            obs = event.observation
            if obs is None:
                return
            self._session_count += 1
            queue = self._queue
            if queue is not None:
                queue.put_nowait(obs)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.OBSERVATION)
        # A second, kind-unfiltered subscription feeds the header's activity indicator:
        # every packet the hub hears lands in a five-minute bucket, in memory only.
        self._count_unsubscribe = self._ctx.events.subscribe(self._count_packet)
        self._ctx.log.info("passive monitor recording")

    async def _process_observations(self) -> None:
        """Serially record queued observations, opening the run row on the first one.

        A single worker drains the queue so recording never races the run-row creation and
        the database write never runs inline with the hub's synchronous event dispatch (see
        :meth:`start`) — the same shape as :class:`~meshterm.services.chat_service.ChatService`'s
        inbound worker.
        """
        assert self._queue is not None
        while True:
            obs = await self._queue.get()
            try:
                if self._run_id is None:
                    self._run_id = self._ctx.repo.start_run(
                        "monitor", {"mode": "background"}, self._ctx.profile_name
                    )
                self._ctx.repo.record_observation(self._run_id, obs)
                register_node_type(obs.node, obs.node_type)
            except Exception as exc:  # noqa: BLE001 - never let logging break capture
                self._ctx.log.debug("monitor: failed to record observation: %s", exc)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        """Stop recording to history and close the run record. Idempotent.

        A no-op if not recording. The event hub keeps listening; only this service's
        recording subscription and its recording worker are removed.
        """
        if not self.active:
            return
        try:
            assert self._unsubscribe is not None
            self._unsubscribe()
        finally:
            self._unsubscribe = None
        if self._count_unsubscribe is not None:
            self._count_unsubscribe()
            self._count_unsubscribe = None
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        self._queue = None
        if self._run_id is not None:
            captured = self._session_count - self._run_start_count
            self._ctx.repo.finish_run(self._run_id, "ok", {"observations": captured})
            self._ctx.log.info("passive monitor stopped (run %s, %s pkts)", self._run_id, captured)
            self._run_id = None

    async def aclose(self) -> None:
        """Stop capture at session end."""
        await self.stop()
