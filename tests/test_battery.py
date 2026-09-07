"""Battery gauge tests: the LiPo curve, the one-cell braille glyph, and the poller.

The widget is pure (percent + flags → a coloured glyph) and the poller runs against a
fake context, so charge estimation, the charging sweep, the low-battery blink, and the
charging inference are all assertable without a device or a terminal.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from meshterm.core.connection import charging_from_battery_level_status
from meshterm.services import battery_service
from meshterm.services.battery_service import (
    _BATTERY_PRESENT_FLOOR_MV,
    POLL_S,
    BatteryService,
    battery_percent,
)
from meshterm.ui.widgets import _BATTERY_FLASH_PCT, battery_cell

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


def _glyph_style(cell) -> str:
    """The style on the gauge's glyph span — the cell's colour, not the percent's."""
    return str(cell.spans[0].style)


def test_battery_cell_fills_in_eight_half_row_steps() -> None:
    """One rung per 12.5%: each dot row lights left first, then right, bottom to top."""
    assert battery_cell(100).plain[0] == "⣿"  # all 8 dots
    assert battery_cell(90).plain[0] == "⣿"
    assert battery_cell(80).plain[0] == chr(0x2800 | 0xF7)  # 7 rungs: + the top row's left
    assert battery_cell(70).plain[0] == chr(0x2800 | 0xF6)  # 6 rungs: the bottom three rows
    assert battery_cell(60).plain[0] == chr(0x2800 | 0xE6)  # 5 rungs: + a left dot
    assert battery_cell(45).plain[0] == chr(0x2800 | 0xE4)  # 4 rungs: the bottom two rows
    assert battery_cell(30).plain[0] == chr(0x2800 | 0xC4)  # 3 rungs: + a left dot
    assert battery_cell(20).plain[0] == chr(0x2800 | 0xC0)  # 2 rungs: the bottom row
    assert battery_cell(5).plain[0] == chr(0x2800 | 0x40)  # 1 rung: the bottom-left dot
    # A dead-flat pack still lights that one dot rather than drawing an empty cell.
    assert battery_cell(0).plain[0] == chr(0x2800 | 0x40)


def test_battery_cell_steps_on_every_eighth_of_the_pack() -> None:
    """The fill is monotonic and really does carry eight distinct levels, 12.5% apart."""
    fills = [battery_cell(pct).plain[0] for pct in range(101)]
    assert len(set(fills)) == 8
    # Each band takes its lower bound: 12.5% is the second rung, 87.5% the eighth.
    assert fills[12] != fills[13] and fills[13] == fills[24] != fills[25]
    assert fills[87] != fills[88] and fills[88] == fills[100]


def test_battery_cell_shows_the_true_percent() -> None:
    """The number beside the glyph is the actual charge."""
    assert battery_cell(42).plain == chr(0x2800 | 0xE4) + " 42%"


def test_battery_cell_colours_by_band_while_the_dots_carry_the_detail() -> None:
    """Three broad bands over half / over a quarter / below, each a block of its own hue."""
    assert _glyph_style(battery_cell(100)) == "batt.full"  # light green on green
    assert _glyph_style(battery_cell(50)) == "batt.full"
    assert _glyph_style(battery_cell(49)) == "batt.mid"  # yellow on brown
    assert _glyph_style(battery_cell(25)) == "batt.mid"
    assert _glyph_style(battery_cell(24)) == "batt.low"  # light red on red
    assert _glyph_style(battery_cell(7)) == "batt.low"
    # The band holds across a band's worth of rungs, so only the fill moves inside it.
    # (Below 6.25% the alarm takes the cell over, which the next test covers.)
    turns = [
        p
        for p in range(8, 101)
        if _glyph_style(battery_cell(p)) != _glyph_style(battery_cell(p - 1))
    ]
    assert turns == [25, 50]
    assert len({battery_cell(p).plain[0] for p in range(25, 50)}) == 2  # two rungs per band


def test_battery_cell_alarm_drops_the_block_on_alternate_frames() -> None:
    """Under 6.25% the cell alternates black-on-red with the light red on the bare page."""
    # Half a rung: a rung is an eighth of the pack, so the alarm line is 6.25%.
    assert _BATTERY_FLASH_PCT == 6.25
    assert _glyph_style(battery_cell(6, frame=0)) == "batt.flash"  # black dots on red
    assert _glyph_style(battery_cell(6, frame=1)) == "batt.flash.off"  # light red, no ground
    assert _glyph_style(battery_cell(0, frame=1)) == "batt.flash.off"
    # Everything above the line holds its band, however low it is.
    assert {_glyph_style(battery_cell(7, frame=f)) for f in range(4)} == {"batt.low"}
    assert {_glyph_style(battery_cell(40, frame=f)) for f in range(4)} == {"batt.mid"}


def test_battery_cell_charging_sweeps_bottom_to_full_holding_the_percent() -> None:
    """Charging animates the fill empty→full on a loop while the % stays the true charge."""
    fills = [battery_cell(20, charging=True, frame=f).plain[0] for f in range(6)]
    assert fills[0] == chr(0x2800)  # empty
    assert fills[4] == "⣿"  # full
    assert fills[5] == fills[0]  # loops back
    assert battery_cell(20, charging=True, frame=4).plain.endswith(" 20%")


