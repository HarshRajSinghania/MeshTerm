"""Tests for the transmit cooldown: the shared clock, the countdown, and the way out.

Three layers again — what the clock owes (:mod:`meshterm.core.transmit_gate`), what the
dialog draws, and what :func:`~meshterm.ui.cooldown.wait_for_cooldown` decides at each
side of its threshold — plus the check that a real transmission is what starts the clock,
so the gate can never be a number nothing sets.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from meshterm.core import transmit_gate
from meshterm.core.preferences import Preferences, get_spec, install
from meshterm.core.transmit_gate import TransmitGate
from meshterm.ui.cooldown import SILENT_WAIT_S, wait_for_cooldown
from meshterm.ui.tui.prompt import CountdownDialog
from meshterm.ui.tui.screen import CANCEL
from tests.conftest import plain as _plain


@pytest.fixture(autouse=True)
def _defaults():
    """Every test starts from an idle clock and the built-in preferences."""
    install(Preferences())
    transmit_gate.current().reset()
    yield
    install(Preferences())
    transmit_gate.current().reset()


# -- the clock -------------------------------------------------------------------


def test_an_idle_gate_owes_nothing() -> None:
    """Nothing sent, nothing to wait for — a fresh session transmits at once."""
    gate = TransmitGate()
    assert gate.remaining() == 0.0
    assert gate.remaining(flood_advert=True) == 0.0


def test_a_send_starts_the_general_clock() -> None:
    """Any transmission holds the next one off by the transmit cooldown."""
    gate = TransmitGate()
    gate.mark()
    assert gate.remaining() == pytest.approx(5.0, abs=0.5)


def test_a_flood_advert_waits_out_both_clocks() -> None:
    """A flood advert owes its own longer wait; an ordinary send still owes only the short one."""
    gate = TransmitGate()
    gate.mark(flood_advert=True)
    assert gate.remaining() == pytest.approx(5.0, abs=0.5)
    assert gate.remaining(flood_advert=True) == pytest.approx(60.0, abs=0.5)


def test_an_ordinary_send_does_not_arm_the_flood_clock() -> None:
    """Sending a message shouldn't cost you a minute before you may advertise."""
    gate = TransmitGate()
    gate.mark()
    assert gate.remaining(flood_advert=True) == pytest.approx(5.0, abs=0.5)


def test_the_clocks_follow_the_preferences() -> None:
    """Both waits are read live, so a change on the Preferences page applies at once."""
    prefs = Preferences()
    prefs.set("trace_cooldown_s", 0.1)
    prefs.set("flood_advert_cooldown_s", 30.0)
    install(prefs)
    gate = TransmitGate()
    gate.mark(flood_advert=True)
    assert gate.remaining() == pytest.approx(0.1, abs=0.05)
    assert gate.remaining(flood_advert=True) == pytest.approx(30.0, abs=0.5)


def test_reset_clears_both_clocks() -> None:
    """Reset returns a gate to never-sent (for a fresh session, and for these tests)."""
    gate = TransmitGate()
    gate.mark(flood_advert=True)
    gate.reset()
    assert gate.remaining(flood_advert=True) == 0.0


def test_the_bounds_say_what_may_be_asked_for() -> None:
    """The general cooldown has a floor rather than an off switch; a flood advert's is higher."""
    assert get_spec("trace_cooldown_s").minimum == 0.1
    assert get_spec("flood_advert_cooldown_s").minimum == 5.0


# -- the dialog ------------------------------------------------------------------


def _lines(dialog: CountdownDialog) -> list[str]:
    """The dialog's body as plain text."""
    return _plain(dialog.render_body(dialog.dialog_width - 8)).splitlines()


def test_the_countdown_shows_the_clock_its_reason_and_the_way_out() -> None:
    """What is waiting, how long is left, why, and the one chip that abandons it."""
    dialog = CountdownDialog("Flood advert", 47.0, reason="reaches the whole mesh")
    assert dialog.title == "Flood advert"
    body = _lines(dialog)
    assert "Ready in 47s" in body[0]
    assert "reaches the whole mesh" in body[1]
    assert any("Cancel" in line for line in body)
    # Both keys do the one thing on offer, so they share an atom instead of repeating it.
    assert dialog.footer_hint == "Enter/Esc cancel"


def test_the_clock_its_reason_and_the_chip_are_all_centered() -> None:
    """Centered like every other dialog's body — a chip against the left border read as odd."""
    dialog = CountdownDialog("Flood advert", 47.0, reason="reaches the whole mesh")
    width = dialog.dialog_width - 8
    for line in _lines(dialog):
        if not line.strip():
            continue
        left = len(line) - len(line.lstrip(" "))
        right = width - len(line.rstrip(" "))
        assert abs(left - right) <= 1, line


def test_a_part_second_still_reads_as_a_second() -> None:
    """The clock counts the way a person would say it: 0.2s left is still "1s"."""
    assert "Ready in 1s" in _lines(CountdownDialog("Advert", 0.2))[0]
    assert "Ready in 3s" in _lines(CountdownDialog("Advert", 2.4))[0]


def test_the_box_does_not_resize_as_it_counts() -> None:
    """Sized once, from the opening clock — a box that shrinks under you is harder to read."""
    dialog = CountdownDialog("Flood advert", 47.0, reason="reaches the whole mesh")
    width = dialog.dialog_width
    dialog.set_remaining(3.0)
    assert dialog.dialog_width == width


