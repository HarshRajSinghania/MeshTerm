"""The MeshTerm wordmark for the startup splash, loaded from the assets folder.

The art itself lives in ``meshterm/assets`` — hand-drawn ``.txt`` sources beside the
pre-coloured ``.ansi`` the splash actually draws (``scripts/colour-logo.py`` paints one
into the other). Files are read fresh on every call, so the wordmark can be re-styled by
editing the art alone: no code change and no restart of the design loop.

There is more than one mark, at different widths, and the *screen* picks — not the
platform. A 53-column PicoCalc console and a desktop terminal dragged narrow have the same
problem, so :func:`load_logo` answers the only question that matters: which is the widest
mark that fits the columns I have?
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.cells import cell_len
from rich.text import Text

#: Where the art lives — a sibling of the code, not mixed into it, and inside the package
#: so it ships with an installed wheel.
_ASSETS = Path(__file__).resolve().parent.parent / "assets"

#: Every wordmark, widest first. :func:`load_logo` walks this and takes the first that
#: fits, so adding a size is dropping a file in and naming it here.
_VARIANTS: tuple[str, ...] = ("logo.ansi", "logo_53.ansi")


def _rows(name: str) -> list[str]:
    """The named mark's rows, or ``[]`` when it can't be read."""
    try:
        text = (_ASSETS / name).read_text(encoding="utf-8")
    except OSError:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":  # drop the trailing newline's empty row
        lines.pop()
    return lines


def logo_width(rows: list[str]) -> int:
    """The display width of a mark's widest row, escape sequences discounted."""
    return max((cell_len(Text.from_ansi(row).plain) for row in rows), default=0)


def load_logo(max_cols: Optional[int] = None) -> list[str]:
    """Return the widest wordmark that fits ``max_cols``, as pre-coloured ANSI rows.

    Args:
        max_cols: The columns available to draw in. ``None`` asks for the full-size mark
            without fitting — for a caller that will fit it later (see
            :func:`~meshterm.ui.tui.frame.compose_startup`, which knows the real width
            only at compose time).

    Returns:
        The mark's rows, or ``[]`` when none fits (or none could be read) — the splash
        draws no banner at all rather than a torn one, and a missing file degrades the
        same way rather than raising.
    """
    for name in _VARIANTS:
        rows = _rows(name)
        if rows and (max_cols is None or logo_width(rows) <= max_cols):
            return rows
    return []
