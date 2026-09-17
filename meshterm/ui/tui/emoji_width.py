# SPDX-License-Identifier: Apache-2.0
"""Align Rich's emoji cell-width table with the terminal's *actual* rendering.

Emoji column width is not standardized. Whether a variation-selector-16 sequence
such as ``"☀️"`` (a sun, U+2600 U+FE0F) occupies one cell or two depends on
the terminal emulator and the font it uses. Rich follows Unicode TR51 and counts
such a sequence as **2** columns; many terminals — notably Windows ``conhost`` and
Windows Terminal with common fonts — draw it as **1**. When the two disagree, Rich
pads the line assuming the wrong width and the panel border that follows the text
lands a column off (the classic "the sun pushes the right border left" bug).

There is no static table that is correct on every terminal, and — importantly —
no fully reliable way to *measure* it either. A cursor-position probe only reveals
how the layer that **tracks the cursor** counts the emoji, which on modern split
terminals is a different component than the one that **paints** it:

* **VS Code integrated terminal** — ConPTY advances the cursor by 2 for ``☀️``,
  but the xterm.js renderer paints it in 1 cell. The border you see follows the
  renderer; a probe can only see ConPTY. They disagree, so the probe is wrong here.
* **Windows Terminal** has the same PTY/renderer split.
* A classic **conhost** window or a traditional **POSIX terminal** does its own
  cursor tracking and painting, so there a probe *is* the ground truth.

Because no escape sequence reports the renderer's width back to the program, we
decide the width for :data:`_PROBE` in layers, most trustworthy first:

1. An explicit override via ``MESHTERM_EMOJI_VS16=1|2`` — always wins.
2. A fingerprint of renderers known to paint VS16 emoji narrow while their PTY
   counts them wide (VS Code, Windows Terminal) — where a probe would mislead.
3. Otherwise, a live cursor probe (``GetConsoleScreenBufferInfo`` on Windows, a
   ``ESC[6n`` Device Status Report on POSIX), valid on terminals that track and
   paint in the same layer.

If the chosen width is 1, we patch Rich's string-measuring function so every panel,
table, and text widget — menus and chat bubbles alike — lines up. On non-interactive
runs (tests, piped output, scripted CLI) we do nothing and keep Rich's defaults.

The same "width 1" verdict also narrows a small, curated set of *lone* emoji — single
codepoints like ``👋`` that carry no variation selector yet are still drawn in one cell
here. Unlike a VS16 sequence there is no structural tell (a trailing ``U+FE0F``) and no
rule separating them from the lone emoji this terminal draws two wide (``📡``, ``💬``):
the split is the font's, not the codepoint's, and a cursor probe can't see it. So the set
is an explicit allowlist (:data:`_DEFAULT_NARROW_LONE`, extended without a code change via
``MESHTERM_NARROW_EMOJI``), and — because it is the panel border *prompt_toolkit* places,
not Rich — narrowing a lone emoji patches *both* width authorities, where the VS16 fix only
needed Rich (prompt_toolkit never promoted a VS16 sequence in the first place).

Country **flags** (``🇨🇦``, ``🇺🇸``) are the same "width 1" story but need no allowlist,
because for them there *is* a rule: a flag is a grapheme pair of Regional Indicator Symbols
(see :func:`_is_regional_indicator`), which the terminal — and Rich — draw as one two-cell
glyph, while prompt_toolkit's wcwidth counts each indicator as two and so measures the flag
as *four* (a two-column notch, worse than a lone emoji's one). Since the indicators are
shared across flags, they can only be handled as a whole category, never one country at a
time; narrowing every indicator to one cell makes any flag sum to two in both authorities.

Some VS16 sequences run the other way — this terminal draws them **two** cells where the "width 1"
verdict would narrow them — and they are carved back out into a curated *wide* set
(:data:`_DEFAULT_WIDE_BASE`, extended without a code change via ``MESHTERM_WIDE_EMOJI``) counted two
in **both** authorities. The renderer is simply not uniform: the probe ``☀️`` paints in one cell,
but ``🛩️`` (a small airplane, U+1F6E9 U+FE0F) paints in two, and narrowing it along with the rest
pulls its chat-row border a column short and smears the line. It is the mirror image of the
lone-narrow allowlist, curated for the same reason: which glyph a terminal draws wide is the font's
business, not the codepoint's, and a cursor probe reads the PTY, not the renderer. Keyed on the
*base* codepoint (the trailing ``U+FE0F`` is skipped regardless).

An emoji **ZWJ sequence** (``🤷‍♂️``, ``👨‍👩‍👧``, ``🏳️‍🌈``) is a different failure and
needs no curation at all, because it has a structural tell of its own: the zero-width joiner says
the codepoints around it are *one glyph*, and a terminal draws it in the width of its base. Rich's
stock measurement already knows this; prompt_toolkit's does not — it sums the parts, so it reserves
three cells for the shrug and **six** for the family, and every one of them pulls the row's right
border in. Both authorities are corrected the same way here (:func:`_joined_out`), and — because
prompt_toolkit builds its screen one *codepoint* at a time — the sequence is also handed to it as a
single fragment (:class:`ClusterTextControl`) so it becomes one ``Char`` rather than four. The
cluster measures whatever its base measures, so the two curated sets above still reach it: listing a
base in the wide set widens the whole sequence with it.

Two neighbours of that rule are structural too, and corrected at either width. A **skin tone**
(``👍🏽``, or the toned base of ``👨🏻‍💻``) recolours the glyph before it and takes no cell,
as Rich already measures it; wcwidth gives each modifier two, so prompt_toolkit measures it zero
here and a joined run carries it along. And a **stranded joiner** — a MeshCore name cut at its byte
limit just past the ZWJ of ``🏴‍☠️`` — joins nothing (:func:`_joins`): both authorities used to fold
the next character into the flag regardless, and that character is the next lane's padding.

A **bare** codepoint the authorities *already agree* measures one is not a candidate for that set,
however wide the glyph looks in a font book: without a variation selector asking for emoji
presentation, an emoji outside Emoji_Presentation (``🛣`` U+1F6E3, ``🕸`` U+1F578) draws as a
one-cell text glyph, which is what both authorities said. Forcing one to two reserves a cell the
terminal never draws and pulls the row's border a column *in* — see :data:`_DEFAULT_WIDE_BASE` for
the case that proved it.

**What this module is no longer answerable for.** A row's *layout* no longer rests on these
tables being right about a glyph. :mod:`~meshterm.ui.tui.colsnap` writes an absolute column
address after anything whose drawn width is uncertain, so a mismeasurement costs one cell inside
that glyph's own lane instead of every lane after it — which is the only answer that can reach a
name off the air, since the curated sets can only ever hold glyphs somebody has already looked
at. What those sets still decide is the *reservation*: a listed glyph gets neither a blank cell
beside it nor an overhang written over, so adding a confirmed one is still worth doing.

Two jobs beside the measuring belong here, because they are the same question asked of the same
table. **Where a string may be cut** (:func:`cut_cells`, over :func:`clusters`): a lane that
truncates a name a codepoint at a time cuts through the middle of a glyph, and the pieces that
leaves — a stranded joiner, half a flag — are exactly what every correction above is written
against. And **what may be drawn at all** (:func:`drawable`): an advert name is written by a
stranger and can carry a newline or an escape, which no width table can make safe.
"""

