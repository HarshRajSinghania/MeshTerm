r"""Diagnose one emoji for the MeshTerm terminal-aligned width machinery.

Given an emoji on the command line, report its codepoints and how the three width
authorities measure it (wcwidth, Rich, prompt_toolkit), then classify it and print the
exact action the `add-narrow-emoji` skill should take. Run from the repo root:

    $DEV_VENV_PYTHON .claude/skills/add-narrow-emoji/classify.py 👋

(``$DEV_VENV_PYTHON`` is the interpreter named in ``.dev.env``; ``.dev.env.example`` shows
the shape.)

The point is to remove the guesswork the skill's judgement call depends on: a lone wide
codepoint gets an allowlist entry, a flag is already handled by category, and a ZWJ /
keycap / modifier cluster is *not* covered and must not be shoehorned in.
"""

from __future__ import annotations

import pathlib
import sys

# The chat border fix only ever engages on a terminal that draws emoji narrow; measure
# there or trust the maintainer's eyes. This script just classifies — it never patches.
sys.stdout.reconfigure(encoding="utf-8")  # print emoji safely on a cp1252 console
# Run from anywhere: put the repo root (four levels up) on the path so `meshterm` imports.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

import rich.cells as rc  # noqa: E402
from prompt_toolkit.utils import get_cwidth  # noqa: E402
from wcwidth import wcwidth  # noqa: E402

from meshterm.ui.tui.emoji_width import (  # noqa: E402
    _DEFAULT_NARROW_LONE,
    _DEFAULT_WIDE_BASE,
    _is_regional_indicator,
    _make_cell_len,
    _make_pt_cache,
)

_ZWJ = "‍"
_VS16 = "️"

_NARROW = frozenset(_DEFAULT_NARROW_LONE)
_WIDE = frozenset(_DEFAULT_WIDE_BASE) - frozenset((_ZWJ, _VS16))


def _base(emoji: str) -> str:
    """The emoji's base codepoint — what the wide set is keyed on (selectors dropped)."""
    others = [c for c in emoji if c not in (_VS16, _ZWJ)]
    return others[0] if len(others) == 1 else ""


def _classify(emoji: str) -> tuple[str, str]:
    """Return ``(label, action)`` for ``emoji`` — what it is and what the skill should do."""
    if not emoji:
        return "empty", "Nothing to classify — pass an emoji as the argument."

    # Already curated? Say so first — it is the commonest answer to "please add this one",
    # and every branch below would otherwise re-derive advice for a decision already made.
    base = _base(emoji)
    if base and base in _WIDE:
        return (
            "lone codepoint drawn WIDE — already listed",
            "ALREADY IN _DEFAULT_WIDE_BASE (both authorities measure it 2, bare and with its "
            "VS16 selector). Nothing to add. If a row still misaligns, the culprit is another "
            "glyph on it — classify that one instead.",
        )
    if len(emoji) == 1 and emoji in _NARROW:
        return (
            "lone codepoint drawn NARROW — already listed",
            "ALREADY IN _DEFAULT_NARROW_LONE (both authorities measure it 1). Nothing to add.",
        )

    if len(emoji) == 2 and all(_is_regional_indicator(c) for c in emoji):
        return (
            "flag (Regional Indicator pair)",
            "ALREADY HANDLED as a category — no allowlist entry. Verify pt width is 2 below; "
            "if so there is nothing to add. Never list a single flag (it half-fixes its "
            "neighbours that share an indicator).",
        )
    if _ZWJ in emoji:
        return (
            "ZWJ sequence (multi-glyph cluster)",
            "NOT covered by the lone/flag mechanism (a family, a profession, a flag-with-tag). "
            "Do not add it to either set — the parts would be resized individually and "
            "misalign. Flag this to a maintainer for bespoke handling.",
        )
    if len(emoji) == 1:
        if wcwidth(emoji) == 2:
            return (
                "lone wide codepoint",
                "IF your terminal draws it in ONE cell, add it to _DEFAULT_NARROW_LONE in "
                "meshterm/ui/tui/emoji_width.py (or MESHTERM_NARROW_EMOJI for a session test). "
                "If your terminal draws it TWO wide, leave it — narrowing would break its border.",
            )
        return (
            "lone narrow codepoint (bare, text presentation)",
            "NO CHANGE — and resist the pull to 'fix' it. Both authorities measure it one "
            "because it sits outside Emoji_Presentation, and with no VS16 asking for emoji "
            "presentation the font draws a one-cell text glyph: they are right. Forcing it "
            "to two reserves a cell the terminal never draws and pulls the row's border a "
            "column IN — the mistake U+1F6E3 🛣 cost the Trophy case. If this row really is "
            "misaligned, another glyph on it is the culprit; classify that one.",
        )
    if base and _VS16 in emoji:
        return (
            "VS16 emoji-presentation sequence",
            "HANDLED BY DEFAULT (the variation selector is skipped, so the base is measured "
            "alone and the narrow-VS16 verdict applies). If your terminal draws THIS one two "
            "cells wide anyway — the renderer is not uniform — add its BASE codepoint to "
            "_DEFAULT_WIDE_BASE (or MESHTERM_WIDE_EMOJI), as U+1F6E9 🛩️ needed.",
        )
    return (
        "multi-codepoint cluster (keycap / skin-tone / other)",
        "NOT a plain lone codepoint — the lone/flag mechanism does not cover it. Flag this to a "
        "maintainer rather than adding parts to either set.",
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: classify.py <emoji>")
        return 2
    emoji = argv[1]
    label, action = _classify(emoji)

    print(f"emoji      : {emoji}")
    print("codepoints : " + " ".join(f"U+{ord(c):04X}" for c in emoji))
    print(f"grapheme   : {len(emoji)} codepoint(s)")
    print(f"wcwidth    : {sum(max(0, wcwidth(c)) for c in emoji)} (per char: "
          + ", ".join(str(wcwidth(c)) for c in emoji) + ")")
    print(f"Rich now   : {rc.cell_len(emoji)}")
    print(f"pt now     : {get_cwidth(emoji)}  <- the authority that places the panel border")
    # "fixed" = what the width-1 calibration path already produces today, both sets applied.
    print(f"Rich fixed : {_make_cell_len(_NARROW, _WIDE)(emoji)}")
    print(f"pt fixed   : {_make_pt_cache(_NARROW, _WIDE)[emoji]}")
    print()
    print(f"class      : {label}")
    print(f"action     : {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