def test_the_countdown_resolves_itself_at_zero() -> None:
    """Reaching zero is the dialog's own answer: the wait is over, the action goes ahead."""
    dialog = CountdownDialog("Advert", 5.0)
    dialog.future = asyncio.get_event_loop_policy().new_event_loop().create_future()
    dialog.set_remaining(3.0)
    assert not dialog.future.done()
    dialog.set_remaining(0.0)
    assert dialog.future.done() and dialog.future.result() is True


@pytest.mark.parametrize("key", ["enter", "escape"])
def test_either_key_abandons_the_wait(key: str) -> None:
    """There is nothing to choose between, so Enter and Esc both mean cancel."""
    dialog = CountdownDialog("Advert", 30.0)
    dialog.future = asyncio.get_event_loop_policy().new_event_loop().create_future()
    dialog.handle(key)
    assert dialog.future.done() and dialog.future.result() is CANCEL


# -- the wait --------------------------------------------------------------------


class _Session:
    """A session that answers ``run_screen`` from a script and records what it was shown."""

    def __init__(self, answer: Any = True) -> None:
        self.answer = answer
        self.shown: list[CountdownDialog] = []

    async def run_screen(self, screen: Any) -> Any:
        self.shown.append(screen)
        return self.answer

    def invalidate(self) -> None:
        pass


class _Ui:
    """A UI surface carrying nothing but a session (all the wait helper reads)."""

    def __init__(self, session: Optional[_Session]) -> None:
        self.session = session


class _Ctx:
    """The slice of :class:`~meshterm.context.AppContext` the wait helper touches."""

    def __init__(self, session: Optional[_Session]) -> None:
        self.ui = _Ui(session)


async def test_no_cooldown_goes_straight_through() -> None:
    """Nothing owed, nothing shown: the action fires with no dialog in the way."""
    session = _Session()
    assert await wait_for_cooldown(_Ctx(session), action="Advert") is True
    assert session.shown == []


async def test_a_short_wait_passes_without_a_word() -> None:
    """Under the threshold the wait just happens: a dialog that flashes up *is* the interruption."""
    prefs = Preferences()
    prefs.set("trace_cooldown_s", 0.3)  # comfortably under SILENT_WAIT_S
    install(prefs)
    transmit_gate.mark()
    session = _Session()
    assert await wait_for_cooldown(_Ctx(session), action="Advert") is True
    assert session.shown == []
    # And it really did wait it out. Not exactly zero: the sleep is for the wait read a
    # few instructions earlier, so a millisecond or two of it is still nominally owed.
    assert transmit_gate.remaining() < 0.05


async def test_a_long_wait_opens_a_countdown() -> None:
    """Over the threshold the reader is told what is happening, and for how long."""
    transmit_gate.mark(flood_advert=True)  # a minute owed on the flood clock
    session = _Session(answer=True)
    assert await wait_for_cooldown(_Ctx(session), action="Flood advert", flood_advert=True)
    assert len(session.shown) == 1
    dialog = session.shown[0]
    assert dialog.title == "Flood advert"
    assert "whole mesh" in _lines(dialog)[1]


async def test_cancelling_the_countdown_abandons_the_action() -> None:
    """Backing out of the wait is backing out of what was waiting."""
    transmit_gate.mark(flood_advert=True)
    session = _Session(answer=CANCEL)
    assert await wait_for_cooldown(
        _Ctx(session), action="Flood advert", flood_advert=True
    ) is False


async def test_the_threshold_is_where_it_says_it_is() -> None:
    """SILENT_WAIT_S is the whole rule: at or under it, silence; over it, a dialog."""
    assert SILENT_WAIT_S == 2.0
    prefs = Preferences()
    prefs.set("trace_cooldown_s", SILENT_WAIT_S + 3)
    install(prefs)
    transmit_gate.mark()
    session = _Session()
    await wait_for_cooldown(_Ctx(session), action="Advert")
    assert len(session.shown) == 1


async def test_a_scripted_run_waits_without_a_dialog() -> None:
    """The CLI has no session to float a dialog over, so it takes the wait silently."""
    prefs = Preferences()
    prefs.set("trace_cooldown_s", 0.2)
    install(prefs)
    transmit_gate.mark()
    assert await wait_for_cooldown(_Ctx(None), action="Advert") is True


# -- what starts the clock -------------------------------------------------------


async def test_a_real_send_is_what_arms_the_gate() -> None:
    """The clock is set by transmitting, not by anything remembering to set it.

    Driven against the simulator, which marks the gate at exactly the points the radio
    does — so the countdown is walkable (and this is checkable) with no hardware.
    """
    from meshterm.core.connection import MockDevice

    device = MockDevice()
    await device.connect()

    assert transmit_gate.remaining() == 0.0
    await device.send_advert(False)
    assert transmit_gate.remaining() > 0
    assert transmit_gate.remaining(flood_advert=True) == pytest.approx(
        transmit_gate.remaining(), abs=0.1
    )  # a zero-hop advert does not arm the flood clock

    transmit_gate.current().reset()
    await device.send_advert(True)
    assert transmit_gate.remaining(flood_advert=True) > transmit_gate.remaining()

    transmit_gate.current().reset()
    await device.run_trace("Alice")
    assert transmit_gate.remaining() > 0