from __future__ import annotations

import os
import sys
import unicodedata
from collections.abc import Callable, Iterator

from prompt_toolkit.layout.controls import FormattedTextControl
from rich.cells import cell_len

from ...core import win32dll

#: The zero-width joiner: the tell that the codepoints it sits between are **one glyph**
#: (``🤷‍♂️``, ``👨‍👩‍👧``), which a terminal draws in the width of the sequence's base.
_ZWJ = "‍"

#: Variation selector 16, the request for emoji presentation.
_VS16 = "️"

#: The five Fitzpatrick skin-tone modifiers (U+1F3FB–U+1F3FF). One following an emoji base
#: *recolours* that glyph rather than drawing one of its own — ``👍🏽`` is a thumb, not a thumb
#: beside a swatch — so it occupies no cell. Rich's table already says zero; wcwidth says two,
#: which is how ``👨🏻‍💻`` came to cost prompt_toolkit four cells for a two-cell glyph.
_MODIFIERS = frozenset(chr(cp) for cp in range(0x1F3FB, 0x1F400))

#: What may trail a base inside one glyph with no joiner in front of it: its variation selector
#: and its skin tone. A joined run gathers these after its base and after every component.
_EXTENDERS = frozenset((_VS16, *_MODIFIERS))

#: Codepoints that never occupy a cell on their own: zero-width joiner and the
#: emoji-presentation variation selector. Skipping them means a base glyph is
#: measured at its own East-Asian width, with no VS16 "promote to 2" step.
_ZERO_WIDTH = (_ZWJ, _VS16)

#: Unicode general categories holding nothing a terminal can draw: control characters
#: (``Cc``), the format and bidi-control block (``Cf``), surrogates (``Cs``), and the line
#: and paragraph separators (``Zl``/``Zp``). See :func:`drawable`, which folds them to a
#: space — and keeps the one ``Cf`` codepoint that matters, the zero-width joiner.
_UNDRAWABLE = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))

#: The probe: a sun. Its base (U+2600) is East-Asian *narrow* (1), and Rich
#: promotes the U+2600+U+FE0F pair to 2. So a terminal that reports 1 for this is
#: exactly the terminal whose borders Rich currently misaligns.
_PROBE = "☀️"

