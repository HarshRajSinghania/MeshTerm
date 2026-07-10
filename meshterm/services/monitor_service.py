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

from typing import TYPE_CHECKING, Optional

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent

if TYPE_CHECKING:
    from ..context import AppContext


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
        self._run_id: Optional[int] = None
        self._session_count = 0
        self._run_start_count = 0
        # Total observations already in the database when the session began; the live
        # "total" is this plus what we capture this session (this process is the only
        # writer during an interactive session), avoiding a DB count on every repaint.
        self._start_total = ctx.repo.observation_count()

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

    def status_text(self) -> str:
        """Return a compact one-line status for the live menu header.

        Monitoring has no off state, so this reports activity rather than a switch:
        capture counts while the hub is pumping, or a waiting note until a device link
        gives it something to hear.

        Returns:
            A glyph-prefixed summary line.
        """
        if self._ctx.events.active:
            return (
                f"● heard {self._session_count} this session "
                f"· {self.total_count()} total"
            )
        return f"○ heard {self.total_count()} all-time · waiting for a device"

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

        def on_event(event: MeshEvent) -> None:
            # Runs on the event loop as packets arrive; keep it cheap and defensive so a
            # single bad write can never take down the subscription.
            obs = event.observation
            if obs is None:
                return
            self._session_count += 1
            try:
                if self._run_id is None:
                    self._run_id = self._ctx.repo.start_run(
                        "monitor", {"mode": "background"}, self._ctx.profile_name
                    )
                self._ctx.repo.record_observation(self._run_id, obs)
            except Exception as exc:  # noqa: BLE001 - never let logging break capture
                self._ctx.log.debug("monitor: failed to record observation: %s", exc)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.OBSERVATION)
        self._ctx.log.info("passive monitor recording")

    async def stop(self) -> None:
        """Stop recording to history and close the run record. Idempotent.

        A no-op if not recording. The event hub keeps listening; only this service's
        recording subscription is removed.
        """
        if not self.active:
            return
        try:
            assert self._unsubscribe is not None
            self._unsubscribe()
        finally:
            self._unsubscribe = None
        if self._run_id is not None:
            captured = self._session_count - self._run_start_count
            self._ctx.repo.finish_run(self._run_id, "ok", {"observations": captured})
            self._ctx.log.info("passive monitor stopped (run %s, %s pkts)", self._run_id, captured)
            self._run_id = None

    async def aclose(self) -> None:
        """Stop capture at session end."""
        await self.stop()
