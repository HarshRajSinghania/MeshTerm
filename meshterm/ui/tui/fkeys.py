"""The PicoCalc F-key lane: a fixed footer row mapping F1–F5 (and their opposites).

The PicoCalc keyboard has five dedicated function keys; its MCU translates Shift+F1..F5
into plain F6–F10 keycodes (measured in P0), so "Shift+key = the opposite action" is
literal hardware behaviour and the app simply binds all ten. One footer row teaches both
banks: each slot shows its digit and primary label, and — while the optional modifier
watch reports Shift held — flips to the opposite bank's labels live.

A screen describes its lane as five :class:`FPair` slots (``None`` = unassigned); the
session resolves a pressed F-key to the slot's action string and dispatches it through
the normal action funnel, so screens gain F-key support without any key handling of
their own. The defaults duplicate the nav cluster (which sits on an Fn layer on this
keyboard) plus commit/leave — the pairs that mean something on every screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from rich.text import Text


@dataclass(frozen=True, slots=True)
class FPair:
    """One F-key slot: the primary action (Fn) and its opposite (F(n+5)).

    Attributes:
        label: Hint text for the primary, ≤7 cells (the lane budgets 5 slots into 53).
        action: The screen action the primary dispatches (``"home"``, ``"enter"``, …).
        opp_label: Hint text for the opposite bank.
        opp_action: The action the opposite dispatches.
    """

    label: str
    action: str
    opp_label: str
    opp_action: str


#: A lane is five slots, F1..F5 left to right; ``None`` leaves a slot unassigned.
Lane = Sequence[Optional[FPair]]

#: The screen-agnostic default lane. Top/End and the page pair mirror the nav cluster
#: (Fn-layered on the physical keyboard, so the dedicated F-keys earn their duplicate);
#: F5 commits and its opposite leaves — the one inversion every screen understands.
DEFAULT_LANE: tuple[Optional[FPair], ...] = (
    FPair("Top", "home", "End", "end"),
    FPair("PgUp", "pageup", "PgDn", "pagedown"),
    None,
    None,
    FPair("OK", "enter", "Back", "escape"),
)


def action_for(lane: Lane, number: int) -> Optional[str]:
    """The action F-key ``number`` (1–10) resolves to on this lane, or ``None``.

    F1–F5 take the slot's primary action, F6–F10 its opposite (the physical Shift bank).
    """
    index = (number - 1) % 5
    if index >= len(lane) or lane[index] is None:
        return None
    pair = lane[index]
    return pair.opp_action if number > 5 else pair.action


def lane_text(lane: Lane, *, shifted: bool = False) -> Text:
    """Render the lane as the fixed footer row: ``1 Top  2 PgUp  ···  5 OK``.

    Args:
        lane: The five slots.
        shifted: Show the opposite bank's digits and labels (the modifier watch saw
            Shift go down); the static fallback always shows the primary bank.

    Returns:
        A one-line :class:`Text`, ≤53 cells with ≤7-cell labels.
    """
    row = Text()
    for index in range(5):
        pair = lane[index] if index < len(lane) else None
        if row.plain:
            row.append("  ")
        number = index + 6 if shifted else index + 1
        if pair is None:
            row.append(f"{number}", style="track")
            continue
        label = pair.opp_label if shifted else pair.label
        # The row's budget is exact at 53: five 9-cell slots and four 2-cell gaps. A
        # label is clipped to its slot rather than pushing the lane over — and F10's
        # two-digit key costs its own label a cell.
        budget = 6 if number >= 10 else 7
        row.append(f"{number}", style="accent")
        row.append(f" {label[:budget]}", style="muted")
    return row