#: Lone-codepoint emoji this terminal draws in a single cell even though wcwidth and Rich
#: both call them two — the residual of the VS16 story for glyphs that carry no variation
#: selector (``👋`` is the one confirmed here). There is no rule separating these from the
#: emoji the same terminal draws two wide (``📡``, ``💬``, the menu icons): the split is the
#: font's glyph, not the codepoint, and it cannot be probed (a cursor probe reads the PTY,
#: not the renderer — see the module docstring). So this is a curated allowlist, seeded with
#: the confirmed glyph and extended — no code change — through ``MESHTERM_NARROW_EMOJI``.
_DEFAULT_NARROW_LONE = "👋"

#: Base codepoints this terminal draws *two* cells wide where the width authorities say one —
#: the mirror of :data:`_DEFAULT_NARROW_LONE`, and populated for one reason only: a **VS16
#: sequence** the narrow-VS16 verdict gets wrong. The renderer is not uniform (see the module
#: docstring): ``☀️``, the calibration probe, paints in one cell, but ``🛩️`` (U+1F6E9 U+FE0F)
#: paints in two, so blanket-narrowing it would pull that row's border a column short.
#:
#: A **bare** codepoint does not belong here, and the attempt to put one here is worth recording
#: because the mistake is inviting. An emoji outside Emoji_Presentation — ``🛣`` U+1F6E3, ``🕸``
#: U+1F578 — has East-Asian width Neutral, so Rich and wcwidth both measure it one; without a
#: variation selector asking for emoji presentation the font draws it as a one-cell text glyph,
#: which is exactly what they said. Listing ``🛣`` to force it to two made the Trophy case's
#: Longest-distance heading reserve a cell the terminal never drew, pulling that row's right
#: border one column in (JP, 2026-08-09) — while ``🕸``, the same Unicode class one board down and
#: never listed, framed flush the whole time. **Both authorities already agreeing at one is not a
#: bug to correct**; when a row misaligns, check whether the glyph carries U+FE0F before assuming
#: the font overrode the measurement.
#:
#: Which VS16 sequence a terminal paints wide is still the font's business rather than the
#: codepoint's, and a probe reads the PTY and not the renderer, so this stays a curated set,
#: extended without a code change through ``MESHTERM_WIDE_EMOJI``. Keyed on the base codepoint,
#: because the trailing selector is skipped by both authorities either way.
_DEFAULT_WIDE_BASE = "\U0001f6e9"  # 🛩 airplane, drawn two-wide with its VS16 selector

#: Set once :func:`calibrate` has run so repeated calls are cheap no-ops.
_CALIBRATED = False

#: Whether the width authorities have been redirected through this module. Gates
#: :class:`ClusterTextControl`: handing prompt_toolkit a joined sequence as one character is
#: only right once something measures that character as one glyph, so the delivery and the
#: measurement are switched on together or not at all.
_CLUSTERS = False


def _is_regional_indicator(char: str) -> bool:
    """Whether ``char`` is a single Regional Indicator Symbol — a country flag's building block.

    The two-letter flags (``🇨🇦``, ``🇺🇸``) are a grapheme pair drawn from this block
    (U+1F1E6–U+1F1FF). wcwidth counts each indicator as two, so prompt_toolkit measures a flag
    as *four* cells, while Rich and the terminal draw the pair as one two-cell glyph. Narrowing
    every indicator to one makes any flag sum to two in both authorities, so a chat row with a
    flag frames flush. This is a whole category, not a per-country entry: the indicators are
    shared across flags (``🇨🇦`` and ``🇨🇳`` both start with ``C``), so they can only be
    handled as a block — listing one flag would half-fix its neighbours and skip the rest.
    """
    return len(char) == 1 and 0x1F1E6 <= ord(char) <= 0x1F1FF


def _joins(char: str) -> bool:
    """Whether a zero-width joiner in front of ``char`` really joins it to the glyph before.

    Every component an emoji ZWJ sequence joins is a pictograph — ``♂``, ``☠``, ``❤``, ``💻``,
    all at U+2000 and above — so that is the test. What it rules out is a **stranded** joiner.
    A MeshCore advert name is cut at a byte limit, and a name that ended on a joined emoji
    (``That's So Fetch 🏴‍☠️``) arrives cut just past the joiner, followed by whatever the row
    puts after the name: a space of lane padding, the next lane. Both width authorities used
    to fold that character into the flag regardless, so the padding measured nothing while the
    terminal still drew it, and every lane after the name started a column late.
    """
    return char >= " " and not char.isspace()


def _stranded_out(text: str) -> str:
    """Drop every joiner that joins nothing (see :func:`_joins`), leaving real sequences whole."""
    last = len(text) - 1
    return "".join(
        char
        for index, char in enumerate(text)
        if char != _ZWJ or (index < last and _joins(text[index + 1]))
    )


