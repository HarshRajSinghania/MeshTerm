"""The PicoCalc F-key lane: a fixed footer row of five coloured chips, F1-F5.

The PicoCalc keyboard has five dedicated function keys; its MCU translates Shift+F1..F5
into plain F6-F10 keycodes (measured in P0), so "Shift+key = the opposite action" is
literal hardware behaviour and the app simply binds all ten.

By JP's spec (2026-08-01): a lane slot is for functionality that would otherwise be
*hard to reach* — not a shortcut to a key that is already close at hand. Enter and Esc
sit right on the keyboard and are already the easiest keys to hit, so they never occupy
a slot. Paging (PgUp/PgDn) has no physical key at all, so a screen that scrolls earns it
the prime F4/F5 pair. Ctrl+letter chords (Ctrl+P for message paths, Ctrl+arrows for a
sort column, …) are fiddly to hold on this keyboard, so a screen promotes its own onto
the lone F3 slot (a second, if it has one, riding F3's Shift companion). The five
*plain* keys carry a screen's most-used, otherwise-awkward actions — reachable with no
Shift at all — and the Shift bank (F6-F10) is overflow for a screen with more than five
such actions. A lane can, and does, differ from screen to screen.

A screen describes its lane as five :class:`FPair` slots (``None`` = unassigned); the
session resolves a pressed F-key to the slot's action string and dispatches it through
the normal action funnel, so screens gain F-key support without any key handling of
their own. An :class:`FPair` need not carry a Shift-bank action at all — a "lone" slot
(``opp_label=""``) simply renders blank in the shifted bank and F(n+5) does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from rich.text import Text

#: Every chip is exactly this many cells: a 2-cell key label, a space, a 6-cell
#: description. Five chips plus four 2-cell gaps sum to exactly 53 — the PicoCalc's
#: readable width — with nothing left over.
_CHIP_WIDTH = 9
_DESC_WIDTH = 6
_GAP = "  "


@dataclass(frozen=True, slots=True)
class FPair:
    """One F-key slot: the plain-key action and its optional Shift-bank companion.

    Attributes:
        label: Verb-phrase hint for the plain key, e.g. ``"Zoom +"`` — what the key
            *does* on this screen, never the key's own name (``"PgUp"``). Clipped/padded
            to 6 cells.
        action: The screen action the plain key dispatches (``"home"``, ``"enter"``, …).
        opp_label: Hint for the Shift-bank companion, or ``""`` for a lone slot with no
            companion — the shifted bank then renders that chip blank and the
            corresponding F(n+5) does nothing.
        opp_action: The action the Shift-bank companion dispatches, or ``""``.
    """

    label: str
    action: str
    opp_label: str = ""
    opp_action: str = ""


#: A lane is five slots, F1..F5 left to right; ``None`` leaves a slot unassigned.
Lane = Sequence[Optional[FPair]]

#: The screen-agnostic default lane. No Enter or Esc slot — both keys sit right on the
#: keyboard already. F4/F5 (the most reachable pair) carry paging, which has no physical
#: key at all; F1/F2 carry the nav cluster's jump-to-top/end, Fn-layered on the physical
#: keyboard and so worth a slot too, just a notch behind paging; F3 is the lone slot a
#: screen's own :attr:`~meshterm.ui.tui.screen.Screen.fkey_lane` override reaches for
#: first — typically a Ctrl+letter chord promoted up because chording is a pain here.
DEFAULT_LANE: tuple[Optional[FPair], ...] = (
    FPair("Top", "home"),
    FPair("End", "end"),
    None,
    FPair("PgUp", "pageup"),
    FPair("PgDn", "pagedown"),
)


def action_for(lane: Lane, number: int) -> Optional[str]:
    """The action F-key ``number`` (1-10) resolves to on this lane, or ``None``.

    F1-F5 take the slot's plain-key action, F6-F10 (the physical Shift bank) take its
    companion — or resolve to nothing when the slot has none (a lone slot, or an
    unassigned one).
    """
    index = (number - 1) % 5
    if index >= len(lane) or lane[index] is None:
        return None
    pair = lane[index]
    if number > 5:
        return pair.opp_action or None
    return pair.action


def _chip(number: int, label: str) -> str:
    """The exact 9-cell chip body: ``F1 Zoom +`` (``10`` stands in for ``F10``)."""
    key = f"F{number}" if number < 10 else "10"
    return f"{key} {label[:_DESC_WIDTH]:<{_DESC_WIDTH}}"


def lane_text(lane: Lane, *, shifted: bool = False) -> Text:
    """Render the lane as five 9-cell chips: coloured fill, white text, 2-cell gaps.

    Args:
        lane: The five slots.
        shifted: Show the Shift-bank chips (F6-F10) in the green fill; the plain bank
            (F1-F5) renders in the gray fill.

    Returns:
        A one-line :class:`Text`, exactly 53 cells: five 9-cell chips and four 2-cell
        gaps.
    """
    fill = "fkey.chip.shift" if shifted else "fkey.chip"
    row = Text()
    for index in range(5):
        if index:
            row.append(_GAP)
        pair = lane[index] if index < len(lane) else None
        number = index + 6 if shifted else index + 1
        label = (pair.opp_label if shifted else pair.label) if pair else ""
        if not label:
            # Unassigned (no slot, or a lone slot's empty Shift bank): the bare key
            # number, unfilled — nothing to press here.
            key = f"F{number}" if number < 10 else "10"
            row.append(f"{key}{' ' * (_CHIP_WIDTH - len(key))}", style="muted")
            continue
        row.append(_chip(number, label), style=fill)
    return row