def test_battery_cell_charging_keeps_the_bands_colour_while_the_fill_sweeps() -> None:
    """The sweep is an animation, not a reading: colour still answers for the real charge."""
    charging = {_glyph_style(battery_cell(20, charging=True, frame=f)) for f in range(5)}
    assert charging == {"batt.low"}
    charging = {_glyph_style(battery_cell(40, charging=True, frame=f)) for f in range(5)}
    assert charging == {"batt.mid"}
    charging = {_glyph_style(battery_cell(90, charging=True, frame=f)) for f in range(5)}
    assert charging == {"batt.full"}


def test_battery_cell_charging_never_alarms_however_empty_the_pack_is() -> None:
    """A pack taking charge says so with the sweep; the alarm would only fight it."""
    for pct in (0, 3, 6, 10, 20):
        styles = {_glyph_style(battery_cell(pct, charging=True, frame=f)) for f in range(10)}
        assert styles == {"batt.low"}


def test_battery_cell_animates_off_the_frame_counter_alone() -> None:
    """Both live states run on every platform: no effects flag, only the repaint cadence."""
    fills = [battery_cell(20, charging=True, frame=f).plain[0] for f in range(5)]
    assert len(set(fills)) == 5  # the sweep really climbs, rather than holding one frame
    assert len({_glyph_style(battery_cell(3, frame=f)) for f in range(2)}) == 2  # and alarms


def test_battery_cell_at_full_rests_full_however_the_charging_flag_reads() -> None:
    """A topped-off pack draws as not charging, so a plugged-in charger stops the sweep."""
    assert battery_cell(100, charging=True, frame=0).plain == "⣿ 100%"
    assert {battery_cell(100, charging=True, frame=f).plain for f in range(6)} == {"⣿ 100%"}
    # One percent short is still filling, and still sweeps.
    assert battery_cell(99, charging=True, frame=0).plain[0] == chr(0x2800)


# --- the poller ---------------------------------------------------------------------------


def _service(levels, hw_charging=None) -> BatteryService:
    """A ``BatteryService`` over a fake context whose device answers ``levels`` in turn.

    ``hw_charging`` is what the device's firmware charging flag reports each poll — ``None``
    (the default, and every real MeshCore device) means "no flag", so the poller infers.
    """
    seq = list(levels)

    async def get_battery() -> dict:
        return {"level": seq.pop(0)} if seq else {}

    async def get_hw_charging():
        return hw_charging

    async def device() -> SimpleNamespace:
        return SimpleNamespace(get_battery=get_battery, get_hw_charging=get_hw_charging)

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


def _history_of(now: float, mvs: list[int]) -> list[tuple[float, int]]:
    """``(time, mv)`` samples one poll apart, oldest first, the last landing at ``now``."""
    n = len(mvs)
    return [(now - (n - 1 - i) * POLL_S, mv) for i, mv in enumerate(mvs)]


def test_battery_poller_calls_charging_only_on_a_strong_sustained_rise() -> None:
    """A large, sustained climb reads as charging; a gentle one only holds a verdict already set."""
    svc = _service([])
    now = 1000.0

    # A clear, sustained rise from a cold (not-charging) start crosses the ON threshold.
    svc._history.extend(_history_of(now, [3700, 3712, 3724, 3736, 3748, 3760, 3772, 3784]))
    assert svc._charging(now) is True

    # A gentle climb (over the hold floor, under the start floor) will not *start* a verdict…
    svc._reading = SimpleNamespace(charging=False)
    svc._history.clear()
    svc._history.extend(_history_of(now, [3900, 3902, 3904, 3915, 3917, 3919]))
    assert svc._charging(now) is False
    # …but it *holds* one already in flight, so a real charge doesn't flicker on poll jitter.
    svc._reading = SimpleNamespace(charging=True)
    assert svc._charging(now) is True


def test_battery_poller_charging_ignores_load_sag_and_flat_or_falling_packs() -> None:
    """A transient TX sag, a flat pack, and a falling pack all read as *not charging*."""
    svc = _service([])
    now = 1000.0

    # A single deep TX sag at the window's start would fool a raw first-to-last diff into
    # seeing a +60 mV "rise"; the median of each half discards the spike, so it does not.
    svc._reading = SimpleNamespace(charging=False)
    svc._history.extend(_history_of(now, [3740, 3802, 3798, 3801, 3800, 3799, 3802, 3800]))
    assert svc._charging(now) is False

    # A pack held flat (topped off, or unplugged) drops a charging verdict back off.
    svc._reading = SimpleNamespace(charging=True)
    svc._history.clear()
    svc._history.extend(_history_of(now, [3800, 3801, 3800, 3799, 3800, 3801]))
    assert svc._charging(now) is False

    # A clear discharge never reads as charging.
    svc._reading = SimpleNamespace(charging=True)
    svc._history.clear()
    svc._history.extend(_history_of(now, [3900, 3890, 3880, 3870, 3860, 3850]))
    assert svc._charging(now) is False

    # Too little history to span the window is *not charging*, whatever the last verdict.
    svc._reading = SimpleNamespace(charging=True)
    svc._history.clear()
    svc._history.extend(_history_of(now, [3800, 3900]))
    assert svc._charging(now) is False


