"""The busy overlay: a centred ring spinner floated on top of everything else.

A :class:`BusyOverlay` is the model behind :meth:`~meshterm.ui.tui.session.TuiSession.
busy_overlay` — a :class:`~meshterm.ui.tui.ring.RingSpinner` with an optional caption beneath
it. Unlike a :class:`~meshterm.ui.tui.screen.Screen`, it is *not* part of the screen stack: the session
draws it as the top-most float, so it hovers over whatever is (or is not) on screen. It owns
no input and resolves nothing; the session shows it while an operation runs and drops it when
that operation returns.

Rather than pop in, it stays fully black for a short *hold* and then *fades in from black*
over its next fraction of a second (see :attr:`brightness`): an operation that finishes within
the hold shows nothing at all, and a longer one eases up out of the black rather than appearing
suddenly — so the ring never flashes and never pops in.
"""

from __future__ import annotations

import time

from rich.cells import cell_len
from rich.text import Text

from .render import render_to_ansi
from .ring import RingSpinner, dim_color

#: The muted caption colour (the theme's ``muted``, as a hex so it can be dimmed for the fade).
_CAPTION_COLOR = "#94a3b8"


class BusyOverlay:
    """A ring spinner (plus optional caption) drawn as the top-most floating layer.

    Attributes:
        spinner: The animated :class:`~meshterm.ui.tui.ring.RingSpinner` at the overlay's core.
        message: An optional caption drawn, muted, beneath the ring.
        started_at: The monotonic time the overlay was created, from which the hold + fade run.
        hold: Seconds the ring stays fully black before the fade begins.
        fade: The fade-in duration in seconds, after the hold.
    """

    def __init__(
        self, message: str = "", *, hold: float = 0.2, fade: float = 0.4
    ) -> None:
        """Create a busy overlay.

        Args:
            message: A short caption drawn beneath the ring (empty for a bare ring).
            hold: Seconds to stay fully black before fading in, so an operation that finishes
                this fast shows nothing at all.
            fade: Seconds over which the ring then brightens from black to full colour.
        """
        self.spinner = RingSpinner()
        self.message = message
        self.started_at = time.monotonic()
        self.hold = max(0.0, hold)
        self.fade = max(0.0, fade)

    @property
    def brightness(self) -> float:
        """The current fade level in ``[0, 1]``: 0 through the :attr:`hold`, then easing to 1.

        Stays exactly 0 for the first :attr:`hold` seconds (nothing paints), then a smoothstep
        easing over :attr:`fade` gives a gentle glow up out of the black rather than a linear
        ramp.
        """
        elapsed = time.monotonic() - self.started_at - self.hold
        if elapsed <= 0:
            return 0.0
        if self.fade <= 0:
            return 1.0
        t = min(1.0, elapsed / self.fade)
        return t * t * (3 - 2 * t)  # smoothstep

    def tick(self) -> None:
        """Advance the ring's comet one animation step."""
        self.spinner.tick()

    def restart(self) -> None:
        """Restart the intro from scratch: rewind the fade ramp and the comet to their starts.

        Called each time the ring is (re)exposed — its first appearance and every time it comes
        back after a prompt covered it — so the black hold and fade-in play fresh every display
        rather than the ring snapping back at full brightness mid-way through an operation.
        """
        self.started_at = time.monotonic()
        self.spinner.reset()

    def render(self) -> str:
        """Render the ring, plus any caption, as a centred ANSI block, at the current fade level.

        The rows are pre-padded to a common width and joined into one multi-line
        :class:`~rich.text.Text`, so the session's content-sized float centres a clean
        rectangle over the screen.

        Returns:
            An ANSI string of the ring rows and an optional centred caption line.
        """
        level = self.brightness
        ring = self.spinner.render(level)
        rows: list[Text] = ring.split("\n")
        width = max((cell_len(row.plain) for row in rows), default=0)
        if self.message:
            width = max(width, cell_len(self.message))
        block = Text("\n").join(_center_text(row, width) for row in rows)
        if self.message:
            block.append("\n")
            caption = Text(self.message, style=dim_color(_CAPTION_COLOR, level))
            block.append_text(_center_text(caption, width))
        return render_to_ansi(block, width)


def _center_text(text: Text, width: int) -> Text:
    """Return ``text`` padded with spaces so it is centred within ``width`` cells."""
    pad = max(0, width - cell_len(text.plain))
    left = pad // 2
    out = Text(" " * left)
    out.append_text(text)
    out.append(" " * (pad - left))
    return out
