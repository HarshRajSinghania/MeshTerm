"""The always-on mesh event hub.

The hub is the single seam between the raw companion-device event stream and everything
that wants to react to it. It opens *one* subscription to the connected device, normalizes
what arrives into typed :class:`~meshterm.core.events.MeshEvent` values, and fans each
one out to any number of subscribers. Unlike a one-shot capture it is meant to run for the
whole life of an interactive session — a MeshCore client must always be listening — so
subscribers come and go while the pump keeps running underneath them.

This decouples *listening* from *consuming*: the passive monitor's database logging becomes
just one subscriber (see :class:`~meshterm.services.monitor_service.MonitorService`), and
future client features (a live node list, an incoming-message inbox) attach as additional
subscribers without touching the device layer.

The hub is session-scoped state on the :class:`~meshterm.context.AppContext`
(``ctx.events``). Fan-out runs synchronously on the event loop as packets arrive, so
handlers should stay cheap; a handler that returns a coroutine is scheduled as a task.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Optional, Union

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent

if TYPE_CHECKING:
    from ..context import AppContext

#: A subscriber callback. Invoked with each matching :class:`MeshEvent`. Keep it cheap;
#: it runs inline on the event loop as packets arrive. It may return a coroutine, which
#: the hub schedules as a task rather than awaiting inline.
EventHandler = Callable[[MeshEvent], Union[None, Awaitable[None]]]


@dataclass(slots=True)
class _Subscription:
    """One registered subscriber: a handler plus the kinds it cares about.

    Attributes:
        handler: The callback to invoke with matching events.
        kinds: The event kinds to deliver; an empty set means *all* kinds.
    """

    handler: EventHandler
    kinds: frozenset = field(default_factory=frozenset)

    def wants(self, kind: EventKind) -> bool:
        """Whether this subscription should receive an event of ``kind``."""
        return not self.kinds or kind in self.kinds


class EventHub:
    """Owns the device event subscription and fans events out to subscribers.

    Interact through :meth:`subscribe` / :meth:`stream` to consume events and the async
    lifecycle methods (:meth:`start`, :meth:`stop`, :meth:`aclose`) to control the pump.
    """

    def __init__(self, ctx: "AppContext") -> None:
        """Initialize an idle hub bound to an application context.

        Args:
            ctx: The shared application context, used to open the device connection and
                to log. No device is opened until :meth:`start` is called.
        """
        self._ctx = ctx
        self._device_unsubscribe: Optional[Unsubscribe] = None
        self._subs: list[_Subscription] = []
        self._tasks: set[asyncio.Task] = set()

    @property
    def active(self) -> bool:
        """Whether the pump is running (subscribed to the device)."""
        return self._device_unsubscribe is not None

    async def start(self) -> None:
        """Open the device subscription and begin pumping events. Idempotent.

        Opens the companion connection (which may raise if no device can be selected) and
        subscribes to its observation stream. Subscribers registered before or after this
        call all receive events once the pump is running.

        Raises:
            Exception: Propagates any device/subscription error; the hub stays inactive so
                the caller can surface the problem and retry later.
        """
        if self.active:
            return
        device = await self._ctx.device()
        self._device_unsubscribe = await device.subscribe_events(self.publish)
        self._ctx.log.info("event hub started")

    async def stop(self) -> None:
        """Release the device subscription and drop any scheduled handler tasks.

        Idempotent. Registered subscribers are left in place, so a later :meth:`start`
        resumes delivering to them.
        """
        if self._device_unsubscribe is not None:
            try:
                self._device_unsubscribe()
            finally:
                self._device_unsubscribe = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._ctx.log.info("event hub stopped")

    def subscribe(self, handler: EventHandler, *kinds: EventKind) -> Unsubscribe:
        """Register ``handler`` to receive events, and return a callable that removes it.

        Args:
            handler: The callback invoked with each matching :class:`MeshEvent`.
            *kinds: The event kinds to receive. Pass none to receive *every* kind.

        Returns:
            A zero-argument callable that unregisters the subscription.
        """
        sub = _Subscription(handler=handler, kinds=frozenset(kinds))
        self._subs.append(sub)

        def unsubscribe() -> None:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass  # already removed; unsubscribe is idempotent

        return unsubscribe

    def stream(self, *kinds: EventKind, maxsize: int = 0):
        """Return an async iterator yielding matching events as they arrive.

        An ergonomic ``async for event in hub.stream(...)`` adapter over :meth:`subscribe`,
        backed by an :class:`asyncio.Queue`. The subscription is removed automatically when
        the iterator is closed (e.g. the ``async for`` loop breaks or the consumer is
        cancelled).

        Args:
            *kinds: The event kinds to receive. Pass none to receive every kind.
            maxsize: Optional bound on the backing queue (``0`` = unbounded).

        Returns:
            An async generator of :class:`MeshEvent`.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        unsubscribe = self.subscribe(queue.put_nowait, *kinds)

        async def _iterator():
            try:
                while True:
                    yield await queue.get()
            finally:
                unsubscribe()

        return _iterator()

    def publish(self, event: MeshEvent) -> None:
        """Fan ``event`` out to every subscriber that wants its kind.

        Delivery is synchronous and best-effort: a handler that raises is logged and
        skipped so one bad subscriber can never take down the pump or starve the others.
        A handler that returns a coroutine is scheduled as a background task.

        Args:
            event: The event to deliver.
        """
        for sub in list(self._subs):
            if not sub.wants(event.kind):
                continue
            try:
                result = sub.handler(event)
            except Exception as exc:  # noqa: BLE001 - one bad subscriber must not break the pump
                self._ctx.log.debug("event hub: subscriber raised: %s", exc)
                continue
            if asyncio.iscoroutine(result):
                self._schedule(result)

    def _schedule(self, coro: Awaitable[None]) -> None:
        """Run an async handler's coroutine as a tracked background task."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_for(
        self,
        *kinds: EventKind,
        predicate: Optional[Callable[[MeshEvent], bool]] = None,
        timeout: Optional[float] = None,
    ) -> Optional[MeshEvent]:
        """Await the next event matching ``kinds`` (and ``predicate``), or time out.

        A one-shot convenience for consumers that want to block for a specific event
        rather than register a standing handler. The temporary subscription is always
        removed before returning.

        Args:
            *kinds: The event kinds to accept. Pass none to accept every kind.
            predicate: Optional extra filter; the event must also satisfy it to match.
            timeout: Seconds to wait before giving up, or ``None`` to wait indefinitely.

        Returns:
            The matching :class:`MeshEvent`, or ``None`` if ``timeout`` elapsed first.
        """
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()

        def handler(event: MeshEvent) -> None:
            if not future.done() and (predicate is None or predicate(event)):
                future.set_result(event)

        unsubscribe = self.subscribe(handler, *kinds)
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            unsubscribe()

    async def aclose(self) -> None:
        """Stop the pump at session end (an alias for :meth:`stop`)."""
        await self.stop()
