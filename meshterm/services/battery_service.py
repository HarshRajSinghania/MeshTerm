"""The battery poller: keep the status bar's fuel gauge current, quietly.

A session-long task that reads the connected companion's battery voltage on a slow
cadence and caches a small :class:`BatteryReading` for the header to draw (see
:func:`~meshterm.ui.widgets.battery_cell`). Like the advert scheduler and the Watchtower
it holds no device subscription — each pass just checks whether a device is connected and
skips quietly otherwise — so it is indifferent to reconnects, ticking across a link drop
and resuming once the radio is back.

Two device facts shape what it can report:

* Firmware exposes the pack as a **terminal voltage in millivolts**, not a percentage, so
  the reading is turned into a state-of-charge estimate against a single-cell LiPo
  discharge curve (:func:`battery_percent`). Devices with no battery gauge answer with no
  usable level; those are reported as *absent* so the header shows nothing for them.
* Firmware exposes **no charging flag at all**. Charging is therefore *inferred* from the
  terminal voltage trending upward across the recent sample window — the only dynamic
  signal available. It is an estimate, held steady through the flat stretches (a charger's
  constant-voltage phase, or an idle pack) so the gauge doesn't flap.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between battery reads. A pack drifts slowly, so a coarse tick is plenty; it is
#: also fine enough that the charging trend (measured over :data:`_TREND_WINDOW_S`) has
#: several samples to work with.
POLL_S = 20.0

#: Millivolts at or above which a reading counts as a real battery. A device with no fuel
#: gauge answers with an empty frame or a zero level; anything below a badly-depleted
#: single cell is treated as *no battery present* and the gauge is hidden.
_BATTERY_PRESENT_FLOOR_MV = 1500

#: Recent-samples window (seconds) the charging trend is measured over, and the rise across
#: it that flips the estimate to *charging* (or, as a fall, to *not charging*). Between the
#: two the last verdict is held, so a flat voltage — charger in constant-voltage phase, or a
#: resting pack — doesn't make the gauge flicker between the two states.
_TREND_WINDOW_S = 180.0
_CHARGING_RISE_MV = 15

#: How many samples to retain — the trend window plus a little slack at the poll cadence.
_TREND_SAMPLES = int(_TREND_WINDOW_S / POLL_S) + 2

#: Single-cell LiPo terminal-voltage → state-of-charge lookup ``(millivolts, percent)``,
#: high to low. The discharge curve is far from a straight line — most of the usable charge
#: sits in a narrow band around 3.7–3.9 V — so a piecewise table read with linear
#: interpolation tracks it far better than a flat voltage-to-percent line. These are the
#: widely-used community/Battery-University rungs.
_LIPO_SOC: tuple[tuple[int, int], ...] = (
    (4200, 100), (4150, 95), (4110, 90), (4080, 85), (4020, 80), (3980, 75),
    (3950, 70), (3910, 65), (3870, 60), (3850, 55), (3840, 50), (3820, 45),
    (3800, 40), (3790, 35), (3770, 30), (3750, 25), (3730, 20), (3710, 15),
    (3690, 10), (3610, 5), (3270, 0),
)


def battery_percent(millivolts: int) -> int:
    """Estimate a single-cell LiPo's state of charge (0–100%) from its terminal mV.

    Reads the :data:`_LIPO_SOC` curve, interpolating linearly between the two rungs the
    voltage falls between and clamping past either end.

    Args:
        millivolts: The pack's terminal voltage in millivolts.

    Returns:
        The estimated charge as a whole percent in ``[0, 100]``.
    """
    mv = int(millivolts)
    if mv >= _LIPO_SOC[0][0]:
        return 100
    if mv <= _LIPO_SOC[-1][0]:
        return 0
    for (v_hi, p_hi), (v_lo, p_lo) in zip(_LIPO_SOC, _LIPO_SOC[1:], strict=False):
        if v_lo <= mv <= v_hi:
            frac = (mv - v_lo) / (v_hi - v_lo)
            return round(p_lo + (p_hi - p_lo) * frac)
    return 0  # pragma: no cover - the clamps above cover the whole range


@dataclass(frozen=True)
class BatteryReading:
    """One cached battery snapshot for the header to draw.

    Attributes:
        millivolts: The pack's terminal voltage, as the firmware reported it.
        percent: The state-of-charge estimate (see :func:`battery_percent`).
        charging: Whether the pack appears to be taking charge (inferred from a rising
            terminal voltage — the firmware exposes no charging flag).
    """

    millivolts: int
    percent: int
    charging: bool


class BatteryService:
    """Polls the connected companion's battery and caches a reading for the header.

    Interact through the async lifecycle methods (:meth:`start`, :meth:`stop`,
    :meth:`aclose`) and read the latest snapshot with :meth:`reading`; everything else
    happens on the loop's own schedule.
    """

    def __init__(self, ctx: "AppContext") -> None:
        """Initialize an idle poller bound to an application context.

        Args:
            ctx: The shared application context, read each pass for the connected device.
                Nothing is touched until :meth:`start`.
        """
        self._ctx = ctx
        self._task: Optional[asyncio.Task] = None
        self._reading: Optional[BatteryReading] = None
        #: Recent ``(monotonic_time, millivolts)`` samples, for the charging trend.
        self._history: deque[tuple[float, int]] = deque(maxlen=_TREND_SAMPLES)

    @property
    def active(self) -> bool:
        """Whether the poll loop is running."""
        return self._task is not None and not self._task.done()

    def reading(self) -> Optional[BatteryReading]:
        """The latest battery snapshot, or ``None`` when unknown or no battery is present."""
        return self._reading

    async def start(self) -> None:
        """Start the poll loop. Idempotent; never blocks on the first read."""
        if self.active:
            return
        self._task = asyncio.ensure_future(self._run())
        self._ctx.log.info("battery poller started")

    async def stop(self) -> None:
        """Stop the poll loop. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._ctx.log.info("battery poller stopped")

    async def aclose(self) -> None:
        """Stop the loop at session end (an alias for :meth:`stop`)."""
        await self.stop()

    async def _run(self) -> None:
        """Read once at once, then tick forever: a best-effort pass, then sleep."""
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad read must not kill the loop
                self._ctx.log.debug("battery poller: read failed: %s", exc)
            await asyncio.sleep(POLL_S)

    async def _poll(self) -> None:
        """One pass: read the pack, estimate charge, and re-derive the charging trend."""
        ctx = self._ctx
        if not ctx.is_connected:
            return
        device = await ctx.device()
        info = await device.get_battery()
        mv = int(info.get("level") or 0)
        if mv < _BATTERY_PRESENT_FLOOR_MV:
            # No usable level — this companion has no battery gauge; report absence so the
            # header draws nothing, and forget any stale trend from a different device.
            self._reading = None
            self._history.clear()
            return
        now = time.monotonic()
        self._history.append((now, mv))
        self._reading = BatteryReading(
            millivolts=mv,
            percent=battery_percent(mv),
            charging=self._charging(now),
        )

    def _charging(self, now: float) -> bool:
        """Infer whether the pack is charging from its recent terminal-voltage trend.

        The firmware exposes no charging flag, so this is the best available signal: a
        clear rise across the window means charge is going in, a clear fall means it is
        coming out, and a flat stretch holds the previous verdict (so a charger's
        constant-voltage phase or a resting pack doesn't make the gauge flap).

        Args:
            now: The current monotonic time (the just-taken sample's timestamp).

        Returns:
            The charging estimate for this reading.
        """
        last = self._reading.charging if self._reading is not None else False
        recent = [(t, mv) for t, mv in self._history if now - t <= _TREND_WINDOW_S]
        if len(recent) < 2:
            return last
        rise = recent[-1][1] - recent[0][1]
        if rise >= _CHARGING_RISE_MV:
            return True
        if rise <= -_CHARGING_RISE_MV:
            return False
        return last
