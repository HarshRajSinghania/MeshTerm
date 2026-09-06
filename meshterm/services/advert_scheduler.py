"""The background-advert scheduler: keep this node announced without anyone asking.

A quiet session-long task that sends a zero-hop (direct) advert and a flood advert on
their configured cadences (see :class:`~meshterm.core.advert_store.AdvertStore`; the
cadence rows live in the device-configuration editor). The countdown for each type runs
from the *last* advert of that type — scheduled or manual — so using the Send advert menu
pushes the next background send out by a full cadence rather than doubling up.

Deliberately transmission-shy, in keeping with the app's single-transmission rule:

* At most **one** advert goes out per pass, even when both types are due (the other
  follows a pass later), so the scheduler can never burst.
* A device with no recorded last-send is *armed* — the countdown starts now — instead of
  advertised immediately, so a fresh install stays silent for its first full cadence.

The scheduler holds no device subscription; each pass just checks whether a device is
connected and skips quietly otherwise. That makes it indifferent to reconnects — it keeps
ticking across a link drop and resumes sending once the session's reconnect flow has done
its job.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between scheduler passes. Cadences are hours, so a coarse tick is plenty; it
#: also spaces consecutive sends (when both types come due together) a comfortable gap
#: apart on the shared mesh.
POLL_S = 30.0


class AdvertScheduler:
    """Owns the background-advert loop for an interactive session.

    Interact through the async lifecycle methods (:meth:`start`, :meth:`stop`,
    :meth:`aclose`); everything else happens on the loop's own schedule.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize an idle scheduler bound to an application context.

        Args:
            ctx: The shared application context, read each pass for the connected
                device and the advert store. Nothing is touched until :meth:`start`.
        """
        self._ctx = ctx
        self._task: asyncio.Task | None = None
        # The connected device's public key, learned once per connection. Keyed by the
        # device object's identity so a reconnect (a fresh Device) re-probes it.
        self._key_for: tuple[int, str] | None = None

    @property
    def active(self) -> bool:
        """Whether the scheduler loop is running."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the scheduler loop. Idempotent; never touches the radio itself."""
        if self.active:
            return
        self._task = asyncio.ensure_future(self._run())
        self._ctx.log.info("advert scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler loop. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._ctx.log.info("advert scheduler stopped")

    async def aclose(self) -> None:
        """Stop the loop at session end (an alias for :meth:`stop`)."""
        await self.stop()

    async def _run(self) -> None:
        """Tick forever: sleep, then make one best-effort pass."""
        while True:
            await asyncio.sleep(POLL_S)
            try:
                await self._pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the loop
                self._ctx.log.debug("advert scheduler: pass failed: %s", exc)

    async def _pass(self) -> None:
        """One pass: arm never-sent clocks, and send the most overdue advert type if due."""
        ctx = self._ctx
        if not ctx.is_connected:
            return
        key = await self._public_key()
        if not key:
            return

        store = ctx.advert_store
        # Arm fresh clocks so "never sent" becomes "counting from now" (no-op otherwise).
        store.arm(key, flood=False)
        store.arm(key, flood=True)

        policy = store.load(key)
        # One send per pass, direct first (the cheaper, zero-hop announcement); a flood
        # due at the same moment follows on the next pass, POLL_S later.
        for flood in (False, True):
            if policy.due(flood):
                device = await ctx.device()
                await device.send_advert(flood)
                store.mark_sent(key, flood=flood)
                kind = "flood" if flood else "zero-hop"
                ctx.log.info("advert scheduler: sent scheduled %s advert", kind)
                return

    async def _public_key(self) -> str:
        """The connected device's public key, probed once per connection (best-effort)."""
        device = self._ctx._device
        if device is None:
            return ""
        ident = id(device)
        if self._key_for is not None and self._key_for[0] == ident:
            return self._key_for[1]
        try:
            info = await device.get_self_info()
        except Exception:  # noqa: BLE001 - identity probe is best-effort; retry next pass
            return ""
        key = str(info.get("public_key") or "")
        if key:
            self._key_for = (ident, key)
        return key
