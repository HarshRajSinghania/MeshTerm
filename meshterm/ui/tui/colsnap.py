# SPDX-License-Identifier: Apache-2.0
"""Pin a written row's columns to the terminal's own grid, so no glyph can shift it.

Every other correction in this package answers *how many cells does this glyph take* —
:mod:`~meshterm.ui.tui.emoji_width` is that question's whole home, two width authorities and
two hand-curated exception sets deep. This module answers the one that actually decides
whether a row lines up: **which column does the next glyph land in?**

A row reaches the terminal as one run of characters, and the terminal advances its own cursor
by its own idea of each glyph's width. Where that idea differs from ours by a cell,
*everything after that glyph* is drawn a column out — the lanes to its right shift, the values
under a heading stop being under it, and the panel's right border notches in one short of the
frame. One emoji in one node's name, and the row it sits on is the only row in the list that
does not line up.

**That disagreement cannot be measured away**, which is the part worth being plain about.
Whether a font draws a given emoji in one cell or two is the font's business rather than the
codepoint's; there is no table that is right on every terminal; and no escape sequence reports
a renderer's width back to the program, because on a split terminal the component that tracks
the cursor and the component that paints the glyph are different programs that disagree with
each other (a cursor probe reads the PTY, not the painter — :mod:`~meshterm.ui.tui.emoji_width`
carries that reasoning in full, along with the two curated sets it leaves us with). Curating
exceptions one confirmed glyph at a time lines up the rows somebody has already looked at, and
leaves every name that arrives off the air — written by a stranger, carrying whatever they felt
like typing — exactly as broken as it was. A contact list is the worst case in the app for
precisely that reason: its content is the one thing on screen that nobody can put on a list in
advance.

So this module stops asking the terminal to agree. After any glyph whose drawn width is not
certain, the row carries an **absolute column address** — ``CSI n G``, naming the one-based
column the app measured that glyph into — so the terminal's answer to *where am I* is replaced
with ours before the next glyph is drawn. The cursor is re-pinned to the app's own grid at
every point where a disagreement could have started, and a mismeasured glyph can no longer
move anything but itself:

* Drawn **narrower** than we reserved, it leaves a blank cell after it. The cells a glyph is
  reserved are erased in its own colours before it is drawn (``CSI n X``), so that cell is
  blank in the right background: never a character left over from the last frame, and never a
  hole in the fill of a chip.
* Drawn **wider**, its overhang is written over by whatever the next column holds.

Both are one cell of cosmetic damage inside the glyph's own lane, where a shift was a whole row
of it. The curated width sets keep their job — they are what makes the *reservation* right, so
a listed glyph gets neither the gap nor the overwrite — but they stop being what holds the row
together. A glyph nobody has ever classified now costs a cell instead of a row.

**What is certain.** Pinning after a glyph that did not need it is harmless (it addresses the
column the cursor is already in) but it is not free: five bytes on the wire and a cluster walk
in Python, and MeshTerm draws some non-ASCII glyphs by the thousand — a map is a canvas of
braille, every frame is box drawing, a path ribbon is half blocks and chevrons. So
:data:`_TEXT_GLYPHS` lists the ranges this app draws in bulk whose one-cell *text* width both
authorities already agree on, and :func:`snap_row` pins after everything else. A row holding
nothing outside that list is handed back untouched, which is nearly every row of nearly every
screen; the walk is paid only where an emoji actually is.

Getting an entry in that list wrong costs exactly what today costs anyway — one glyph's row —
so the list is a performance claim and never the thing correctness rests on. That is the whole
point of the inversion: the old exception sets had to be *complete* to keep the app aligned,
and this one only has to be *cheap*.

**Two writers, two spellings of the same pin.** A frame reaches the terminal one of two ways,
and each knows something different about where its cursor is:

* :mod:`~meshterm.ui.tui.fastrender` writes a plain full-screen frame itself, each changed row
  as one run from column 0. It knows every column absolutely, so :func:`snap_row` spells the
  pin as an absolute address, ``CSI n G``.
* Anything prompt_toolkit lays out — a floating dialog, and the backdrop it repaints — goes out
  through its differential renderer, which never knows a column absolutely: it steps a cursor
  of its own by relative moves and adds each character's measured width as it writes. So
  :class:`PinnedOutput` spells the pin *relative to the glyph itself* — save the cursor, draw
  the glyph, restore, step forward exactly the width the renderer is about to add. The
  terminal's cursor then lands where the renderer's arithmetic says it is, after every glyph,
  and a relative move computed from that arithmetic is a correct move. Nothing is tracked, so
  nothing can drift out of step with the renderer's own bookkeeping.

A platform that never draws a glyph outside its own font has nothing to pin, and pins nothing:
the PicoCalc folds every emoji away before it reaches the console, and every glyph it does draw
is one :mod:`~meshterm.ui.fontset` has verified on the device.

The escape hatch is ``MESHTERM_COLUMN_SNAP=0``: this is terminal geometry, read once at session
build, not a choice anybody makes about how the app behaves.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from typing import Any

from prompt_toolkit.utils import get_cwidth
from rich.cells import cell_len

from ...platforms import get_platform
from .emoji_width import clusters

#: The glyph ranges MeshTerm draws in bulk whose single-cell width both width authorities
#: already agree on, so a row made only of them needs no pinning at all. Each entry is an
#: inclusive ``(first, last)`` pair; a single glyph names itself twice.
#:
#: These are *text* glyphs — outside Emoji_Presentation, carrying no variation selector — which
#: is the class the emoji-width module's hardest-won lesson is about: ``U+1F6E3`` and
#: ``U+1F578`` measure one cell by both authorities because the font draws them as one-cell
#: text, and forcing either to two pulled a row's border in. Everything else non-ASCII — every
#: pictograph, every flag, every joined sequence, every fullwidth character, and anything nobody
#: has ever looked at — falls outside this list and gets pinned.
_TEXT_GLYPHS: tuple[tuple[str, str], ...] = (
    ("\x00", "\x7f"),  # ASCII: every letter, digit and escape byte in a composed row
    (" ", "ſ"),  # Latin-1 + Latin Extended-A: an accented name, °, ·, ±
    ("‐", "‧"),  # General Punctuation: – — ‘ ’ “ ” • …
    ("←", "↓"),  # ← ↑ → ↓, the arrow atoms every footer hint leads with
    ("─", "╿"),  # Box Drawing: every panel border and every rule
    ("▀", "▟"),  # Block Elements: the path line's half blocks, the splash art
    ("■", "■"),  # ■ room server
    ("▲", "▲"),  # ▲ repeater
    ("▸", "▸"),  # ▸ a row's own marker
    ("◉", "◉"),  # ◉ sensor
    ("○", "●"),  # ○ unknown through ● node, and the rings between them
    ("★", "★"),  # ★ you
    ("✓", "✓"),  # ✓ ok
    ("✗", "✗"),  # ✗ err
    ("❯", "❯"),  # ❯ the cursor
    ("⠀", "⣿"),  # Braille: every chart, and the whole map canvas
    ("", ""),  # the powerline chevrons a path ribbon's seams are drawn from
)

#: Matches the first glyph in a row that is *not* one of :data:`_TEXT_GLYPHS` — the probe that
#: decides whether a row needs walking at all. A negated class of the ranges above, so the test
#: is one C-speed scan of the row instead of a Python loop over its cells.
_UNPINNED = re.compile("[^" + "".join(f"{low}-{high}" for low, high in _TEXT_GLYPHS) + "]")

#: One escape sequence: a CSI (which is everything a composed row actually carries — the
#: theme's SGR colour runs), an OSC string, or a bare two-character escape. Matched so it can
#: be copied through *without* advancing the column: a style change occupies no cell, and
#: counting one would pin every glyph after it to the wrong column.
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")

#: Pinned rows, keyed by the row as composed. Scrolling a list rewrites the same few rows over
#: and over — the highlighted one, and the one the highlight just left — so the walk is paid
#: once per distinct row rather than once per paint.
_CACHE: OrderedDict[str, str] = OrderedDict()

#: Rows the cache holds before evicting the least recently used.
_CACHE_MAX = 1024

#: DECSC and DECRC: save the cursor, and return to it. What :class:`PinnedOutput` brackets a
#: glyph with, so the step after it is measured from where the glyph *started* rather than from
#: wherever the terminal's own width table left the cursor. Both also save and restore the
#: current colour, which is harmless here: nothing changes it between the two.
_SAVE = "\x1b7"
_RESTORE = "\x1b8"


def _erase(width: int) -> str:
    """ECH: blank ``width`` cells from the cursor in the current colours, without moving it.

    Written in front of a glyph reserved more than one cell, so every cell of its reservation is
    painted by this frame whatever the font does with the glyph: the background a chip or a
    highlighted row filled it with, rather than whatever the terminal last held there.
    """
    return f"\x1b[{width}X"


def enabled() -> bool:
    """Whether written glyphs are pinned to the columns the app measured them into.

    On wherever the platform draws emoji, with ``MESHTERM_COLUMN_SNAP=0`` to turn it off for one
    run — the escape hatch a terminal that mishandles the pins would need, and the switch that
    shows what the alignment looks like without them. Off on a platform that draws no emoji at
    all, where every glyph is one the platform's own font has already verified. Read at session
    build, like its neighbours.
    """
    return os.environ.get("MESHTERM_COLUMN_SNAP") != "0" and get_platform().emoji


def _pinned(cluster: str) -> bool:
    """Whether ``cluster`` needs an absolute column address written after it.

    True for anything the app cannot be sure the terminal draws at the width it measured: every
    multi-codepoint glyph (a flag, a joined sequence, an emoji carrying a variation selector or
    a skin tone — the forms :func:`~meshterm.ui.tui.emoji_width.clusters` gathers), and every
    lone codepoint outside :data:`_TEXT_GLYPHS`.
    """
    return len(cluster) > 1 or bool(_UNPINNED.match(cluster))


def snap_row(row: str) -> str:
    """``row`` with an absolute column address after every glyph that could be mismeasured.

    The row is taken to start in **column 0**, which is what
    :mod:`~meshterm.ui.tui.fastrender` guarantees: it addresses the row, then writes it whole.

    A row holding only :data:`_TEXT_GLYPHS` comes back unchanged and untouched — one scan, no
    walk, no allocation — because that is nearly every row the app draws.

    Args:
        row: One composed terminal row: text with the theme's escape sequences in it, already
            sliced to the terminal's width.

    Returns:
        The row, with ``CSI n G`` inserted wherever the cursor has to be re-pinned.
    """
    if _UNPINNED.search(row) is None:
        return row
    hit = _CACHE.get(row)
    if hit is not None:
        _CACHE.move_to_end(row)
        return hit
    out = _snapped(row)
    _CACHE[row] = out
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return out


def _snapped(row: str) -> str:
    """Walk ``row`` glyph by glyph, tracking the column, and pin where :func:`_pinned` says.

    The address is written **lazily** — held over until something drawable actually follows it —
    so a row that ends on an emoji (a chat line, very often) pays nothing for an address nothing
    would have used, and a style change between two glyphs stays where the theme put it.
    """
    parts: list[str] = []
    column = 0
    owed = -1  # the column a held-over address names, or -1 when none is owed
    position = 0
    for escape in _ESCAPE.finditer(row):
        column, owed = _pin_text(row[position : escape.start()], parts, column, owed)
        parts.append(escape.group())
        position = escape.end()
    column, owed = _pin_text(row[position:], parts, column, owed)
    return "".join(parts)


def _pin_text(text: str, parts: list[str], column: int, owed: int) -> tuple[int, int]:
    """Append one escape-free stretch of ``text`` to ``parts``, pinned glyph by glyph.

    Args:
        text: The stretch to append, with no escape sequence in it.
        parts: The row being built, appended to in place.
        column: The column this stretch starts in.
        owed: The column a held-over address names, or ``-1`` for none.

    Returns:
        The column the stretch ends in, and the address it ends owing.
    """
    for cluster in clusters(text):
        if owed >= 0:
            parts.append(f"\x1b[{owed + 1}G")
            owed = -1
        width = cell_len(cluster)
        pinned = _pinned(cluster)
        if pinned and width > 1:
            parts.append(_erase(width))
        parts.append(cluster)
        column += width
        if pinned:
            owed = column
    return column, owed


def _lone_pinned_glyph(data: str) -> bool:
    """Whether ``data`` is exactly one glyph, and one :func:`_pinned` says needs pinning.

    What prompt_toolkit's renderer writes is one screen cell's content at a time — a character,
    or a whole sequence it was handed as one (see
    :class:`~meshterm.ui.tui.emoji_width.ClusterTextControl`) — so that is the only shape pinned.
    Anything longer is not a cell, and passes through as it came.
    """
    if len(data) == 1:
        return bool(_UNPINNED.match(data))
    for cluster in clusters(data):
        return cluster == data
    return False


class PinnedOutput:
    """A prompt_toolkit ``Output`` proxy that pins every uncertain glyph it is asked to write.

    prompt_toolkit's renderer writes a screen cell with ``write`` and then adds that cell's
    measured width to a cursor it keeps for itself; every later move on the row is computed
    from that cursor, relatively. Where the terminal draws the glyph at another width, the two
    cursors part company and every move after it lands a column out. This proxy closes the gap
    at the one moment it opens: a glyph :func:`_pinned` cannot vouch for is written between a
    cursor save and a restore, and followed by a forward step of exactly the width the renderer
    is about to add — so the terminal's cursor is back in step before the renderer's next move.

    Everything else — ``write_raw``, the cursor moves, the size, the attributes — forwards to the
    wrapped output untouched, and so does ``write`` for anything that is not a lone uncertain
    glyph, which is almost every call it gets.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap ``inner``, the concrete prompt_toolkit output for the real terminal."""
        self._inner = inner

    def write(self, data: str) -> None:
        """Write ``data``, pinning the cursor after it when it is a glyph worth pinning."""
        inner = self._inner
        if data.isascii() or not _lone_pinned_glyph(data):
            inner.write(data)
            return
        width = get_cwidth(data)
        # The renderer skips the cells after a wide glyph as the glyph's own and never writes
        # them, so erasing them first is what keeps a glyph drawn narrower from leaving the
        # last frame's character in the cell it did not cover.
        inner.write_raw(_SAVE + _erase(width) if width > 1 else _SAVE)
        inner.write(data)
        # A forward step of zero is read as a step of one, so a glyph that measures nothing
        # (a stranded joiner) just returns to where it started.
        inner.write_raw(f"{_RESTORE}\x1b[{width}C" if width else _RESTORE)

    def __getattr__(self, name: str) -> Any:
        """Forward every other attribute and method straight to the wrapped output."""
        return getattr(self._inner, name)


__all__ = ["PinnedOutput", "enabled", "snap_row"]