def _joined_out(text: str) -> str:
    """Drop every codepoint a ZWJ folds into the glyph before it, leaving the bases behind.

    An emoji ZWJ sequence is one grapheme and the terminal draws it in one glyph's worth of
    cells — its base's. ``"🤷‍♂️"`` is a shrug, not a shrug beside a male sign, so measuring it
    means measuring ``"🤷"`` and ignoring what the joiner attached. Removing the joined
    codepoints (and the joiners) leaves a string the ordinary per-codepoint rules — the wide
    set, the narrow allowlist, the flag category — measure correctly, so a cluster inherits
    whatever its base is worth and every existing lever still reaches it.

    A stranded joiner (see :func:`_joins`) goes on its own: the character after it was never
    part of the glyph, and keeps its cell. Variation selectors and skin tones are left in
    place — both authorities give them zero.
    """
    kept = []
    joined = False  # the previous codepoint was a joiner, so this one may belong to that glyph
    for char in text:
        if char == _ZWJ:
            joined = True
            continue
        if joined:
            joined = False
            if _joins(char):
                continue
        kept.append(char)
    return "".join(kept)


def clusters(text: str) -> Iterator[str]:
    """Split ``text`` into the glyphs the terminal actually draws, never inside one.

    The same run :func:`_join_clusters` gathers out of a laid-out line, read off a plain
    string instead: a base, whatever extends it (:data:`_EXTENDERS` — a variation selector, a
    skin tone), then any number of *joiner + component + extenders* groups, plus the
    Regional Indicator **pair** a country flag is drawn from. A joiner that joins nothing
    (:func:`_joins`) is a cluster of its own, because it is not part of the glyph before it.

    The unit a lane has to cut on: every correction in this module measures a *whole* glyph,
    so a cut through the middle of one leaves two pieces neither authority can measure — a
    lone Regional Indicator drawn as a letter, a skin tone with no base, or the stranded
    joiner that goes on to swallow the ellipsis the cut appended. See :func:`cut_cells`.
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
    ``"👨‍👩‍👧"`` after its first joiner leaves a stranded joiner at the end of the lane,
    which then folds the ellipsis the caller appends into the glyph before it
    (:func:`_joined_out`) — the ellipsis measures nothing, the terminal draws it anyway, and
    every lane after the name starts a column late. Cutting a flag in half leaves a lone
    Regional Indicator, which is a letter rather than half a flag.

    So the prefix is built out of whole :func:`clusters`, and a trailing joiner — stranded
    by the cut itself, or already stranded in the name — is dropped rather than left to
    swallow whatever comes next.

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
    reverses everything drawn after it. All three measure zero cells and none of them is a
    width bug that any allowlist here could fix — they are text that must not reach the
    terminal at all, so a lane folds them to the one character that is always safe.

    Folded: control characters, the format and bidi-control block, surrogates, and the line
    and paragraph separators. **Kept:** the zero-width joiner, which is format-class too and
    is the tell that holds an emoji sequence together, along with every mark and modifier a
    glyph is built from. Private-use codepoints are kept as well — that block is where a
    Nerd Font keeps the powerline glyphs the path line is drawn with.
    """
    return "".join(
        " " if char != _ZWJ and unicodedata.category(char) in _UNDRAWABLE else char for char in text
    )


def _narrow_lone_set() -> frozenset[str]:
    """The lone-codepoint emoji to measure as one cell (see :data:`_DEFAULT_NARROW_LONE`).

    ``MESHTERM_NARROW_EMOJI`` overrides the default outright: set it to the full list of
    glyphs you have confirmed this terminal draws narrow (e.g. ``MESHTERM_NARROW_EMOJI=👋🤙``),
    or to the empty string to turn lone-emoji narrowing off entirely. Only consulted once the
    renderer is judged to draw emoji narrow in the first place (see :func:`calibrate`), so on a
    terminal that draws emoji two cells wide the whole allowlist is inert and nothing regresses.
    """
    return frozenset(os.environ.get("MESHTERM_NARROW_EMOJI", _DEFAULT_NARROW_LONE))


def _wide_base_set() -> frozenset[str]:
    """The base codepoints to measure two cells wide (see :data:`_DEFAULT_WIDE_BASE`).

    ``MESHTERM_WIDE_EMOJI`` overrides the default outright: set it to the base glyphs you have
    confirmed this terminal draws two wide (e.g. ``MESHTERM_WIDE_EMOJI=🛩🛣``), or to the empty
    string to trust the narrow-VS16 verdict for every sequence. A trailing variation selector is
    dropped, so pasting the whole rendered glyph (``🛩️``) still keys on the base and never counts
    the zero-width selector as a wide cell itself. Only consulted on the width-1 path (see
    :func:`calibrate`); a terminal that already draws emoji two wide measures these right on its
    own, and nothing here would have narrowed them.
    """
    listed = frozenset(os.environ.get("MESHTERM_WIDE_EMOJI", _DEFAULT_WIDE_BASE))
    return listed - frozenset(_ZERO_WIDTH)


