"""The busy skeleton: a titled placeholder card floated between screens while a load runs.

A :class:`BusyOverlay` is the model behind :meth:`~meshterm.ui.tui.session.TuiSession.
busy_overlay` — a small *skeleton card* standing in for a screen the device is still
fetching: a title, the one-cell working :class:`~meshterm.ui.tui.spinner.Spinner` beside a
caption, and a Knight-Rider scanning-light bar standing in for the content still loading. Unlike a
:class:`~meshterm.ui.tui.screen.Screen` it is *not* part of the screen stack: the session
draws it as the top-most float, so it hovers over the gap between screens that a slow device
operation (Bluetooth most of all) opens — the caller shows it while the fetch runs and drops
it when that fetch returns.

Rather than pop in, it stays fully black for a short *hold* and then *fades in from black*
over its next fraction of a second (see :attr:`brightness`): an operation that finishes within
the hold shows nothing at all, and a longer one eases up out of the black rather than appearing
suddenly — so the card never flashes and never pops in.

Once up, it does not sit still. Beside the spinning chip a Knight-Rider scanning light sweeps
a two-row braille LED bar left→right and back, advanced on each :meth:`BusyOverlay.tick`: a red
head with a persistence-of-vision trail comet-tailing behind it over a dark-gray track. That
travelling light is what reads the card as *working* rather than as a frozen, broken bar.
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

#: The scanner's unlit-LED colour (the theme's ``track`` — a step below ``faint``, a dark
#: slate gray). Every lamp of the bar rests here; the moving light lifts lamps off it toward
#: red and they fade back as the trail decays, so an idle lamp reads as an unlit LED, not data.
_SCAN_TRACK = "#334155"

#: The lit-LED colours the moving light drives lamps toward: a vivid red (the theme's
#: ``hint.err``) for the body of the head, and a pale "highlighted" red at its very hottest
#: heart, so the core reads as white-hot the way a real LED bar's brightest lamp does.
_SCAN_RED = "#ef4444"
_SCAN_HOT = "#fca5a5"

#: The braille cell drawn for every lamp — dots 2,3,5,6, i.e. *two rows* of dots centred in the
#: cell, so the sweep reads as a slim two-row LED strip rather than a solid block.
_SCAN_GLYPH = "⠶"

#: How many lamps (terminal cells) wide the scanning bar is.
_SCAN_CELLS = 18

#: The per-tick persistence-of-vision fade: on each tick every lamp dims to this fraction of
#: its last brightness, so the head drags a comet trail that lags a few lamps behind it.
_SCAN_DECAY = 0.68

#: How many lamps the head advances per tick (its sweep speed) and the head's half-width in
#: lamps (the bright core lit around it before the trail takes over). Tuned so one pass across
#: the bar takes roughly a second at the session's ~0.06 s tick.
_SCAN_SPEED = 0.85
_SCAN_CORE = 1.2


class BusyOverlay:
    """A skeleton card (title, working chip + caption, placeholder rows) drawn as the top float.

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
        #: The scanning-light state driving the Knight-Rider bar: the head's fractional lamp
        #: position, its travel direction (``+1`` right / ``-1`` left, bouncing off each end),
        #: and the per-lamp brightness buffer whose slow decay on each :meth:`tick` is the
        #: persistence-of-vision trail. Seeded with the head already stamped so the first
        #: painted frame shows the light rather than a bare track.
        self._pos = 0.0
        self._dir = 1
        self._glow = [0.0] * _SCAN_CELLS
        self._stamp_core()

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
        """Advance the working chip one frame and step the scanning light along its sweep."""
        self.spinner.tick()
        self._advance()

    def _advance(self) -> None:
        """Fade the whole trail one notch, move the head, and re-light the core around it.

        The decay is the persistence-of-vision effect: every lamp dims toward the track, so the
        lamps the head has already passed glow on for a few frames as a comet tail. The head then
        steps :data:`_SCAN_SPEED` lamps along, reversing when it reaches either end (the
        Knight-Rider bounce), and a fresh bright core is stamped at its new position.
        """
        self._glow = [g * _SCAN_DECAY for g in self._glow]
        self._pos += self._dir * _SCAN_SPEED
        if self._pos >= _SCAN_CELLS - 1:
            self._pos = float(_SCAN_CELLS - 1)
            self._dir = -1
        elif self._pos <= 0:
            self._pos = 0.0
            self._dir = 1
        self._stamp_core()

    def _stamp_core(self) -> None:
        """Light the lamps around the head to full, tapering off over :data:`_SCAN_CORE` lamps.

        Combined with ``max`` so a freshly-lit core never dims a lamp the decaying trail has
        left brighter — the head is always at least as bright as its own tail.
        """
        for i in range(_SCAN_CELLS):
            core = 1.0 - abs(i - self._pos) / _SCAN_CORE
            if core > self._glow[i]:
                self._glow[i] = core

    def restart(self) -> None:
        """Restart the intro from scratch: rewind the fade ramp and the chip to their starts.

        Called each time the card is (re)exposed — its first appearance and every time it comes
        back after a prompt covered it — so the black hold and fade-in play fresh every display
        rather than the card snapping back at full brightness mid-way through an operation.
        """
        self.started_at = time.monotonic()
        self.spinner.reset()
        self._pos = 0.0
        self._dir = 1
        self._glow = [0.0] * _SCAN_CELLS
        self._stamp_core()

    def render(self) -> str:
        """Render the skeleton card as a centred ANSI block, at the current fade level.

        The title, the chip + caption line, a spacer, and the placeholder rows are each padded
        to a common width and joined into one multi-line :class:`~rich.text.Text`, so the
        session's content-sized float centres a clean rectangle over the gap between screens.

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
        lines.append(Text(""))  # a blank row between the caption and the scanning bar
        lines.append(self._scanner_row(level))
        width = max((cell_len(line.plain) for line in lines), default=0)
        block = Text("\n").join(_center_text(line, width) for line in lines)
        return render_to_ansi(block, width)

    def _scanner_row(self, level: float) -> Text:
        """The scanning-light bar: one braille lamp per cell, coloured by its glow, then dimmed.

        Every lamp is the same two-row braille glyph; only its colour moves. A lamp interpolates
        from the unlit ``track`` gray up through vivid red to a pale hot core by its brightness
        in :attr:`_glow` (the moving head plus its persistence-of-vision trail), and the whole
        row is finally dimmed by ``level`` so the light eases in with the card's fade-from-black.
        """
        row = Text()
        for glow in self._glow:
            row.append(_SCAN_GLYPH, style=dim_color(_scan_color(glow), level))
        return row


def _center_text(text: Text, width: int) -> Text:
    """Return ``text`` padded with spaces so it is centred within ``width`` cells."""
    pad = max(0, width - cell_len(text.plain))
    left = pad // 2
    out = Text(" " * left)
    out.append_text(text)
    out.append(" " * (pad - left))
    return out


def _scan_color(glow: float) -> str:
    """Map a lamp's ``[0, 1]`` brightness to its colour: track gray → vivid red → hot core.

    Two stops so the head reads as white-hot: the bottom three-quarters of the range walks the
    unlit ``track`` up to full red, and the last quarter pushes red on toward the pale
    ``_SCAN_HOT`` so only the brightest heart of the head lightens past red.
    """
    if glow <= 0.0:
        return _SCAN_TRACK
    if glow >= 1.0:
        return _SCAN_HOT
    if glow < 0.75:
        return _lerp_hex(_SCAN_TRACK, _SCAN_RED, glow / 0.75)
    return _lerp_hex(_SCAN_RED, _SCAN_HOT, (glow - 0.75) / 0.25)


def _lerp_hex(start: str, end: str, t: float) -> str:
    """Blend two ``#rrggbb`` colours: ``t=0`` returns ``start``, ``t=1`` returns ``end``."""
    t = max(0.0, min(1.0, t))
    channels = []
    for lo, hi in ((1, 3), (3, 5), (5, 7)):
        a = int(start[lo:hi], 16)
        b = int(end[lo:hi], 16)
        channels.append(round(a + (b - a) * t))
    return "#{:02x}{:02x}{:02x}".format(*channels)


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