# --- the host pack (the PicoCalc's own power_supply) ---------------------------------------


def _host_supply(tmp_path, monkeypatch, **files: str):
    """Stand a fake ``power_supply`` directory up and point the poller at it."""
    supply = tmp_path / "picocalc"
    supply.mkdir(exist_ok=True)
    for name, value in files.items():
        (supply / name).write_text(f"{value}\n")
    monkeypatch.setattr(battery_service, "_HOST_SUPPLY", supply)
    return supply


def test_host_pack_reads_the_drivers_own_percent_and_charging_flag(tmp_path, monkeypatch) -> None:
    """On the handheld both numbers are the device's: no LiPo curve, no voltage trend."""
    monkeypatch.setattr(battery_service, "get_platform", lambda: SimpleNamespace(battery="host"))
    svc = _service([])
    # A discharging pack, as the driver reports it (no voltage_now on this one — it has none).
    _host_supply(tmp_path, monkeypatch, capacity="76", status="Discharging")
    asyncio.run(svc._poll())
    assert svc.reading().percent == 76 and svc.reading().charging is False
    # Plugged in: the flag flips on the driver's word alone, with no history to trend over.
    _host_supply(tmp_path, monkeypatch, capacity="22", status="Charging")
    asyncio.run(svc._poll())
    assert svc.reading().charging is True
    assert not svc._history  # the trend machinery never runs on this path
    # A topped-off pack is *not* taking charge, whatever is plugged into it.
    _host_supply(tmp_path, monkeypatch, capacity="100", status="Full")
    asyncio.run(svc._poll())
    assert svc.reading().percent == 100 and svc.reading().charging is False


def test_host_pack_absent_when_the_supply_cant_be_read(tmp_path, monkeypatch) -> None:
    """No driver (or an unreadable one) reports *no battery*, so the header draws no gauge."""
    monkeypatch.setattr(battery_service, "get_platform", lambda: SimpleNamespace(battery="host"))
    svc = _service([])
    monkeypatch.setattr(battery_service, "_HOST_SUPPLY", tmp_path / "nothing-here")
    asyncio.run(svc._poll())
    assert svc.reading() is None
    # A supply that answers with junk is just as absent — never a bogus gauge.
    _host_supply(tmp_path, monkeypatch, capacity="", status="Charging")
    asyncio.run(svc._poll())
    assert svc.reading() is None


# --- the standard BLE charging flag (GATT Battery Level Status, 0x2BED) --------------------


def _level_status(charge_state: int, *, flags: int = 0, extra: bytes = b"") -> bytes:
    """A Battery Level Status value: flags byte, the Power State word, then optional tail."""
    power_state = (charge_state & 0b11) << 5
    return bytes([flags]) + power_state.to_bytes(2, "little") + extra


def test_charging_flag_decodes_the_charge_state_field() -> None:
    """The 2-bit Charge State enum maps to charging / not / unknown per the GSS."""
    assert charging_from_battery_level_status(_level_status(1)) is True  # charging
    assert charging_from_battery_level_status(_level_status(2)) is False  # discharging (active)
    assert charging_from_battery_level_status(_level_status(3)) is False  # discharging (inactive)
    assert charging_from_battery_level_status(_level_status(0)) is None  # unknown → infer


def test_charging_flag_reads_only_the_power_state_word() -> None:
    """Other Power State bits and the optional identifier/level tail don't sway the verdict."""
    # Battery present + wired external power + charging, with identifier & level bytes trailing.
    value = _level_status(1, flags=0b011, extra=b"\xab\xcd\x50")
    assert charging_from_battery_level_status(value) is True
    # A short value (no room for the Power State word) is unknown, never a guess.
    assert charging_from_battery_level_status(b"\x00") is None
    assert charging_from_battery_level_status(b"") is None


def test_battery_poller_prefers_the_hardware_charging_flag_over_inference() -> None:
    """A firmware charging flag wins over the voltage-trend guess, in both directions."""
    now = time.monotonic()
    rising = [3700, 3712, 3724, 3736, 3748, 3760, 3772, 3784]

    # Baseline: with no hardware flag the rising trend is *inferred* as charging.
    svc = _service([3900], hw_charging=None)
    svc._history.extend(_history_of(now, rising))
    asyncio.run(svc._poll())
    assert svc.reading().charging is True

    # Same rising trend, but the hardware reports NOT charging → the flag overrides the guess.
    svc = _service([3900], hw_charging=False)
    svc._history.extend(_history_of(now, rising))
    asyncio.run(svc._poll())
    assert svc.reading().charging is False

    # And a hardware "charging" wins with no trend at all, where inference would say not.
    svc = _service([3900], hw_charging=True)
    asyncio.run(svc._poll())
    assert svc.reading().charging is True
