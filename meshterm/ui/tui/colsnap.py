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

* Drawn **narrower** than we reserved, it leaves a blank cell after it (the row is erased
  before it is drawn, so that cell is bare page rather than debris).
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

**Where this applies.** :mod:`~meshterm.ui.tui.fastrender` writes each changed row itself, as
one run from column 0, and that is the only place in the app where a row's bytes are ours to
touch on their way out. A frame prompt_toolkit lays out instead — a floating dialog, and the
backdrop it forces a full repaint of — still goes through its differential renderer, which
tracks the cursor in arithmetic of its own and is not corrected here.

The escape hatch is ``MESHTERM_COLUMN_SNAP=0``, the same shape as the emoji-width knobs next
door (``MESHTERM_EMOJI_VS16``, ``MESHTERM_NARROW_EMOJI``, ``MESHTERM_WIDE_EMOJI``) and for the
same reason: this is terminal geometry, read once at session build, not a choice anybody makes
about how the app behaves.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict

from rich.cells import cell_len

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


def enabled() -> bool:
    """Whether written rows carry absolute column addresses.

    On always, with ``MESHTERM_COLUMN_SNAP=0`` to turn it off for one run — the escape hatch a
    terminal that mishandles ``CSI n G`` would need, and the switch that shows what the
    alignment looks like without it. Read at session build, like its neighbours.
    """
    return os.environ.get("MESHTERM_COLUMN_SNAP") != "0"


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
        parts.append(cluster)
        column += cell_len(cluster)
        if _pinned(cluster):
            owed = column
    return column, owed


__all__ = ["enabled", "snap_row"]
