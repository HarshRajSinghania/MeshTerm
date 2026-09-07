"""Waiting out a transmit cooldown, visibly and cancellably.

MeshTerm holds itself back between transmissions (see
:mod:`meshterm.core.transmit_gate`). A short hold is nothing anyone needs told about; a
long one, spent frozen, reads as a hung app on the very screen where "did the radio die?"
is the reader's first thought. So the wait has a threshold: under
:data:`SILENT_WAIT_S` it simply happens, and over it becomes a
:class:`~meshterm.ui.tui.prompt.CountdownDialog` — the seconds ticking down over a Cancel
chip, floating on the screen that asked, which the reader can back out of to abandon the
action entirely.

One function, :func:`wait_for_cooldown`, and its answer is the only thing a caller tests:
``True`` means the air is clear and the action may go ahead, ``False`` means the reader
changed their mind.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..core import transmit_gate

if TYPE_CHECKING:
    from ..context import AppContext

#: A wait at or under this many seconds passes without a word. Long enough to cover the
#: ordinary spacing between two transmissions, short enough that a reader never sits in
#: front of an unexplained stillness — a dialog that flashes up for half a second would
#: itself be the interruption.
SILENT_WAIT_S = 2.0

#: How often the countdown redraws. Fast enough that the seconds look like they are
#: counting rather than jumping, slow enough to cost nothing.
_TICK_S = 0.2


async def wait_for_cooldown(ctx: AppContext, *, action: str, flood_advert: bool = False) -> bool:
    """Hold until the transmit cooldown has passed; ``False`` if the reader gave up.

    Args:
        ctx: Shared application context (for the UI surface).
        action: What is waiting, named as the thing about to happen — it becomes the
            dialog's title, so it reads "Flood advert", not "Waiting to send a flood
            advert".
        flood_advert: Whether the pending transmission is a flood advert, which waits out
            its own longer clock as well as the general one.

    Returns:
        ``True`` when the wait is over and the caller may transmit; ``False`` if the
        countdown was cancelled.
    """
    remaining = transmit_gate.remaining(flood_advert=flood_advert)
    if remaining <= 0:
        return True
    if remaining <= SILENT_WAIT_S:
        await asyncio.sleep(remaining)
        return True

    session = getattr(ctx.ui, "session", None)
    if session is None:
        # No full-screen session to float a dialog over (the scripted CLI): the wait is
        # still owed, so it is still taken — just silently, as every other CLI pause is.
        await asyncio.sleep(remaining)
        return True

    from .tui import CANCEL
    from .tui.prompt import CountdownDialog

    reason = (
        "a flood advert reaches the whole mesh"
        if flood_advert
        else "spacing out our own transmissions"
    )
    dialog = CountdownDialog(action, remaining, reason=reason)
    ticker = asyncio.ensure_future(_tick(session, dialog, flood_advert))
    try:
        return await session.run_screen(dialog) is not CANCEL
    finally:
        ticker.cancel()


async def _tick(session, dialog, flood_advert: bool) -> None:  # noqa: ANN001
    """Feed the dialog the live remaining time until it resolves itself at zero."""
    try:
        while True:
            await asyncio.sleep(_TICK_S)
            dialog.set_remaining(transmit_gate.remaining(flood_advert=flood_advert))
            session.invalidate()
    except asyncio.CancelledError:
        # Swallowed so the task finishes on its own final cycle rather than being torn
        # down while pending — the same courtesy the progress dialog's animator takes.
        pass


__all__ = ["SILENT_WAIT_S", "wait_for_cooldown"]
