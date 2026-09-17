# SPDX-License-Identifier: Apache-2.0
"""How wide a glyph is: one rule everywhere, and two cells wherever nobody can know.

Emoji column width is not standardized, and it cannot be discovered. Whether a terminal draws a
given emoji in one cell or two is decided by its font, glyph by glyph, with no rule behind the
split: the same terminal draws ``👋`` in one cell and ``📡`` in two, ``☀️`` in one and ``🛩️`` in
two. No escape sequence reports a renderer's width back to the program, and a cursor probe does
not help: on a split terminal (VS Code, Windows Terminal) the component that tracks the cursor and
the component that paints the glyph are different programs that disagree with each other, and a
probe can only ask the first.

This module used to answer that with a probe and two hand-curated exception sets — glyphs
confirmed, one at a time, to be drawn narrower or wider than the tables said. That lined up every
row somebody had already looked at and none of the rows nobody had, and a contact's name arrives
off the air, written by a stranger, carrying whatever they typed. The sets were retired for that
reason, along with the probe that decided when to apply them.

**What replaced them is two halves, and neither needs to know what the font does.**

* **Placement** belongs to :mod:`~meshterm.ui.tui.colsnap`: every glyph whose drawn width is
  uncertain is written with its column pinned, so whatever width the terminal gives it, the
  glyphs after it land in the columns the app measured.
* **Measurement** belongs here: every glyph that *may* be drawn as an emoji is reserved **two
  cells**, the most a single glyph ever draws. Pinned into two cells, a glyph the font draws in two
  fills them exactly, and one it draws in one leaves a blank beside it — never an overwrite, never
  a shift. That is what lines up the text *after* an emoji from row to row, which pinning alone
  cannot do: it pins to the measured column, and the measurement was the thing that varied.

**"May be drawn as an emoji"** is read off Unicode, never off a font:

* A glyph built from several codepoints: a variation-selector sequence (``☀️``), a ZWJ sequence
  (``👨‍👩‍👧``), a keycap (``1️⃣``), a flag's pair of Regional Indicators (``🇨🇦``).
* A lone codepoint Unicode marks as an emoji with *text* presentation by default (``☀``, ``✈``,
  ``❤``) — the set a variation selector promotes to two, which Rich ships as ``narrow_to_wide`` —
  and a lone Regional Indicator. A font draws either kind either way.
* An emoji whose default presentation is already emoji (``📡``, ``👋``) measures two in both
  width authorities as it stands, and is left to them.

**Two exceptions, both measured at their one cell.** Rich's own single-cell ranges (``©``, ``▶``,
box drawing) are measured by a fast path that never consults a patch, so everything else has to
agree with it. And :data:`_APP_TEXT_MARKS` is the handful of text-default emoji MeshTerm draws
*itself*, as one-cell marks in its own lexicon (``⚠``, ``↕``, ``🗑``). Those are a closed
vocabulary, not content: every glyph in the list is drawn by the app, the app is what checks them
(``meshterm specimen``), and a test holds every such glyph in the source to the list.

**One rule, every authority.** Rich measures in three places — a whole string, a single character,
and the grapheme spans it crops and wraps by — and prompt_toolkit in a fourth. :func:`install`
patches all four from the same rule (:func:`_glyph_width`) at once, and they must never differ:
Rich's segment splitter walks a string a codepoint at a time until a whole-string width meets the
cut it was asked for, and a character measured one inside a string measured two is a cut that walk
never meets.

**Delivery.** prompt_toolkit lays out one codepoint at a time, and folds a zero-width codepoint into
the cell before it while keeping that cell's place, so a mark measured one and followed by a
selector would become a two-cell glyph in a one-cell slot. :class:`ClusterTextControl` hands every
multi-codepoint glyph to it as a single fragment instead, laid out and written as the one glyph it
is.

**Where it applies.** :func:`install` runs once, when the full-screen app starts on a platform that
draws emoji, before its first frame. Nothing else installs it. Tests, piped output and the command
line keep the stock tables, because nothing pins their output: a glyph reserved two cells and drawn
in one, with no pin after it, pulls the rest of its row a column in.

Two jobs beside the measuring belong here, because they are the same question asked of the same
glyphs. **Where a string may be cut** (:func:`cut_cells`, over :func:`clusters`): a lane that
truncates a name a codepoint at a time cuts through the middle of a glyph, and the pieces that
leaves — a stranded joiner, half a flag — are nothing a terminal draws as measured. And **what may
be drawn at all** (:func:`drawable`): an advert name is written by a stranger and can carry a
newline or an escape, which no width can make safe.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterator
from functools import lru_cache

from prompt_toolkit.layout.controls import FormattedTextControl
from rich.cells import cell_len

#: The zero-width joiner: the tell that the codepoints it sits between are **one glyph**
#: (``🤷‍♂️``, ``👨‍👩‍👧``), which a terminal draws in the width of the sequence's base.
_ZWJ = "\u200d"

#: Variation selector 16, the request for emoji presentation.
_VS16 = "\ufe0f"

#: The combining enclosing keycap, which turns the digit or sign before it (and the selector
#: between them) into one keycap glyph: ``1️⃣``.
_KEYCAP = "\u20e3"

#: The five Fitzpatrick skin-tone modifiers (U+1F3FB–U+1F3FF). One following an emoji base
#: *recolours* that glyph rather than drawing one of its own — ``👍🏽`` is a thumb, not a thumb
#: beside a swatch — so it occupies no cell. Rich's table says zero; wcwidth says two.
_MODIFIERS = frozenset(chr(cp) for cp in range(0x1F3FB, 0x1F400))

#: What may trail a base inside one glyph with no joiner in front of it: its variation selector,
#: its skin tone, and a keycap's enclosing mark. A joined run gathers these after its base and
#: after every component.
_EXTENDERS = frozenset((_VS16, _KEYCAP, *_MODIFIERS))

#: Unicode general categories holding nothing a terminal can draw: control characters
#: (``Cc``), the format and bidi-control block (``Cf``), surrogates (``Cs``), and the line
#: and paragraph separators (``Zl``/``Zp``). See :func:`drawable`, which folds them to a
#: space — and keeps the one ``Cf`` codepoint that matters, the zero-width joiner.
_UNDRAWABLE = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))

#: The text-default emoji MeshTerm draws *itself*, as one-cell marks in its own lexicon, and so
#: measures at one cell rather than reserving two. Each is a glyph the app has put on screen
#: deliberately and checks through ``meshterm specimen``, not one that arrived in somebody's
#: name, so its width is a fact about the app's own vocabulary rather than a guess about a font.
#:
#: The list is closed by a test, not by care: every text-default emoji in the package's source
#: must be named here, so a new mark cannot slip into the reserve-two rule unnoticed. A glyph
#: that is not here is reserved two cells, which costs a blank beside it and never a misaligned
#: row — which is why this is a list that may be short, where the sets it replaced had to be
#: complete.
_APP_TEXT_MARKS = frozenset(
    (
        "⚠",  # ⚠ the warning mark
        "↔",  # ↔ a two-way link: the path composer, the key legend
        "↕",  # ↕ reorder
        "↩",  # ↩ reply, in the chat composer
        "⌨",  # ⌨ the command line
        "⚙",  # ⚙ a parameter
        "\U0001f5d1",  # 🗑 clear, delete
        "\U0001f578",  # 🕸 a Trophy case record
        "\U0001f6e3",  # 🛣 a Trophy case record
    )
)

#: Set once :func:`install` has run, so repeated calls are cheap no-ops.
_INSTALLED = False

#: Whether the width authorities have been redirected through this module. Gates
#: :class:`ClusterTextControl`: handing prompt_toolkit a multi-codepoint glyph as one character is
#: only right once something measures that character as one glyph, so the delivery and the
#: measurement are switched on together or not at all.
_CLUSTERS = False


def _is_regional_indicator(char: str) -> bool:
    """Whether ``char`` is a single Regional Indicator Symbol — a country flag's building block.

    The two-letter flags (``🇨🇦``, ``🇺🇸``) are a grapheme pair drawn from this block
    (U+1F1E6–U+1F1FF). The pair is one glyph; either half alone is a letter-like glyph a font
    may draw at either width. It is a whole category, never a per-country entry: the indicators
    are shared across flags (``🇨🇦`` and ``🇨🇳`` both start with ``C``).
    """
    return len(char) == 1 and 0x1F1E6 <= ord(char) <= 0x1F1FF


def _joins(char: str) -> bool:
    """Whether a zero-width joiner in front of ``char`` really joins it to the glyph before.

    Every component an emoji ZWJ sequence joins is a pictograph — ``♂``, ``☠``, ``❤``, ``💻``,
    all at U+2000 and above — so that is the test. What it rules out is a **stranded** joiner.
    A MeshCore advert name is cut at a byte limit, and a name that ended on a joined emoji
    (``That's So Fetch 🏴‍☠️``) arrives cut just past the joiner, followed by whatever the row
    puts after the name: a space of lane padding, the next lane. Folding that character into the
    flag would take its cell from it while the terminal still drew it. It also leaves the joiners
    of the scripts that use them between letters (the Indic conjuncts, below U+2000) as the
    letters they are.
    """
    return char >= "\u2000" and not char.isspace()


@lru_cache(maxsize=1)
def _reserved() -> frozenset[str]:
    """Every lone codepoint that is reserved two cells, read off Unicode once.

    The text-default emoji Rich's own table lists (``narrow_to_wide``, the codepoints a variation
    selector promotes), less the ones measured by Rich's single-cell fast path and the app's own
    marks (see the module docstring for both), plus the Regional Indicators.
    """
    from rich._unicode_data import load
    from rich.cells import _SINGLE_CELLS

    text_default = frozenset(
        char
        for char in load("auto").narrow_to_wide
        if not char.isascii() and char not in _SINGLE_CELLS and char not in _APP_TEXT_MARKS
    )
    return text_default | frozenset(chr(cp) for cp in range(0x1F1E6, 0x1F200))


@lru_cache(maxsize=1)
def _probe() -> re.Pattern[str]:
    """Matches the first codepoint in a string that could make :func:`_glyph_width` differ.

    A string with none of these measures exactly as the stock tables say, so it is handed to
    them untouched — one C-speed scan instead of a cluster walk, which is nearly every string.
    """
    chars = {_ZWJ, _VS16, _KEYCAP, *_MODIFIERS, *_reserved()}
    return re.compile("[" + "".join(re.escape(char) for char in sorted(chars)) + "]")


def _override(char: str) -> int | None:
    """What this module says a lone codepoint measures, or ``None`` to take the stock answer.

    A skin tone takes no cell (wcwidth gives it two). A reserved codepoint (:func:`_reserved`)
    takes two. Everything else is the stock table's business.
    """
    if char in _MODIFIERS:
        return 0
    if char in _reserved():
        return 2
    return None


def _glyph_width(glyph: str, stock: Callable[[str], int]) -> int:
    """The cells one glyph is reserved — the single rule every width authority is patched with.

    A glyph built from several codepoints that carries a joiner, a variation selector or a keycap,
    or is a flag's indicator pair, is reserved two. Anything else is its base codepoint: a skin tone
    rides along with its base, so a toned emoji measures what the emoji does, and a letter wearing a
    stray modifier stays a letter.

    Args:
        glyph: One glyph, as :func:`clusters` splits it.
        stock: The stock width of a single codepoint, in the authority being patched.

    Returns:
        The glyph's width in cells.
    """
    base = glyph[0]
    if len(glyph) > 1 and (
        _ZWJ in glyph or _VS16 in glyph or _KEYCAP in glyph or _is_regional_indicator(base)
    ):
        return 2
    width = _override(base)
    return max(0, stock(base)) if width is None else width


def clusters(text: str) -> Iterator[str]:
    """Split ``text`` into the glyphs the terminal actually draws, never inside one.

    The same run :func:`_join_clusters` gathers out of a laid-out line, read off a plain
    string instead: a base, whatever extends it (:data:`_EXTENDERS` — a variation selector, a
    skin tone, a keycap's enclosing mark), then any number of *joiner + component + extenders*
    groups, plus the Regional Indicator **pair** a country flag is drawn from. A joiner that
    joins nothing (:func:`_joins`) is a cluster of its own, because it is not part of the glyph
    before it.

    The unit a lane has to cut on and a width is measured by: a cut through the middle of a
    glyph leaves pieces that are nothing a terminal draws as measured — a lone Regional
    Indicator drawn as a letter, a skin tone with no base, or a stranded joiner. See
    :func:`cut_cells`.
    """
    index = 0
    count = len(text)
    while index < count:
        start = index
        index += 1
        while index < count and text[index] in _EXTENDERS:
            index += 1
        if _is_regional_indicator(text[start]) and index < count:
            if _is_regional_indicator(text[index]):
                index += 1
                while index < count and text[index] in _EXTENDERS:
                    index += 1
        while index + 1 < count and text[index] == _ZWJ and _joins(text[index + 1]):
            index += 2  # the joiner and the codepoint it joins
            while index < count and text[index] in _EXTENDERS:
                index += 1
        yield text[start:index]


def cut_cells(text: str, width: int) -> str:
    """The longest prefix of ``text`` that fits ``width`` cells, cut **between** glyphs.

    What a fixed lane needs when a name outruns it (:func:`~meshterm.ui.menus.fit_cells`),
    and the reason it cannot simply walk the string a character at a time: a MeshCore name
    is written by whoever owns the radio, so it arrives carrying whatever emoji they typed,
    and a codepoint-by-codepoint cut lands inside one about as often as not. Cutting
    ``"👨‍👩‍👧"`` after its first joiner leaves a stranded joiner at the end of the lane, and
    cutting a flag in half leaves a lone Regional Indicator, which is a letter rather than half
    a flag.

    So the prefix is built out of whole :func:`clusters`, and a trailing joiner — stranded
    by the cut itself, or already stranded in the name — is dropped rather than left beside
    whatever the caller appends.

    Args:
        text: The text to cut.
        width: The most cells the result may occupy.

    Returns:
        A prefix measuring at most ``width`` cells, with every glyph in it whole.
    """
    kept: list[str] = []
    used = 0
    for cluster in clusters(text):
        size = cell_len(cluster)
        if used + size > width:
            break
        kept.append(cluster)
        used += size
    while kept and kept[-1] == _ZWJ:
        kept.pop()
    return "".join(kept)


def drawable(text: str) -> str:
    """``text`` with every codepoint a terminal cannot *draw* folded to a space.

    A name comes off the air from a stranger's radio, and nothing on the way in promises it
    holds only characters. A newline in one ends the row mid-lane; an ``ESC`` starts an
    escape sequence in the middle of a screen the app composed; a bidi override (``U+202E``)
    reverses everything drawn after it. None of them is a width question at all — they are
    text that must not reach the terminal, so a lane folds them to the one character that is
    always safe.

    Folded: control characters, the format and bidi-control block, surrogates, and the line
    and paragraph separators. **Kept:** the zero-width joiner, which is format-class too and
    is the tell that holds an emoji sequence together, along with every mark and modifier a
    glyph is built from. Private-use codepoints are kept as well — that block is where a
    Nerd Font keeps the powerline glyphs the path line is drawn with.
    """
    return "".join(
        " " if char != _ZWJ and unicodedata.category(char) in _UNDRAWABLE else char for char in text
    )


def install() -> None:
    """Patch every width authority with the reserve-two rule, once, before the first frame.

    Rich's whole-string width, its single-character width (in both modules that read it) and its
    grapheme spans, and prompt_toolkit's width cache, all from :func:`_glyph_width` — see the
    module docstring for why all four and why at once. Each patch hands a string with nothing
    reservable in it straight to the stock code it replaced, so the common case pays one scan.

    Safe to call more than once; only the first call does anything. Called from the menu loop on
    a platform that draws emoji; see the module docstring for why nothing else calls it.
    """
    global _INSTALLED, _CLUSTERS
    if _INSTALLED:
        return
    _INSTALLED = True

    import prompt_toolkit.utils as ptu
    import rich.cells as cells
    import rich.segment as segment

    from .render import _ANSI_CACHE

    probe = _probe()
    stock_size = cells.get_character_cell_size
    stock_cell_len = cells._cell_len
    stock_split = cells.split_graphemes
    single_cells = cells._is_single_cell_widths

    @lru_cache(maxsize=4096)
    def get_character_cell_size(character: str, unicode_version: str = "auto") -> int:
        width = _override(character)
        return stock_size(character, unicode_version) if width is None else width

    def _cell_len(text: str, unicode_version: str = "auto") -> int:
        if single_cells(text):
            return len(text)
        if probe.search(text) is None:
            return stock_cell_len(text, unicode_version)
        return sum(
            _glyph_width(glyph, lambda char: stock_size(char, unicode_version))
            for glyph in clusters(text)
        )

    def split_graphemes(text: str, unicode_version: str = "auto") -> tuple[list, int]:
        if probe.search(text) is None:
            return stock_split(text, unicode_version)
        spans: list[tuple[int, int, int]] = []
        total = 0
        index = 0
        for glyph in clusters(text):
            end = index + len(glyph)
            width = _glyph_width(glyph, lambda char: stock_size(char, unicode_version))
            if width == 0 and spans:
                # Zero-width pieces belong to the glyph before them, as Rich's own splitter has it,
                # so a crop never lands between a letter and its mark.
                start, _end, size = spans[-1]
                spans[-1] = (start, end, size)
            else:
                spans.append((index, end, width))
                total += width
            index = end
        return spans, total

    class _ReservingCache(type(ptu._CHAR_SIZES_CACHE)):  # type: ignore[misc]
        """prompt_toolkit's width cache, answering from :func:`_glyph_width`."""

        def __missing__(self, string: str) -> int:
            if len(string) == 1:
                width = _override(string)
                if width is None:
                    return super().__missing__(string)
                self[string] = width
                return width
            if probe.search(string) is None:
                return super().__missing__(string)
            total = sum(_glyph_width(glyph, self.__getitem__) for glyph in clusters(string))
            # A whole line holding an emoji is cheap to recompute, and would otherwise slip past
            # the base class's rotation of long strings and grow the cache without bound.
            if len(string) <= self.LONG_STRING_MIN_LEN:
                self[string] = total
            return total

    cells.get_character_cell_size = get_character_cell_size
    segment.get_character_cell_size = get_character_cell_size
    cells._cell_len = _cell_len
    cells.split_graphemes = split_graphemes
    cells.cached_cell_len.cache_clear()
    ptu._CHAR_SIZES_CACHE = _ReservingCache()
    _CLUSTERS = True
    # The measurement just changed under every renderable, so anything rasterized before
    # (nothing, in the normal boot order — but never trust that) is stale.
    _ANSI_CACHE.clear()


class _Cluster(str):
    """A multi-codepoint glyph that must reach the screen buffer as **one** character.

    prompt_toolkit builds its screen a codepoint at a time — ``for c in text`` in
    ``Window._copy_body`` — and gives each one its own cell-occupying ``Char``. A glyph measured
    correctly at two cells is therefore still laid out as its parts: a shrug followed by a male
    sign, a flag as two letters, a warning mark with a selector folded into a slot one cell wide.
    Iterating a cluster yields the whole glyph instead, so that loop makes a single ``Char`` of it:
    one width in prompt_toolkit's arithmetic, every codepoint written to the terminal back to back,
    and one composed glyph on the screen.

    It is a ``str`` subclass rather than a wrapper because it has to survive as ordinary text
    through everything else prompt_toolkit does with a fragment — joining, slicing, comparing.
    ``str`` methods return plain ``str``, so the marking is lost on the first ``split``; that is
    why :class:`ClusterTextControl` applies it to the finished lines and nothing earlier.
    """

    __slots__ = ()

    def __iter__(self):  # type: ignore[override]
        yield str(self)


#: Codepoints whose presence in a laid-out line means some glyph on it is built from several.
_JOINERS = frozenset((_ZWJ, *_EXTENDERS))


def _join_clusters(line: list) -> list:
    """Merge each multi-codepoint glyph in one screen line into a single :class:`_Cluster` fragment.

    The line arrives one codepoint per fragment (that is what
    :class:`~prompt_toolkit.formatted_text.ANSI` produces), so a glyph built from several is a run
    to be gathered, the same run :func:`clusters` reads off a plain string: a base and whatever
    extends it, a flag's indicator pair, and any number of *joiner + component + extenders* groups.

    Every such glyph is merged, a lone selector pair and a toned emoji included. prompt_toolkit
    would fold those into the cell before them on its own, but it keeps that cell's place while it
    does, so a mark measured one cell and followed by a selector would become a two-cell glyph in a
    one-cell slot. And the renderer's writes are what :class:`~meshterm.ui.tui.colsnap.PinnedOutput`
    pins: a pin between the two halves of a flag is a cursor move between them, after which a
    terminal no longer joins them into one glyph at all.

    A joiner with no pictograph after it (see :func:`_joins`) is not part of any glyph, and keeps
    its fragment. The whole line is handed back unchanged when nothing on it could start or extend
    such a glyph, which is almost every line; the scan runs only when a control's content changes.
    """
    if not any(item[1] in _JOINERS or _is_regional_indicator(item[1]) for item in line):
        return line
    merged: list = []
    index = 0
    count = len(line)
    while index < count:
        end = index + 1
        while end < count and line[end][1] in _EXTENDERS:
            end += 1
        if (
            _is_regional_indicator(line[index][1])
            and end < count
            and _is_regional_indicator(line[end][1])
        ):
            end += 1
            while end < count and line[end][1] in _EXTENDERS:
                end += 1
        while end + 1 < count and line[end][1] == _ZWJ and _joins(line[end + 1][1]):
            end += 2  # the joiner and the codepoint it joins
            while end < count and line[end][1] in _EXTENDERS:
                end += 1
        if end - index > 1:
            text = "".join(item[1] for item in line[index:end])
            merged.append((line[index][0], _Cluster(text)))
        else:
            merged.append(line[index])
        index = end
    return merged


class ClusterTextControl(FormattedTextControl):
    """A :class:`~prompt_toolkit.layout.controls.FormattedTextControl` that keeps glyphs whole.

    The control is the last place a screen's text is still arranged in lines and still ours to
    touch, which is exactly what merging a glyph needs: ``split_lines`` runs before this point and
    returns plain ``str`` parts, so a :class:`_Cluster` marked any earlier would not survive to be
    laid out. Everything downstream — the width lookup, the wrapping, the screen buffer — reads the
    merged lines.

    Merging is gated on :func:`install` having run, because handing prompt_toolkit a glyph as one
    character is only right once its cache measures that character as one glyph. Off that path
    (a test, a platform that draws no emoji) this is its base class exactly.
    """

    def create_content(self, width: int, height: int | None):  # type: ignore[override]
        """The base class's content, with each line's multi-codepoint glyphs joined into one.

        The joining is wrapped around the returned object's line lookup and memoised
        behind it, because prompt_toolkit hands back the same content object paint after
        paint — walking every line of every frame would cost far more than it saves.
        """
        content = super().create_content(width, height)
        # The base class caches its ``UIContent`` per (fragments, width, cursor), so the same
        # object comes back paint after paint: wrap its line lookup once, and memoise the merge
        # behind it, rather than re-walking every line of every frame.
        if _CLUSTERS and not getattr(content, "_clusters_merged", False):
            content._clusters_merged = True  # type: ignore[attr-defined]
            source = content.get_line
            cache: dict[int, list] = {}

            def get_line(i: int, _source=source, _cache=cache) -> list:
                line = _cache.get(i)
                if line is None:
                    line = _cache[i] = _join_clusters(_source(i))
                return line

            content.get_line = get_line
        return content


__all__ = ["ClusterTextControl", "clusters", "cut_cells", "drawable", "install"]
