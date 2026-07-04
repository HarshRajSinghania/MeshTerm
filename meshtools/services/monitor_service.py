"""The passive-monitor background service.

Passive monitoring records every advert/telemetry frame the companion overhears to the
database, building the longitudinal history the coverage map and link-quality alerting
read back. Unlike a one-shot capture, it runs as a non-blocking background subscription:
once started it logs observations as they arrive while the user keeps using the app, and
keeps going across menu actions until stopped or the session ends.

The service is session-scoped state on the :class:`~meshtools.context.AppContext`
(``ctx.monitor``). It owns the on/off preference (persisted via
:class:`~meshtools.core.monitor_store.MonitorStore`), the live subscription, and the
counters shown live in the menu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..core.connection import Unsubscribe
from ..core.models import Observation

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.monitor_store import MonitorStore


class MonitorService:
    """Manages the always-on, non-blocking passive-monitor subscription.

    Attributes are private; interact through the properties and the async lifecycle
    methods (:meth:`enable`, :meth:`disable`, :meth:`toggle`, :meth:`start`, :meth:`stop`,
    :meth:`aclose`).
    """

    def __init__(self, ctx: "AppContext", store: "MonitorStore") -> None:
        """Initialize the service and load the persisted on/off preference.

        Args:
            ctx: The shared application context (device, repository, logger).
            store: Persistence for the on/off preference.
        """
        self._ctx = ctx
        self._store = store
        self._enabled = store.load_enabled()
        self._unsubscribe: Optional[Unsubscribe] = None
        self._run_id: Optional[int] = None
        self._session_count = 0
        self._run_start_count = 0
        # Total observations already in the database when the session began; the live
        # "total" is this plus what we capture this session (this process is the only
        # writer during an interactive session), avoiding a DB count on every repaint.
        self._start_total = ctx.repo.observation_count()
        self._error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        """Whether monitoring is the desired state (persisted across sessions)."""
        return self._enabled

    @property
    def active(self) -> bool:
        """Whether a live subscription is currently capturing."""
        return self._unsubscribe is not None

    @property
    def session_count(self) -> int:
        """Observations captured since this process started."""
        return self._session_count

    @property
    def last_error(self) -> Optional[str]:
        """The reason the last :meth:`start` attempt failed, if any."""
        return self._error

    def total_count(self) -> int:
        """Return the total observations logged, all time (including this session)."""
        return self._start_total + self._session_count

    def status_text(self) -> str:
        """Return a compact one-line status for the live menu header.

        Returns:
            A glyph-prefixed summary: capture counts when active, a waiting note when
            enabled but not yet capturing, or an off marker.
        """
        if self.active:
            return (
                f"● monitor ON · {self._session_count} this session "
                f"· {self.total_count()} total"
            )
        if self._enabled:
            return "● monitor ON (waiting for a device)"
        return "○ monitor OFF"

    async def start(self) -> None:
        """Open a background subscription and begin logging observations.

        Idempotent: a no-op if already active. Opens the device connection (which may
        raise if no device can be selected) and records a ``monitor`` run that the
        captured observations are linked to.

        Raises:
            Exception: Propagates any device/subscription error after recording it; the
                on/off preference is left unchanged so the caller can surface the problem.
        """
        if self.active:
            return
        try:
            device = await self._ctx.device()
        except Exception as exc:  # noqa: BLE001 - remember why, then re-raise
            self._error = str(exc)
            raise
        run_id = self._ctx.repo.start_run(
            "monitor", {"mode": "background"}, self._ctx.profile_name
        )
        self._run_start_count = self._session_count

        def on_observation(obs: Observation) -> None:
            # Runs on the event loop as packets arrive; keep it cheap and defensive so a
            # single bad write can never take down the subscription.
            self._session_count += 1
            try:
                self._ctx.repo.record_observation(run_id, obs)
            except Exception as exc:  # noqa: BLE001 - never let logging break capture
                self._ctx.log.debug("monitor: failed to record observation: %s", exc)

        try:
            self._unsubscribe = await device.subscribe_observations(on_observation)
        except Exception as exc:  # noqa: BLE001 - record, remember, and re-raise
            self._ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            self._error = str(exc)
            raise
        self._run_id = run_id
        self._error = None
        self._ctx.log.info("passive monitor started (run %s)", run_id)

    async def stop(self) -> None:
        """Stop the background subscription and close its run record.

        Idempotent: a no-op if not active.
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

    async def enable(self) -> None:
        """Turn monitoring on, persist the preference, and start capturing.

        Raises:
            Exception: If capture could not start (e.g. no device); the preference is
                still persisted as *on* so it resumes once a device is available.
        """
        self._enabled = True
        self._store.save_enabled(True)
        await self.start()

    async def disable(self) -> None:
        """Turn monitoring off, persist the preference, and stop capturing."""
        self._enabled = False
        self._store.save_enabled(False)
        await self.stop()

    async def toggle(self) -> bool:
        """Flip the on/off state, starting or stopping capture accordingly.

        Returns:
            The new enabled state (``True`` if now on).

        Raises:
            Exception: Propagates a failure to start capture when turning on.
        """
        if self._enabled:
            await self.disable()
        else:
            await self.enable()
        return self._enabled

    async def aclose(self) -> None:
        """Stop capture at session end without changing the persisted preference."""
        await self.stop()
