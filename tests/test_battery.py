"""Battery gauge tests: the LiPo curve, the one-cell braille glyph, and the poller.

The widget is pure (percent + flags → a coloured glyph) and the poller runs against a
fake context, so charge estimation, the charging sweep, the low-battery blink, and the
charging inference are all assertable without a device or a terminal.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from meshterm.services.battery_service import (
    _BATTERY_PRESENT_FLOOR_MV,
    _CHARGING_RISE_MV,
    BatteryService,
    battery_percent,
)
from meshterm.ui.widgets import _BATTERY_CRITICAL, battery_cell

# --- the LiPo state-of-charge curve -------------------------------------------------------


def test_battery_percent_clamps_past_either_end() -> None:
    """Above the top rung reads 100%, below the bottom rung reads 0%."""
    assert battery_percent(4300) == 100
    assert battery_percent(4200) == 100
    assert battery_percent(3270) == 0
    assert battery_percent(3000) == 0


def test_battery_percent_interpolates_and_stays_monotonic() -> None:
    """A rung reads exactly; between rungs interpolates; more volts is never less charge."""
    assert battery_percent(3840) == 50  # an exact rung
    assert 40 < battery_percent(3810) < 45  # between the 3820/45 and 3800/40 rungs
    pcts = [battery_percent(mv) for mv in range(3270, 4201, 10)]
    assert pcts == sorted(pcts)


# --- the one-cell braille glyph -----------------------------------------------------------


def test_battery_cell_fills_from_the_bottom_by_tier() -> None:
    """Each tier drops dot rows off the top: full, six, four, two dots lit."""
    assert battery_cell(100).plain[0] == "⣿"                 # all 8 dots
    assert battery_cell(90).plain[0] == "⣿"
    assert battery_cell(60).plain[0] == chr(0x2800 | 0xF6)   # bottom 6 dots
    assert battery_cell(30).plain[0] == chr(0x2800 | 0xE4)   # bottom 4 dots
    assert battery_cell(10).plain[0] == chr(0x2800 | 0xC0)   # bottom 2 dots


def test_battery_cell_shows_the_true_percent() -> None:
    """The number beside the glyph is the actual charge."""
    assert battery_cell(42).plain == chr(0x2800 | 0xE4) + " 42%"


def test_battery_cell_colours_by_tier() -> None:
    """Green healthy, orange at a quarter, red near empty — the fuel-gauge palette."""
    assert str(battery_cell(90).style) == "batt.high"   # green
    assert str(battery_cell(60).style) == "batt.high"   # green
    assert str(battery_cell(30).style) == "batt.mid"    # orange
    assert str(battery_cell(5).style) == "batt.low"     # red


def test_battery_cell_blinks_when_critically_low() -> None:
    """Below the critical line the red glyph flickers to a dim slate on alternate frames."""
    assert 5 <= _BATTERY_CRITICAL
    assert str(battery_cell(5, frame=0).style) == "batt.low"  # the on-beat, red
    assert str(battery_cell(5, frame=1).style) == "batt.dim"  # the off-beat, dim
    # Above the critical line the glyph never blinks.
    assert {str(battery_cell(40, frame=f).style) for f in range(4)} == {"batt.mid"}


def test_battery_cell_charging_sweeps_bottom_to_full_holding_the_percent() -> None:
    """Charging animates the fill empty→full on a loop while the % stays the true charge."""
    fills = [battery_cell(20, charging=True, frame=f).plain[0] for f in range(6)]
    assert fills[0] == chr(0x2800)   # empty
    assert fills[4] == "⣿"            # full
    assert fills[5] == fills[0]       # loops back
    assert battery_cell(20, charging=True, frame=4).plain.endswith(" 20%")
    # The charge colour tracks the real state of charge (20% → red), held across the sweep.
    assert {str(battery_cell(20, charging=True, frame=f).style) for f in range(6)} == {"batt.low"}


# --- the poller ---------------------------------------------------------------------------


def _service(levels) -> BatteryService:
    """A ``BatteryService`` over a fake context whose device answers ``levels`` in turn."""
    seq = list(levels)

    async def get_battery() -> dict:
        return {"level": seq.pop(0)} if seq else {}

    async def device() -> SimpleNamespace:
        return SimpleNamespace(get_battery=get_battery)

    ctx = SimpleNamespace(
        is_connected=True,
        device=device,
        log=SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None),
    )
    return BatteryService(ctx)


def test_battery_poller_reads_and_estimates() -> None:
    """One pass reads the pack and caches a charge estimate; charging is off with one sample."""
    svc = _service([3840])  # 50%
    asyncio.run(svc._poll())
    reading = svc.reading()
    assert reading is not None
    assert reading.millivolts == 3840 and reading.percent == 50 and reading.charging is False


def test_battery_poller_hides_a_device_with_no_pack() -> None:
    """An empty or sub-floor level reads as *no battery*, so the header shows nothing."""
    svc = _service([0])
    asyncio.run(svc._poll())
    assert svc.reading() is None
    svc = _service([_BATTERY_PRESENT_FLOOR_MV - 1])
    asyncio.run(svc._poll())
    assert svc.reading() is None


def test_battery_poller_infers_charging_from_the_voltage_trend() -> None:
    """A clear rise reads as charging, a clear fall as not, a flat stretch holds the verdict."""
    svc = _service([])
    now = 1000.0
    svc._history.extend([(now - 120, 3700), (now, 3700 + _CHARGING_RISE_MV)])
    assert svc._charging(now) is True  # rising → charging

    svc._reading = SimpleNamespace(charging=True)
    svc._history.clear()
    svc._history.extend([(now - 120, 3900), (now, 3900 - _CHARGING_RISE_MV)])
    assert svc._charging(now) is False  # falling → not charging

    svc._reading = SimpleNamespace(charging=True)
    svc._history.clear()
    svc._history.extend([(now - 60, 3800), (now, 3803)])
    assert svc._charging(now) is True  # flat → holds the last verdict