def calibrate(*, force_width: int | None = None) -> None:
    """Measure the terminal's emoji width once and patch Rich to match.

    Safe to call more than once (only the first call does work) and safe to call
    when there is no real terminal (it becomes a no-op). Call this after the
    terminal is available but *before* a full-screen renderer takes it over.

    Args:
        force_width: Skip measuring and act as if the terminal rendered the probe
            at this width. For tests; production code omits it.
    """
    global _CALIBRATED
    if _CALIBRATED:
        return
    _CALIBRATED = True

    width = force_width if force_width is not None else _decide_vs16_width()
    # width 1 -> the renderer draws emoji narrow: stop Rich promoting VS16 sequences and
    # narrow the curated lone-codepoint emoji this terminal also draws in one cell.
    # width 2 -> Rich's default already matches, so its table is left alone (narrowing here
    # would instead pull a correctly-wide emoji's border a column short) — but prompt_toolkit
    # still sums a ZWJ sequence's parts and a skin tone's cells at either width, and both
    # authorities let a stranded joiner swallow the character after it, which is wrong on
    # every terminal, so those rules are installed on their own.
    # unknown/None -> no terminal to be aligned with; touch nothing.
    if width == 1:
        _install_terminal_widths(_narrow_lone_set(), _wide_base_set())
    elif width == 2:
        _install_cluster_widths()


def _decide_vs16_width() -> int | None:
    """Decide how wide the renderer paints :data:`_PROBE`, in order of trust.

    Returns:
        ``1`` or ``2`` when we have a confident answer, else ``None`` (leave Rich
        untouched). See the module docstring for why a probe alone is not enough.
    """
    override = os.environ.get("MESHTERM_EMOJI_VS16")
    if override in ("1", "2"):
        return int(override)

    # Renderers that paint VS16 emoji in one cell while their PTY counts two, so a
    # cursor probe reads the PTY's (wrong) answer. Fingerprint them directly.
    if os.environ.get("TERM_PROGRAM") == "vscode" or os.environ.get("WT_SESSION"):
        return 1

    # Everywhere else, the terminal tracks and paints in the same layer: measure it.
    return _measure_probe_width()


def _measure_probe_width() -> int | None:
    """Return the column width the live terminal gives :data:`_PROBE`, or ``None``.

    ``None`` means the width could not be measured (not a terminal, the console
    call failed, or the escape reply timed out) — callers treat that as "don't
    touch Rich".
    """
    if not (sys.stdout and sys.stdout.isatty()):
        return None
    try:
        if os.name == "nt":
            return _win_probe_width(_PROBE)
        return _posix_probe_width(_PROBE)
    except Exception:  # noqa: BLE001 - any probe failure must fall back cleanly
        return None


# --- Windows: read the cursor cell column from the console API ----------------


def _win_probe_width(probe: str) -> int | None:
    """Measure ``probe`` width via the Windows console cursor position."""
    import ctypes
    from ctypes import wintypes

    class _COORD(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", wintypes.SHORT),
            ("Top", wintypes.SHORT),
            ("Right", wintypes.SHORT),
            ("Bottom", wintypes.SHORT),
        ]

    class _CSBI(ctypes.Structure):
        _fields_ = [
            ("dwSize", _COORD),
            ("dwCursorPosition", _COORD),
            ("wAttributes", wintypes.WORD),
            ("srWindow", _SMALL_RECT),
            ("dwMaximumWindowSize", _COORD),
        ]

    # A private handle: declaring these signatures on `ctypes.windll.kernel32` would
    # rewrite them for prompt_toolkit too, which calls the same function object with a
    # struct class of its own. See meshterm.core.win32dll.
    kernel32 = win32dll.kernel32()
    # Declare signatures explicitly. Without this ctypes assumes a 32-bit int
    # return, which truncates the 64-bit console HANDLE and makes every
    # subsequent call fail — silently reducing the probe to "no measurement".
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_CSBI),
    ]
    kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL

    STD_OUTPUT_HANDLE = wintypes.DWORD(-11).value
    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

    def cursor_x() -> int | None:
        info = _CSBI()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None
        return info.dwCursorPosition.X

    sys.stdout.write("\r")
    sys.stdout.flush()
    start = cursor_x()
    if start is None:
        return None
    sys.stdout.write(probe)
    sys.stdout.flush()
    end = cursor_x()
    # Erase the probe so it never flashes into the user's scrollback.
    sys.stdout.write("\r" + " " * (2 if end is None else max(0, end - start)) + "\r")
    sys.stdout.flush()
    if end is None:
        return None
    return end - start


# --- POSIX: ask the terminal with a Device Status Report ----------------------


def _posix_probe_width(probe: str, timeout: float = 0.3) -> int | None:
    """Measure ``probe`` width via a cursor-position report (``ESC[6n``)."""
    import re
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Column 1, print the probe, then ask where the cursor landed.
        sys.stdout.write("\r" + probe + "\x1b[6n")
        sys.stdout.flush()
        reply = ""
        while "R" not in reply:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                break
            reply += os.read(fd, 32).decode(errors="ignore")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    # Erase the probe from the line.
    sys.stdout.write("\r\x1b[K")
    sys.stdout.flush()

    match = re.search(r"\x1b\[\d+;(\d+)R", reply)
    if not match:
        return None
    # We started at column 1, so the reported column is 1 + width.
    return int(match.group(1)) - 1


