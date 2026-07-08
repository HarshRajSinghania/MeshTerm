"""A reusable text spinner: one animated glyph for a "working" indicator.

A small, screen-agnostic animation helper. It holds a cycle of frame glyphs and a current
position; a caller advances it with :meth:`tick` (typically from an animation timer) and
reads the current frame as a bare glyph or as a styled Rich :class:`~rich.text.Text`. The
busy splash uses it, but it is deliberately independent of any screen so any "working"
indicator across the toolkit can reuse the same animation.
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text


class Spinner:
    """An animated spinner that cycles through a set of frame glyphs.

    Attributes:
        style: The Rich style :meth:`text` applies to the current glyph.
    """

    #: The default Braille-dot frames — a smooth, single-cell rotation.
    BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    #: A plain ASCII cycle for terminals/fonts without Braille glyphs.
    LINE = "|/-\\"

    def __init__(self, frames: str = BRAILLE, *, style: str = "accent") -> None:
        """Create a spinner resting on its first frame.

        Args:
            frames: The glyphs to cycle through, one per animation frame.
            style: The Rich style :meth:`text` applies to the current glyph.
        """
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

    def text(self, style: Optional[str] = None) -> Text:
        """Return the current glyph as a styled Rich :class:`~rich.text.Text`.

        Args:
            style: A style to use instead of the spinner's own :attr:`style`.

        Returns:
            The current glyph, styled.
        """
        return Text(self.frame, style=self.style if style is None else style)
