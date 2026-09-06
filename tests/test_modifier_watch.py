"""The F-key lane's Shift-state watcher: the raw flag and the shift-bank latch."""

from __future__ import annotations

from meshterm.services import modifier_watch


def _reset() -> None:
    modifier_watch._shift_down = False
    modifier_watch._last_shift_bank_at = None


def test_shift_down_reflects_the_raw_flag() -> None:
    _reset()
    try:
        assert modifier_watch.shift_down() is False
        modifier_watch._shift_down = True
        assert modifier_watch.shift_down() is True
    finally:
        _reset()


def test_shift_bank_key_latches_over_the_release_flicker(monkeypatch) -> None:
    """An F6-F10 press keeps the lane's shifted read for a grace window, even if the raw
    watcher (wrongly, per the MCU quirk) reports Shift already back up.
    """
    _reset()
    try:
        clock = [100.0]
        monkeypatch.setattr(modifier_watch.time, "monotonic", lambda: clock[0])

        assert modifier_watch.shift_down() is False
        modifier_watch.note_shift_bank_key()
        assert modifier_watch.shift_down() is True  # latched, raw flag never went true

        clock[0] += modifier_watch._SHIFT_BANK_GRACE_S - 0.05
        assert modifier_watch.shift_down() is True  # still inside the grace window

        clock[0] += 0.1
        assert modifier_watch.shift_down() is False  # grace lapsed, Shift really let go
    finally:
        _reset()