# --- the patch ----------------------------------------------------------------


def _make_cell_len(narrow: frozenset[str], wide: frozenset[str]) -> Callable[[str, str], int]:
    """Build Rich's ``_cell_len`` replacement: the terminal's true width for every string.

    Corrections fold into the one measuring loop the whole render pipeline reads:

    * **VS16 promotion** — a variation selector (:data:`_ZERO_WIDTH`) is skipped, so a narrow
      base (``☀`` = 1) is measured on its own instead of promoted to two by a trailing
      ``U+FE0F``. This is the general emoji-presentation fix (see the module docstring).
    * **Wide VS16 exceptions** — a base codepoint in ``wide`` (``🛩`` and friends) that this
      terminal draws two cells despite the narrow-VS16 verdict is forced back to two, so its
      selector-skipped base does not collapse to one and smear the row.
    * **Curated lone emoji** — a single codepoint in ``narrow`` (``👋`` and friends) that Rich
      would call two but this terminal draws in one is counted as one, so a chat row carrying
      it pads to a flush right border instead of a column-short notch.
    * **Flag indicators** — a Regional Indicator (:func:`_is_regional_indicator`) counts as one,
      so a country flag sums to two (Rich already agrees here; this keeps the two loops in step).
    * **ZWJ sequences** — the codepoints a joiner folds in are dropped first
      (:func:`_joined_out`), so ``"🤷‍♂️"`` is measured as the one glyph it is. Rich's stock
      loop does this and the selector-skipping replacement above would otherwise lose it,
      counting the male sign as a cell of its own and pulling the row's border a column in.
      A *stranded* joiner (:func:`_joins`) folds in nothing, where the stock loop would have
      swallowed the padding space after a byte-cut name.

    Everything else keeps its ``get_character_cell_size`` value, so a lone emoji the terminal
    *does* draw two wide (a menu icon like ``📡``, never placed in ``narrow``) is left alone.
    """
    import rich.cells as cells

    get_size = cells.get_character_cell_size

    def _cell_len(text: str, unicode_version: str = "auto") -> int:
        total = 0
        for char in _joined_out(text) if _ZWJ in text else text:
            if char in _ZERO_WIDTH:
                continue
            if char in wide:
                total += 2
            elif char in narrow or _is_regional_indicator(char):
                total += 1
            else:
                total += get_size(char, unicode_version)
        return total

    return _cell_len


def _make_pt_cache(narrow: frozenset[str], wide: frozenset[str], *, flags: bool = True) -> object:
    """Build a prompt_toolkit char-width cache that measures every ``narrow`` glyph as one.

    prompt_toolkit is the authority that *places the panel's right border*: it lays the
    composed frame into its screen buffer stepping a cursor by ``get_cwidth`` (backed by
    ``_CHAR_SIZES_CACHE``), so unless it too measures ``👋`` as one, it positions the border a
    column past where the terminal draws the narrow glyph — the notch survives even with Rich
    corrected. The subclass returns one for a listed lone glyph (or a flag's Regional Indicator)
    and defers everything else to the stock ``wcwidth`` logic; because the base ``__missing__``
    sums per character through the cache, a whole chat line ``"Bob 👋 hi"`` inherits the one-cell
    ``👋`` for free, and a flag ``"🇨🇦"`` sums its two one-cell indicators to a flush two.

    A **ZWJ sequence** is measured as the single glyph it is, by summing only the bases the
    joiner left behind (:func:`_joined_out`) — so ``"🤷‍♂️"`` is two rather than three and
    ``"👨‍👩‍👧"`` two rather than six. It runs through this same cache, so a base in ``wide`` or
    ``narrow`` still sets the sequence's width. Delivering it is the other half of the job:
    prompt_toolkit lays out one codepoint at a time, so the sequence also has to reach it as a
    single fragment — see :class:`ClusterTextControl`.

    A ``wide`` base codepoint (``🛩``) is the reverse case and matters most here: prompt_toolkit's
    wcwidth already calls a VS16 base one on its own (it never promoted the pair), so without this
    the airplane's border lands a column short of where the terminal paints its two-cell glyph. The
    subclass forces such a base to two; the same per-character summation then gives ``"🛩️ hi"`` its
    correct width, the trailing selector staying zero.

    A **skin tone** (:data:`_MODIFIERS`) measures zero, as Rich already has it: wcwidth calls
    each modifier two, so ``👍🏽`` cost four cells here and the joined ``👨🏻‍💻`` four as well.
    At zero, prompt_toolkit's own layout folds the modifier into the cell before it, the way it
    folds a variation selector — and :class:`ClusterTextControl` carries it inside a joined run.

    Args:
        narrow: lone codepoints to measure as one cell.
        wide: base codepoints to measure as two.
        flags: whether to narrow Regional Indicators so a country flag sums to two. Off on the
            width-2 path, where the terminal's own flag rendering has not been confirmed and a
            renderer without flag glyphs draws the two indicators as four cells of its own.
    """
    import prompt_toolkit.utils as ptu

    base_cache_cls = type(ptu._CHAR_SIZES_CACHE)

    class _NarrowLoneCache(base_cache_cls):  # type: ignore[valid-type, misc]
        def __missing__(self, string: str) -> int:
            if _ZWJ in string:
                # One glyph, however many codepoints: measure the bases the joiner left
                # behind, through this same cache so every rule above still applies. Only
                # a short result is kept — a whole *line* holding a sequence is cheap to
                # recompute and would otherwise slip past the base class's long-string
                # rotation and grow the cache without bound.
                total = sum(self[char] for char in _joined_out(string))
                if len(string) <= self.LONG_STRING_MIN_LEN:
                    self[string] = total
                return total
            if string in _MODIFIERS:
                self[string] = 0
                return 0
            if string in wide:
                self[string] = 2
                return 2
            if string in narrow or (flags and _is_regional_indicator(string)):
                self[string] = 1
                return 1
            return super().__missing__(string)

    return _NarrowLoneCache()


