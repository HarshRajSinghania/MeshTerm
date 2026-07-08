"""The MeshTerm wordmark for the startup splash, loaded from ``logo.ansi``.

The art lives in a sibling ``logo.ansi`` file (pre-coloured ANSI, one row per line) and is
read fresh on every call, so the wordmark can be re-styled by editing that file alone — no
code change or restart of the design loop required.
"""

from __future__ import annotations

from pathlib import Path

#: The ANSI art file, kept beside this module so it ships inside the package.
_LOGO_PATH = Path(__file__).with_name("logo.ansi")


def load_logo() -> list[str]:
    """Return the wordmark's rows (pre-coloured ANSI strings), or ``[]`` if unavailable.

    Read from :data:`_LOGO_PATH` on each call so an edit to the art is picked up without a
    restart. A missing or unreadable file degrades to no banner rather than raising, so the
    splash still works.
    """
    try:
        text = _LOGO_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = text.split("\n")
    if lines and lines[-1] == "":  # drop the trailing newline's empty row
        lines.pop()
    return lines
