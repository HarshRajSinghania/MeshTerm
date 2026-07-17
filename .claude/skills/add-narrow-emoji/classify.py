"""Diagnose one emoji for the MeshTerm terminal-aligned width machinery.

Given an emoji on the command line, report its codepoints and how the three width
authorities measure it (wcwidth, Rich, prompt_toolkit), then classify it and print the
exact action the `add-narrow-emoji` skill should take. Run from the repo root:

    d:\\vibe\\MeshTerm\\.venv\\Scripts\\python.exe .claude\\skills\\add-narrow-emoji\\classify.py 👋

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

from wcwidth import wcwidth  # noqa: E402

import rich.cells as rc  # noqa: E402
from prompt_toolkit.utils import get_cwidth  # noqa: E402

from meshterm.ui.tui.emoji_width import (  # noqa: E402
    _DEFAULT_NARROW_LONE,
    _is_regional_indicator,
    _make_cell_len,
    _make_pt_cache,
)

_ZWJ = "‍"
_VS16 = "️"


def _classify(emoji: str) -> tuple[str, str]:
    """Return ``(label, action)`` for ``emoji`` — what it is and what the skill should do."""
    cps = [ord(c) for c in emoji]
    if not cps:
        return "empty", "Nothing to classify — pass an emoji as the argument."
    if all(_is_regional_indicator(c) for c in emoji) and len(emoji) == 2:
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
            "Do not add it to the allowlist — the parts would be narrowed individually and "
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
            "lone narrow codepoint",
            "Already one cell everywhere — it never notches a border. No change needed.",
        )
    others = [c for c in emoji if c not in (_VS16, _ZWJ)]
    if len(others) == 1 and _VS16 in emoji:
        return (
            "VS16 emoji-presentation sequence",
            "ALREADY HANDLED (the variation selector is skipped so the base is measured alone). "
            "No allowlist entry needed.",
        )
    return (
        "multi-codepoint cluster (keycap / skin-tone / other)",
        "NOT a plain lone codepoint — the lone/flag mechanism does not cover it. Flag this to a "
        "maintainer rather than adding parts to the allowlist.",
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: classify.py <emoji>")
        return 2
    emoji = argv[1]
    narrow = frozenset(_DEFAULT_NARROW_LONE)
    label, action = _classify(emoji)

    print(f"emoji      : {emoji}")
    print("codepoints : " + " ".join(f"U+{ord(c):04X}" for c in emoji))
    print(f"grapheme   : {len(emoji)} codepoint(s)")
    print(f"wcwidth    : {sum(max(0, wcwidth(c)) for c in emoji)} (per char: "
          + ", ".join(str(wcwidth(c)) for c in emoji) + ")")
    print(f"Rich now   : {rc.cell_len(emoji)}")
    print(f"pt now     : {get_cwidth(emoji)}  <- the authority that places the panel border")
    print(f"Rich fixed : {_make_cell_len(narrow)(emoji)}")
    print(f"pt fixed   : {_make_pt_cache(narrow)[emoji]}")
    print()
    print(f"class      : {label}")
    print(f"action     : {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
