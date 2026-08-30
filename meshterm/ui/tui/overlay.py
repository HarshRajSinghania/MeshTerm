"""The busy skeleton: a titled card floated between screens while a load runs.

A :class:`BusyOverlay` is the model behind :meth:`~meshterm.ui.tui.session.TuiSession.
busy_overlay` — a small *skeleton card* standing in for a screen the device is still
fetching: a title and the one-cell working :class:`~meshterm.ui.tui.spinner.Spinner`
beside a caption. Unlike a :class:`~meshterm.ui.tui.screen.Screen` it is *not* part of the
screen stack: the session draws it as the top-most float, so it hovers over the gap between
screens that a slow device operation (Bluetooth most of all) opens — the caller shows it
while the fetch runs and drops it when that fetch returns.

Rather than pop in, it stays fully black for a short *hold* and then *fades in from black*
over its next fraction of a second (see :attr:`brightness`): an operation that finishes within
the hold shows nothing at all, and a longer one eases up out of the black rather than appearing
suddenly — so the card never flashes and never pops in.

The spinning chip is the whole animation, and says everything the card has to say: the app is
working. It once shared the card with a scanning-light LED bar (JP, 2026-08-29) — eighteen
cells of travelling red that drew the eye harder than the words did, for a screen the reader
is only ever passing through.
"""

from __future__ import annotations

import time

from rich.cells import cell_len
from rich.text import Text

from .render import render_to_ansi
from .spinner import Spinner

#: The title line's colour (the theme's ``title.accent``, as hex so the fade can dim it).
_TITLE_COLOR = "bold #a5b4fc"

#: The working chip's colour (the theme's ``accent`` — the same beloved one-cell spinner).
_CHIP_COLOR = "bold #818cf8"

#: The caption colour (the theme's ``muted``, as a hex so it can be dimmed for the fade).
_CAPTION_COLOR = "#94a3b8"


class BusyOverlay:
    """A skeleton card (title, working chip + caption) drawn as the session's top-most float.

    Attributes:
        spinner: The animated one-cell :class:`~meshterm.ui.tui.spinner.Spinner` — the same
            working chip used inline elsewhere — sitting beside the caption.
        title: An optional heading for the screen being fetched (empty for a bare card).
        message: An optional caption drawn beside the chip (e.g. "reading from …").
        started_at: The monotonic time the card was created, from which the hold + fade run.
        hold: Seconds the card stays fully black before the fade begins.
        fade: The fade-in duration in seconds, after the hold.
    """

    def __init__(
        self,
        message: str = "",
        *,
        title: str = "",
        hold: float = 0.2,
        fade: float = 0.4,
    ) -> None:
        """Create a busy skeleton card.

        Args:
            message: A short caption drawn beside the working chip (empty for a bare card).
            title: An optional heading naming the screen being fetched.
            hold: Seconds to stay fully black before fading in, so an operation that finishes
                this fast shows nothing at all.
            fade: Seconds over which the card then brightens from black to full colour.
        """
        self.spinner = Spinner()
        self.title = title
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
        """Advance the working chip one frame — the card's only animation."""
        self.spinner.tick()

    def restart(self) -> None:
        """Restart the intro from scratch: rewind the fade ramp and the chip to their starts.

        Called each time the card is (re)exposed — its first appearance and every time it comes
        back after a prompt covered it — so the black hold and fade-in play fresh every display
        rather than the card snapping back at full brightness mid-way through an operation.
        """
        self.started_at = time.monotonic()
        self.spinner.reset()

    def render(self) -> str:
        """Render the skeleton card as a centred ANSI block, at the current fade level.

        The title and the chip + caption line are each padded to a common width and joined
        into one multi-line :class:`~rich.text.Text`, so the session's content-sized float
        centres a clean rectangle over the gap between screens.

        Returns:
            An ANSI string of the card's rows.
        """
        level = self.brightness
        lines: list[Text] = []
        if self.title:
            lines.append(Text(self.title, style=dim_color(_TITLE_COLOR, level)))
        status = self.spinner.text(dim_color(_CHIP_COLOR, level))
        if self.message:
            status.append("  ")
            status.append(self.message, style=dim_color(_CAPTION_COLOR, level))
        lines.append(status)
        width = max((cell_len(line.plain) for line in lines), default=0)
        block = Text("\n").join(_center_text(line, width) for line in lines)
        return render_to_ansi(block, width)


def _center_text(text: Text, width: int) -> Text:
    """Return ``text`` padded with spaces so it is centred within ``width`` cells."""
    pad = max(0, width - cell_len(text.plain))
    left = pad // 2
    out = Text(" " * left)
    out.append_text(text)
    out.append(" " * (pad - left))
    return out


def dim_color(style: str, factor: float) -> str:
    """Scale a Rich hex-colour style toward black by ``factor`` (0 = black, 1 = unchanged).

    Only the ``#rrggbb`` token is scaled; any attribute words (e.g. ``bold``) are preserved.
    A style without a hex colour is returned untouched, as is any ``factor >= 1``.

    Args:
        style: A Rich style such as ``"#38bdf8"`` or ``"bold #ecfeff"``.
        factor: The brightness multiplier, clamped to ``[0, 1]``.

    Returns:
        The style with its colour dimmed toward black.
    """
    if factor >= 1.0:
        return style
    factor = max(0.0, factor)
    parts = []
    for token in style.split():
        if token.startswith("#") and len(token) == 7:
            r = round(int(token[1:3], 16) * factor)
            g = round(int(token[3:5], 16) * factor)
            b = round(int(token[5:7], 16) * factor)
            parts.append(f"#{r:02x}{g:02x}{b:02x}")
        else:
            parts.append(token)
    return " ".join(parts)