def _install_terminal_widths(narrow: frozenset[str], wide: frozenset[str]) -> None:
    """Redirect both width authorities through the terminal-aligned measurement, in one shot.

    Rich's ``cell_len`` delegates to the module-level ``_cell_len``; replacing that (and
    clearing the memoised wrapper) steers every downstream caller — panels, tables, text —
    through :func:`_make_cell_len`. prompt_toolkit is patched too, because it is what places the
    border (see :func:`_make_pt_cache`): a lone emoji, a wide-VS16 exception, or a flag all need
    it, and even empty ``narrow``/``wide`` sets still leave the flag-indicator category to
    correct, so the cache is always swapped. Both run at :func:`calibrate` time, before a frame is
    drawn, so no stale measurement or ``Char`` is ever painted.

    Measuring a ZWJ sequence as one glyph is half of that job, so this is also what arms the
    other half — :class:`ClusterTextControl` only merges once something is measuring what it
    merges.
    """
    global _CLUSTERS
    import prompt_toolkit.utils as ptu
    import rich.cells as cells

    from .render import _ANSI_CACHE

    cells.cached_cell_len.cache_clear()
    cells._cell_len = _make_cell_len(narrow, wide)
    ptu._CHAR_SIZES_CACHE = _make_pt_cache(narrow, wide)
    _CLUSTERS = True
    # The measurement just changed under every renderable, so anything rasterized before
    # calibration (nothing, in the normal boot order — but never trust that) is stale.
    _ANSI_CACHE.clear()


def _install_cluster_widths() -> None:
    """Correct what is wrong at *either* emoji width: joined runs, skin tones, stranded joiners.

    A terminal that draws ``☀️`` in two cells needs none of the narrowing above — Rich's table
    already matches what it paints, and so does prompt_toolkit's for everything that stands on
    its own. A joined sequence is the exception: prompt_toolkit adds up its codepoints wherever
    it runs, so ``👨‍👩‍👧`` reserves six cells for a two-cell glyph on the widest terminal as
    surely as on the narrowest, and a skin tone costs it two more on any of them. So
    prompt_toolkit's cache is swapped, with both curated sets empty and the flag category left
    alone, since neither has been confirmed here.

    Rich's *table* is left as it is — it measures a real cluster and a skin tone right on its
    own — and only its loop is wrapped, for the one string it gets wrong: it skips whatever
    follows a joiner without asking what that is, so the padding after a byte-cut ``🏴‍``
    measured nothing (see :func:`_joins`). The wrapper takes the stranded joiners out before the
    stock loop sees the string; every other string reaches it untouched.
    """
    global _CLUSTERS
    import prompt_toolkit.utils as ptu
    import rich.cells as cells

    from .render import _ANSI_CACHE

    stock = cells._cell_len

    def _cell_len(text: str, unicode_version: str = "auto") -> int:
        return stock(_stranded_out(text) if _ZWJ in text else text, unicode_version)

    cells.cached_cell_len.cache_clear()
    cells._cell_len = _cell_len
    ptu._CHAR_SIZES_CACHE = _make_pt_cache(frozenset(), frozenset(), flags=False)
    _CLUSTERS = True
    _ANSI_CACHE.clear()


