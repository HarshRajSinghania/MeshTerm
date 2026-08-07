"""The PicoCalc F-key lane: a fixed footer row of five coloured chips, F1-F5.

The PicoCalc keyboard has five dedicated function keys; its MCU translates Shift+F1..F5
into plain F6-F10 keycodes (measured in P0), so "Shift+key = the opposite action" is
literal hardware behaviour and the app simply binds all ten.

By JP's spec (2026-08-01): a lane slot is for functionality that would otherwise be
*hard to reach* — not a shortcut to a key that is already close at hand. Enter and Esc
sit right on the keyboard and are already the easiest keys to hit, so they never occupy
a slot. Paging has no physical key at all, so a screen that scrolls earns it the prime
F4/F5 pair. Ctrl+letter chords (Ctrl+P for message paths, Ctrl+arrows for a sort column,
…) are fiddly to hold on this keyboard, so a screen promotes its own onto the lone F3
slot (a second, if it has one, riding F3's Shift companion). The five *plain* keys carry
a screen's most-used, otherwise-awkward actions — reachable with no Shift at all — and
the Shift bank (F6-F10) is overflow for a screen with more than five such actions. A
lane can, and does, differ from screen to screen.

**A chip names an action, never a key** (JP, 2026-08-07). ``PgUp``, ``End`` and their
kin are the names of keys this keyboard doesn't even have; what the slot is for is the
thing it does *here* — ``Page ↑`` through a body, ``Latest`` on a transcript, ``Reset``
on the map, whose Home key refits the view and whose paging keys zoom. A screen that
repurposes a shared action relabels it; a screen the action means nothing on leaves the
slot **empty** rather than filling the lane out. Empty and dim are different claims:
empty says *not a thing here* (Retry in a channel, where nothing is ever acknowledged),
dim says *a thing here, just not right now* (Retry in a direct chat with everything
delivered).

A screen describes its lane as five :class:`FPair` slots (``None`` = unassigned); the
session resolves a pressed F-key to the slot's action string and dispatches it through
the normal action funnel, so screens gain F-key support without any key handling of
their own. An :class:`FPair` need not carry a Shift-bank action at all — a "lone" slot
(``opp_label=""``) simply renders blank in the shifted bank and F(n+5) does nothing.

**A slot never advertises a key that would do nothing.** The lane is the PicoCalc's whole
footer — it stands in for the hint line the other platform draws, and that line has always
dropped an atom whose key is inert (see :attr:`~meshterm.ui.tui.screen.Screen.content_overflows`).
So a screen rebuilds its lane each paint and clears :attr:`FPair.enabled` /
:attr:`FPair.opp_enabled` on whatever is out of reach right now: the chat's *Paths* with
no message picked, its *Retry* with nothing unacknowledged, the shared nav slots on a
screen with nothing to move (:func:`default_lane`). A dimmed slot keeps its label — muted,
unfilled, like an unassigned slot — so the key still reads as *what it would do*, it just
plainly isn't live. The dimming is presentational: the screen's own ``handle`` stays the
authority on what an action does (it no-ops), so a lane built from one-paint-stale metrics
can never swallow a key that would have worked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
        enabled: Whether the plain key's action is available *right now*. ``False`` draws
            the chip dimmed — label kept, fill dropped — rather than pretending (see the
            module docstring).
        opp_enabled: The same, for the Shift-bank companion.
    """

    label: str
    action: str
    opp_label: str = ""
    opp_action: str = ""
    enabled: bool = True
    opp_enabled: bool = True


#: A lane is five slots, F1..F5 left to right; ``None`` leaves a slot unassigned.
Lane = Sequence[Optional[FPair]]

#: The default lane, in the vocabulary of a screen that is one scrolling body: move
#: through it a page at a time, or jump to either end. No Enter or Esc slot — both keys
#: sit right on the keyboard already. F4/F5 (the most reachable pair) carry paging, which
#: has no physical key at all; F1/F2 carry the jumps, Fn-layered on the physical keyboard
#: and so worth a slot too, just a notch behind paging; F3 is the lone slot a screen's own
#: :attr:`~meshterm.ui.tui.screen.Screen.fkey_lane` override reaches for first — typically
#: a Ctrl+letter chord promoted up because chording is a pain here. A screen whose body is
#: not one scrolling column says so in its own words (see :class:`ChatScreen`, ``MapScreen``).
DEFAULT_LANE: tuple[Optional[FPair], ...] = (
    FPair("Top", "home"),
    FPair("Bottom", "end"),
    None,
    FPair("Page ↑", "pageup"),
    FPair("Page ↓", "pagedown"),
)


def default_lane(*, nav: bool = True) -> tuple[Optional[FPair], ...]:
    """:data:`DEFAULT_LANE`, with its four nav slots dimmed unless ``nav``.

    The shared slots all *move* something — the jump-to-ends pair and the paging pair —
    so on a screen with nothing to move (a body that fits its viewport, a transcript with
    no messages) all four are inert and say so. Screens that repurpose the nav actions
    for something always live (the map's zoom) build from :data:`DEFAULT_LANE` instead,
    relabelling as they go.

    Args:
        nav: Whether the nav keys would do anything on this paint — typically
            :attr:`~meshterm.ui.tui.screen.Screen.content_overflows`.

    Returns:
        The five slots, ready for a screen to overwrite the ones it claims.
    """
    if nav:
        return DEFAULT_LANE
    return tuple(
        None if pair is None else replace(pair, enabled=False, opp_enabled=False)
        for pair in DEFAULT_LANE
    )


def action_for(lane: Lane, number: int) -> Optional[str]:
    """The action F-key ``number`` (1-10) resolves to on this lane, or ``None``.

    F1-F5 take the slot's plain-key action, F6-F10 (the physical Shift bank) take its
    companion — or resolve to nothing when the slot has none (a lone slot, or an
    unassigned one). A *dimmed* slot still resolves: dimming is how the lane draws an
    action the screen would no-op anyway, and resolving it regardless keeps a lane built
    from stale metrics from ever swallowing a key that works (see the module docstring).
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
        live = pair.opp_enabled if shifted else pair.enabled
        # A dimmed slot drops the fill and keeps the label, landing in the same muted
        # "nothing to press" class as an unassigned one — while still saying what the
        # key is for once it becomes available.
        row.append(_chip(number, label), style=fill if live else "muted")
    return row
