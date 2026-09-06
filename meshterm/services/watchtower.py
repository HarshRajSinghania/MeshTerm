"""The Watchtower: passive rules over what the hub already hears, raising alerts.

A sentinel, not a prober — it transmits nothing and asks the radio for nothing. It rides
the always-on :class:`~meshterm.services.event_hub.EventHub` like the monitor does, and
watches for the things an operator actually loses sleep over:

* **silence** — a starred node hasn't been heard for its configured threshold. Fires
  once per quiet spell (latched in the store) and re-arms when the node returns, which
  also raises a friendly **recovered** note.
* **SNR sag** — a starred node's receptions are degrading: the median of its last few
  readings sits well below the median of the readings before them. A cooldown keeps a
  slowly dying link from paging every minute.
* **new node** — something never before seen (not in the observation history at start,
  not in the store's memory) just appeared on the mesh. Announced at most once per id,
  ever.

Alerts land in the :class:`~meshterm.core.watch_store.WatchStore` (which persists them),
feed the header's alert badge, and are read and acknowledged in the Watchtower screen
(:mod:`meshterm.ui.watchtower_screen`). The service is session-scoped state on the
:class:`~meshterm.context.AppContext` (``ctx.watchtower``), started alongside the other
always-on services; scripted CLI runs never start it.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import Observation, utcnow
from ..core.watch_store import OFF

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between rule sweeps (the silence rule is time-driven, not packet-driven).
SWEEP_S = 30.0

#: How many recent SNR readings form the "now" side of the sag comparison.
SNR_WINDOW = 4

#: How far (dB) the recent median must sit below the prior median to alert.
SNR_SAG_DB = 6.0

#: Seconds before the sag rule may fire again for the same node.
SNR_COOLDOWN_S = 6 * 3600.0

#: How many SNR readings are kept per node (the comparison uses at most this many).
_SNR_KEEP = 12


class WatchtowerService:
    """Watches starred nodes and the mesh's cast of characters; raises alerts.

    Attributes are private; interact through the properties and the async lifecycle
    methods (:meth:`start`, :meth:`stop`, :meth:`aclose`). Rule evaluation lives in
    the synchronous :meth:`note` / :meth:`evaluate` so tests can drive it directly.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize the (idle) service.

        Args:
            ctx: The shared application context (store, repository, event hub).
        """
        self._ctx = ctx
        self._unsubscribe: Unsubscribe | None = None
        self._task: asyncio.Task | None = None
        #: Every node id ever seen (DB history + store memory + this session), the
        #: baseline the new-node rule compares against. ``None`` until started.
        self._known: set[str] | None = None
        #: Rolling SNR readings per watched node, session-scoped.
        self._snr: dict[str, deque[float]] = {}
        #: When the sag rule last fired per node (the cooldown clock).
        self._snr_fired: dict[str, datetime] = {}

    @property
    def active(self) -> bool:
        """Whether the sentinel is currently watching."""
        return self._unsubscribe is not None

    def unacked_count(self) -> int:
        """Alerts awaiting acknowledgement — the header badge's number."""
        return self._ctx.watch_store.unacked_count()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        """Begin watching. Idempotent, device-free, and safe before any connection.

        Seeds the new-node baseline from the observation history (every id the DB has
        ever heard) plus the store's memory of past announcements, then subscribes to
        the hub and starts the sweep. Like the monitor, packets flow in whenever the
        hub is pumping — including a hub that opens lazily later.
        """
        if self.active:
            return
        store = self._ctx.watch_store
        baseline = {h.node for h in self._ctx.repo.heard_nodes() if h.node}
        baseline |= set(store.state.known)
        baseline |= set(store.watched())
        self._known = baseline

        def on_event(event: MeshEvent) -> None:
            obs = event.observation
            if obs is not None:
                try:
                    self.note(obs)
                except Exception as exc:  # noqa: BLE001 - a rule bug must not kill the hub
                    self._ctx.log.debug("watchtower: note failed: %s", exc)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.OBSERVATION)
        self._task = asyncio.ensure_future(self._sweep())
        self._ctx.log.info("watchtower watching (%d nodes starred)", len(store.watched()))

    async def stop(self) -> None:
        """Stop watching and flush pending store writes. Idempotent."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                pass
            self._task = None
        self._ctx.watch_store.flush()

    async def aclose(self) -> None:
        """Stop watching at session end."""
        await self.stop()

    async def _sweep(self) -> None:
        """Run the time-driven rules on a slow heartbeat."""
        while True:
            await asyncio.sleep(SWEEP_S)
            try:
                self.evaluate()
            except Exception as exc:  # noqa: BLE001 - keep the sentinel alive
                self._ctx.log.debug("watchtower: sweep failed: %s", exc)

    # --- packet-driven rules -------------------------------------------------------------

    def note(self, obs: Observation) -> None:
        """Fold one observation into the rules (called per hub packet; keep cheap).

        Args:
            obs: The overheard observation.
        """
        node = obs.node
        if not node:
            return
        store = self._ctx.watch_store
        entry = store.watched().get(node)

        # New node: never in the DB, the store's memory, or this session before now.
        # Starring a node is itself an introduction, so watched nodes are never "new".
        if self._known is not None and node not in self._known:
            self._known.add(node)
            store.remember_known(node)
            if entry is None and store.new_node_alerts:
                label = obs.name or node
                store.add_alert(
                    "new-node", label,
                    "first appearance — never heard on this mesh before",
                    when=obs.observed_at,
                )

        if entry is None:
            return
        was_silent = entry.silent_since is not None
        store.note_heard(node, when=obs.observed_at, name=obs.name)
        if was_silent:
            store.clear_silent(node)
            store.add_alert(
                "recovered", entry.name,
                "back on the air — heard again after a silence alarm",
                when=obs.observed_at,
            )
        # Packet-kind rows measure our link to the last relay, not to the node, so
        # they prove liveness (above) but say nothing about the node's own signal.
        if entry.snr_watch and obs.snr is not None and obs.kind != "packet":
            self._track_snr(node, entry.name, obs)

    def _track_snr(self, node: str, label: str, obs: Observation) -> None:
        """Fold one SNR reading in and fire the sag rule when the trend warrants."""
        readings = self._snr.setdefault(node, deque(maxlen=_SNR_KEEP))
        readings.append(float(obs.snr))  # type: ignore[arg-type]
        if len(readings) < 2 * SNR_WINDOW:
            return
        recent = statistics.median(list(readings)[-SNR_WINDOW:])
        prior = statistics.median(list(readings)[:-SNR_WINDOW])
        if prior - recent < SNR_SAG_DB:
            return
        fired = self._snr_fired.get(node)
        now = obs.observed_at or utcnow()
        if fired is not None and (now - fired).total_seconds() < SNR_COOLDOWN_S:
            return
        self._snr_fired[node] = now
        self._ctx.watch_store.add_alert(
            "snr", label,
            f"reception sagging — median {prior:+.1f} dB → {recent:+.1f} dB",
            when=now,
        )

    # --- time-driven rules ---------------------------------------------------------------

    def evaluate(self, now: datetime | None = None) -> None:
        """Run the silence rule over every watched node and flush the store.

        Args:
            now: The evaluation time (defaults to the current time; tests inject).
        """
        now = now or utcnow()
        store = self._ctx.watch_store
        for key, entry in store.watched().items():
            hours = entry.silence_hours
            if hours == OFF or entry.last_heard is None or entry.silent_since is not None:
                continue
            quiet_s = (now - entry.last_heard).total_seconds()
            if quiet_s >= hours * 3600:
                store.mark_silent(key, now)
                store.add_alert(
                    "silence", entry.name,
                    f"nothing heard for {hours} h", when=now,
                )
        store.flush()
