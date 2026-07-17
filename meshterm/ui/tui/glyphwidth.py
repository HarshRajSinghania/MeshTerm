"""Align the app's glyph-width model with how the terminal actually draws emoji.

Terminal rendering is a three-party agreement about how many cells each glyph occupies, and
this app has three parties: **Rich** lays out every line (wrapping, padding, and — the part
that bites — the position of a panel's right border), **prompt_toolkit** diffs one frame
against the last and steps a *relative* cursor by its own width model, and the **terminal**
draws the result. Rich and prompt_toolkit both measure an emoji (``👋``, ``🕒``, ``⚡``) as
two cells, via ``wcwidth``. The terminals this app runs on draw it in a *single* cell.

That one-cell disagreement is the whole bug:

* prompt_toolkit reserves two cells but the terminal advances one, so from the emoji rightward
  the diff's cursor model is a column ahead of the terminal. On the next paint, a cell to the
  emoji's right that pt repositions to lands a column early, and the stale cell it meant to
  overwrite lingers — the "characters that don't get cleared" smear.
* Rich sizes the row assuming two cells, so a bordered line with one emoji renders one cell
  short and its right border sits a column in from the frame edge — a ragged notch with a
  blank beside it.

Both vanish the moment all three parties agree. We can't change the terminal, so we teach the
two width authorities the terminal's truth: an emoji is one cell. Rich then pads the row to a
full-width, flush border, and prompt_toolkit's relative cursor tracks the terminal exactly, so
ordinary differential painting stays synced and nothing lingers — no whole-frame repaint, no
compositing tricks. CJK and fullwidth glyphs, which terminals *do* draw two cells wide, are
left at two; node-type marks and braille are already one cell everywhere and are untouched.

This is the sibling of :class:`~meshterm.ui.tui.session._WidthExtendedOutput`: both correct a
width assumption the terminal breaks, in the one place the whole pipeline reads it. Gated by
``MESHTERM_NARROW_EMOJI`` (default on); set it to ``0`` on a terminal that genuinely draws
emoji two cells wide, where the alignment would instead *introduce* a one-cell error.
"""

from __future__ import annotations

import os

from wcwidth import wcwidth

#: Whether to align the width model to a one-cell emoji (see the module docstring). Default on;
#: ``MESHTERM_NARROW_EMOJI=0`` opts out for a terminal that draws emoji two cells wide.
_ALIGN_NARROW_EMOJI = os.environ.get("MESHTERM_NARROW_EMOJI", "1") != "0"


def _is_cjk_or_fullwidth(cp: int) -> bool:
    """Whether ``cp`` is a codepoint terminals reliably draw *two* cells wide.

    These are the East Asian Wide/Fullwidth blocks — CJK ideographs and their radicals, the
    kana and Hangul syllables, and the fullwidth ASCII forms. They share ``wcwidth``'s value of
    two with emoji, so width alone can't tell them apart; the split is by block. Everything here
    is left at two cells, because the terminal draws it at two — only the *rest* of the width-2
    range (the emoji) is the part the terminal narrows.
    """
    return (
        0x1100 <= cp <= 0x115F      # Hangul Jamo
        or 0x2E80 <= cp <= 0x9FFF   # CJK radicals, Kangxi, kana, CJK Unified Ideographs
        or 0xA000 <= cp <= 0xA4CF   # Yi syllables
        or 0xAC00 <= cp <= 0xD7A3   # Hangul syllables
        or 0xF900 <= cp <= 0xFAFF   # CJK compatibility ideographs
        or 0xFE30 <= cp <= 0xFE4F   # CJK compatibility forms
        or 0xFF00 <= cp <= 0xFF60   # Fullwidth forms
        or 0xFFE0 <= cp <= 0xFFE6   # Fullwidth signs
        or 0x20000 <= cp <= 0x3FFFD  # CJK Unified Ideographs Extension B and beyond
    )


def is_narrow_emoji(char: str) -> bool:
    """Whether ``char`` is a glyph ``wcwidth`` calls two cells but the terminal draws in one.

    True for an emoji or pictographic symbol — a single codepoint that ``wcwidth`` measures at
    two and that is not in a CJK/fullwidth block. The ``>= 0x1100`` guard skips the ASCII/Latin
    bulk before the width lookup; ``len == 1`` keeps this to lone codepoints (multi-codepoint
    grapheme clusters — flags, ZWJ sequences — are left to their parts).
    """
    if len(char) != 1:
        return False
    cp = ord(char)
    return cp >= 0x1100 and wcwidth(char) == 2 and not _is_cjk_or_fullwidth(cp)


#: Set once :func:`align_emoji_cell_width` has patched the width authorities, so repeated calls
#: (one per :class:`~meshterm.ui.tui.session.TuiSession`) patch the global state exactly once.
_applied = False


def align_emoji_cell_width() -> None:
    """Teach Rich and prompt_toolkit that an emoji is one cell, matching the terminal.

    Idempotent and gated by :data:`_ALIGN_NARROW_EMOJI`. Patches the two width authorities the
    render pipeline reads:

    * **prompt_toolkit** — ``prompt_toolkit.utils._CHAR_SIZES_CACHE``, the dict behind
      ``get_cwidth`` and therefore every ``Char.width``. We swap in a subclass whose
      ``__missing__`` returns one for a narrow emoji and otherwise defers to the original
      ``wcwidth`` logic, so the diff's cursor model matches the terminal.
    * **Rich** — ``rich.cells.get_character_cell_size``, resolved as a module global by
      ``_cell_len`` on every measurement. We wrap it to return one for a narrow emoji, then
      clear ``cached_cell_len``'s cache so no pre-patch measurement survives, so Rich lays out
      (and borders) each row at the terminal's true width.

    Called at :class:`~meshterm.ui.tui.session.TuiSession` construction, before any frame is
    drawn, so no stale two-cell ``Char`` or cached measurement is ever painted.
    """
    global _applied
    if _applied or not _ALIGN_NARROW_EMOJI:
        return
    _applied = True

    import prompt_toolkit.utils as ptu

    base_cache_cls = type(ptu._CHAR_SIZES_CACHE)

    class _NarrowEmojiCache(base_cache_cls):  # type: ignore[valid-type, misc]
        def __missing__(self, string: str) -> int:
            if is_narrow_emoji(string):
                self[string] = 1
                return 1
            return super().__missing__(string)

    ptu._CHAR_SIZES_CACHE = _NarrowEmojiCache()

    import rich.cells as rc

    _measure = rc.get_character_cell_size

    def _narrow_emoji_cell_size(character: str, unicode_version: str = "auto") -> int:
        if is_narrow_emoji(character):
            return 1
        return _measure(character, unicode_version)

    rc.get_character_cell_size = _narrow_emoji_cell_size
    rc.cached_cell_len.cache_clear()