class _Cluster(str):
    """An emoji ZWJ sequence that must reach the screen buffer as **one** character.

    prompt_toolkit builds its screen a codepoint at a time — ``for c in text`` in
    ``Window._copy_body`` — and gives each one its own cell-occupying ``Char``. A joined
    sequence measured correctly at two cells is therefore still laid out as a two-cell shrug
    followed by a one-cell male sign, and the border lands three columns along from a glyph the
    terminal drew in two. Iterating a cluster yields the whole sequence instead of its parts, so
    that loop makes a single ``Char`` of it: one cell pair in prompt_toolkit's arithmetic, every
    codepoint written to the terminal back to back, and one composed glyph on the screen.

    It is a ``str`` subclass rather than a wrapper because it has to survive as ordinary text
    through everything else prompt_toolkit does with a fragment — joining, slicing, comparing.
    ``str`` methods return plain ``str``, so the marking is lost on the first ``split``; that is
    why :class:`ClusterTextControl` applies it to the finished lines and nothing earlier.
    """

    __slots__ = ()

    def __iter__(self):  # type: ignore[override]
        yield str(self)


def _join_clusters(line: list) -> list:
    """Merge each multi-glyph emoji in one screen line into a single :class:`_Cluster` fragment.

    The line arrives one codepoint per fragment (that is what
    :class:`~prompt_toolkit.formatted_text.ANSI` produces), so a glyph built from several is a run
    to be gathered, the same run :func:`clusters` reads off a plain string. Two shapes need it:

    * **A ZWJ sequence**: a base, its optional variation selector or skin tone
      (:data:`_EXTENDERS`), then any number of *joiner + component + optional extenders* groups.
      prompt_toolkit would otherwise give each component a cell of its own.
    * **A flag**: a pair of Regional Indicators. Each indicator takes a cell of its own in
      prompt_toolkit's arithmetic, and summing them was already made right; what merging adds is
      that the pair is *written* as one. The renderer's writes are what
      :class:`~meshterm.ui.tui.colsnap.PinnedOutput` pins, and a pin between the two halves of
      a flag is a cursor move between them, after which a terminal no longer joins them into
      one glyph at all.

    A run with neither is handed back untouched — including a lone VS16 pair or toned emoji,
    which prompt_toolkit already folds into the preceding cell on its own, and a joiner with no
    pictograph after it (see :func:`_joins`), which is not a sequence at all.

    The whole line is handed back unchanged when it holds no joiner and no indicator, which is
    almost every line; the scan is one pass per line and runs only when a control's content
    actually changes.
    """
    if not any(item[1] == _ZWJ or _is_regional_indicator(item[1]) for item in line):
        return line
    merged: list = []
    index = 0
    count = len(line)
    while index < count:
        end = index + 1
        while end < count and line[end][1] in _EXTENDERS:
            end += 1
        joined = False
        if (
            _is_regional_indicator(line[index][1])
            and end < count
            and _is_regional_indicator(line[end][1])
        ):
            joined = True
            end += 1
            while end < count and line[end][1] in _EXTENDERS:
                end += 1
        while end + 1 < count and line[end][1] == _ZWJ and _joins(line[end + 1][1]):
            joined = True
            end += 2  # the joiner and the codepoint it joins
            while end < count and line[end][1] in _EXTENDERS:
                end += 1
        if joined:
            text = "".join(item[1] for item in line[index:end])
            merged.append((line[index][0], _Cluster(text)))
            index = end
        else:
            merged.append(line[index])
            index += 1
    return merged


class ClusterTextControl(FormattedTextControl):
    """A :class:`~prompt_toolkit.layout.controls.FormattedTextControl` that keeps emoji whole.

    The control is the last place a screen's text is still arranged in lines and still ours to
    touch, which is exactly what merging a sequence needs: ``split_lines`` runs before this
    point and returns plain ``str`` parts, so a :class:`_Cluster` marked any earlier would not
    survive to be laid out. Everything downstream — the width lookup, the wrapping, the screen
    buffer — reads the merged lines.

    Merging is gated on :func:`calibrate` having run, because handing prompt_toolkit a sequence
    as one character is only right once its cache measures that character as one glyph. Off that
    path (a test, a piped run, a terminal we could not measure) this is its base class exactly.
    """

    def create_content(self, width: int, height: int | None):  # type: ignore[override]
        """The base class's content, with each line's ZWJ sequences joined into one glyph.

        The joining is wrapped around the returned object's line lookup and memoised
        behind it, because prompt_toolkit hands back the same content object paint after
        paint — walking every line of every frame would cost far more than it saves.
        """
        content = super().create_content(width, height)
        # The base class caches its ``UIContent`` per (fragments, width, cursor), so the same
        # object comes back paint after paint: wrap its line lookup once, and memoise the merge
        # behind it, rather than re-walking every line of every frame.
        if _CLUSTERS and not getattr(content, "_zwj_merged", False):
            content._zwj_merged = True  # type: ignore[attr-defined]
            source = content.get_line
            cache: dict[int, list] = {}

            def get_line(i: int, _source=source, _cache=cache) -> list:
                line = _cache.get(i)
                if line is None:
                    line = _cache[i] = _join_clusters(_source(i))
                return line

            content.get_line = get_line
        return content


__all__ = ["ClusterTextControl", "calibrate", "clusters", "cut_cells", "drawable"]
