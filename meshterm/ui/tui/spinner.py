# SPDX-License-Identifier: Apache-2.0
"""A reusable text spinner: one animated glyph for a "working" indicator.

A small, screen-agnostic animation helper. It holds a cycle of frame glyphs and a current
position; a caller advances it with :meth:`tick` (typically from an animation timer) and
reads the current frame as a bare glyph or as a styled Rich :class:`~rich.text.Text`. The
busy splash uses it, but it is deliberately independent of any screen so any "working"
indicator across the toolkit can reuse the same animation.
"""

from __future__ import annotations

from rich.text import Text

from ...platforms import get_platform


def spinner_interval() -> float:
    """Seconds a "working" animation should wait between frames, on this platform.

    The single source of the app's spin cadence — every animated wait sleeps on this rather
    than on its own constant, so the rate is one platform decision instead of five copies
    drifting apart. Called at each tick rather than read into a module constant: a constant
    would freeze whatever platform was active at *import* time, which is always the default
    (``set_platform`` runs later, in the CLI callback — see :mod:`meshterm.platforms`).

    Returns:
        The active platform's :attr:`~meshterm.platforms.Platform.spinner_tick_s`.
    """
    return get_platform().spinner_tick_s


class Spinner:
    """An animated spinner that cycles through a set of frame glyphs.

    Attributes:
        style: The Rich style :meth:`text` applies to the current glyph.
    """

    #: The default Braille-dot frames — a smooth, single-cell rotation.
    BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    #: A plain ASCII cycle for terminals/fonts without Braille glyphs.
    LINE = "|/-\\"

    def __init__(self, frames: str | None = None, *, style: str = "accent") -> None:
        """Create a spinner resting on its first frame.

        Args:
            frames: The glyphs to cycle through, one per animation frame. Defaults to the
                active platform's cycle — :data:`BRAILLE` where effects are on, the
                four-frame :data:`LINE` where they aren't. Resolved here, at construction,
                so a spinner built after ``set_platform`` picks the right cycle and never
                re-decides while it spins.
            style: The Rich style :meth:`text` applies to the current glyph.
        """
        if frames is None:
            frames = self.BRAILLE if get_platform().effects else self.LINE
        if not frames:
            raise ValueError("a spinner needs at least one frame")
        self._frames = frames
        self.style = style
        self._index = 0

    @property
    def frame(self) -> str:
        """The current frame's glyph."""
        return self._frames[self._index]

    @property
    def frames(self) -> str:
        """The full cycle of frame glyphs, in order."""
        return self._frames

    def tick(self) -> None:
        """Advance to the next frame, wrapping around at the end of the cycle."""
        self._index = (self._index + 1) % len(self._frames)

    def reset(self) -> None:
        """Return the spinner to its first frame."""
        self._index = 0

    def text(self, style: str | None = None) -> Text:
        """Return the current glyph as a styled Rich :class:`~rich.text.Text`.

        Args:
            style: A style to use instead of the spinner's own :attr:`style`.

        Returns:
            The current glyph, styled.
        """
        return Text(self.frame, style=self.style if style is None else style)
