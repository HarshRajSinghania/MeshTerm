# SPDX-License-Identifier: Apache-2.0
"""Set the companion's clock from this computer — by hand from Device config, or on connect.

Most MeshCore boards carry no real-time clock: a power cycle puts them back at whatever
epoch the firmware boots with, and every message they then send is stamped in a year the
mesh does not agree on. :func:`set_clock` is the one device-side step that corrects it —
read what the radio thinks the time is, write ours, and hand back the drift that was
corrected, the fact worth keeping and the one the set destroys by succeeding. The Device
config screen's *Sync clock* action (``config sync-clock`` on the command line) calls it
and acknowledges on screen; :class:`ClockSync` calls the same function on its own, once
per connection, when the ``set_clock_on_connect`` preference asks for it (off by default —
writing to a radio that nobody asked to be written to is a choice the owner makes).

The automatic set is a background step, not a startup step: the connect path hands the
device over and carries on, and the write happens in a task of its own so a slow or silent
firmware never holds the menu. It reports in the **log**, never on screen.

Once per *connection*, keyed by the device object's identity like the advert scheduler's
key probe: a reconnect after a link drop is a fresh :class:`~meshterm.core.connection.Device`
and gets its own set, because a drop is exactly when the board may have rebooted and lost
its clock again. The interactive session starts the service; scripted CLI runs never do,
so a one-shot command cannot change a radio it was only asked to read.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device


@dataclass(frozen=True, slots=True)
class ClockSet:
    """What one clock set did.

    Attributes:
        epoch: The UNIX time written to the device.
        drift_s: How far the device's clock was from ours *before* the set (device minus
            host, seconds), or ``None`` where the firmware would not report its clock.
    """

    epoch: int
    drift_s: int | None

    @property
    def set_at(self) -> datetime:
        """The written instant, in this computer's local time."""
        return datetime.fromtimestamp(self.epoch).astimezone()

    @property
    def stamp(self) -> str:
        """The written instant as the acknowledgement and the log print it."""
        return self.set_at.strftime("%Y-%m-%d %H:%M:%S")


async def set_clock(device: Device) -> ClockSet:
    """Set ``device``'s clock to this computer's, returning what was corrected.

    The read of the device's own clock before the write is optional — the drift is a
    nicety, and a firmware that will not answer still gets its clock set.

    Args:
        device: A connected device.

    Returns:
        The instant written and the drift it corrected.

    Raises:
        Whatever the device raises when the write itself fails.
    """
    try:
        before = await device.get_time()
    except Exception:  # noqa: BLE001 - optional read; the drift is a nicety
        before = None
    epoch = int(time.time())
    await device.set_time(epoch)
    return ClockSet(epoch=epoch, drift_s=None if before is None else before - epoch)


class ClockSync:
    """Owns the on-connect clock set for an interactive session.

    Interact through :meth:`start`, :meth:`stop`/:meth:`aclose`, and
    :meth:`on_connected`, which the context calls as each connection settles.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize an idle service bound to an application context.

        Args:
            ctx: The shared application context, read for the preference at each
                connection and for the logger. Nothing is touched until :meth:`start`.
        """
        self._ctx = ctx
        self._armed = False
        self._task: asyncio.Task | None = None
        # Identity of the device object last set, so one connection is set once even if
        # the connect path reports it more than once.
        self._synced: int | None = None

    @property
    def active(self) -> bool:
        """Whether the service is armed to act on connections."""
        return self._armed

    async def start(self) -> None:
        """Arm the service, setting the clock at once if a device is already connected.

        Idempotent. The startup picker may have adopted a live connection before any
        service started, so the connection that already exists counts as the first one.
        """
        if self._armed:
            return
        self._armed = True
        device = self._ctx._device
        if device is not None:
            self.on_connected(device)

    async def stop(self) -> None:
        """Disarm, cancelling a set still in flight. Idempotent."""
        self._armed = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def aclose(self) -> None:
        """Stop at session end (an alias for :meth:`stop`)."""
        await self.stop()

    def on_connected(self, device: Device) -> None:
        """Note a settled connection and, if the preference asks, set its clock in the background.

        Returns at once; the set runs in its own task. A no-op while the service is not
        started (scripted runs), while the preference is off, and for a device this
        session has already set.

        Args:
            device: The device the context just finished connecting.
        """
        if not self._armed or not self._ctx.preferences.set_clock_on_connect:
            return
        if self._synced == id(device):
            return
        self._synced = id(device)
        self._task = asyncio.ensure_future(self._sync(device))

    async def _sync(self, device: Device) -> None:
        """Run :func:`set_clock` and put the outcome in the log."""
        log = self._ctx.log
        try:
            done = await set_clock(device)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - best-effort; the link is not ours to break
            log.warning("clock sync: could not set the device clock: %s", exc)
            return
        if done.drift_s is None:
            log.info("clock sync: device clock set to %s (drift unknown)", done.stamp)
        else:
            log.info("clock sync: device clock set to %s (was %+d s off)", done.stamp, done.drift_s)
