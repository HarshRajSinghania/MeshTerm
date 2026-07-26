"""Path-line widget tests: same words in both modes, three overflow shapes.

The widget's contract is exactness — plain mode must reproduce the app-wide arrow
presentation glyph for glyph, powerline mode must interlock chip fills through the
separator's foreground/background trick, and every overflow shape must respect its
cell budget — so these tests assert rendered strings and span styles, not vibes.
"""

from __future__ import annotations

import pytest

import meshterm.ui.pathline as pathline
from meshterm.ui.pathline import POWERLINE_SEP, PathHop, PathLine
from meshterm.ui.theme import node_style


def _styles(text) -> dict[str, str]:  # noqa: ANN001
    """Map each styled slice of a Text to its style string, for spot checks."""
    return {text.plain[s.start:s.end]: str(s.style) for s in text.spans}


def test_plain_mode_matches_the_app_wide_arrow_presentation() -> None:
    """Names in their key hue, us white, keyless muted, arrows muted — the
    ``path_text`` look, reproduced exactly."""
    line = PathLine(
        [
            PathHop("Alice", key="aa"),
            PathHop("77", key="77", lit_bytes=1),
            PathHop("you", you=True),
        ],
        mode="plain",
    )
    text = line.text()
    assert text.plain == "Alice → 77 → you"
    styles = _styles(text)
    assert styles["Alice"] == node_style("aa")
    assert styles["you"] == "you"
    assert styles["77"] == node_style("77")  # the lit hash prefix carries the hue


def test_plain_mode_annotation_dim_and_custom_separator() -> None:
    """The trace ``(3d)`` note rides muted; a dim hop (and the arrow into it) fades;
    a surface's own separator — the atlas trail's ``›`` — passes straight through."""
    line = PathLine(
        [PathHop("YUL", key="3d", annotation="3d"), PathHop("home", you=True, dim=True)],
        mode="plain",
    )
    text = line.text()
    assert text.plain == "YUL (3d) → home"
    styles = _styles(text)
    assert styles[" (3d)"] == "muted"  # the annotation note, space included
    assert styles["home"] == "faint"
    assert styles[" → "] == "faint"  # the arrow into a dim hop fades with it
    trail = PathLine(
        [PathHop("a"), PathHop("b")], mode="plain", separator=" › "
    ).text()
    assert trail.plain == "a › b"


def test_empty_path_reads_as_the_callers_word() -> None:
    """A hopless line is the muted empty note, in every shape."""
    assert PathLine([], mode="plain").text().plain == "direct"
    assert PathLine([], mode="powerline", empty="direct — no relays").text().plain \
        == "direct — no relays"
    assert PathLine([], mode="plain").wrapped(40)[0].plain == "direct"


def test_chips_interlock_fills_through_the_separator() -> None:
    """Each seam's triangle wears the previous chip's fill as foreground on the next
    chip's fill as background; the last points into the page (foreground only)."""
    alice_fill = node_style("aa").split()[-1]
    line = PathLine(
        [PathHop("Alice", key="aa"), PathHop("you", you=True)], mode="powerline"
    )
    text = line.text()
    assert text.plain == f" Alice {POWERLINE_SEP} you {POWERLINE_SEP}"
    seam_styles = [str(s.style) for s in text.spans
                   if text.plain[s.start:s.end] == POWERLINE_SEP]
    assert seam_styles == [f"{alice_fill} on #ffffff", "#ffffff"]


def test_chips_keep_the_same_words_and_honour_style_overrides() -> None:
    """Chips change colours and separators, never the words; an explicit style
    override (hex or theme name) becomes the chip fill."""
    hops = [PathHop("YUL", key="3d", annotation="3d"), PathHop("you", you=True)]
    plain = PathLine(hops, mode="plain").text().plain
    chips = PathLine(hops, mode="powerline").text().plain
    assert plain.replace(" → ", " ") == chips.replace(POWERLINE_SEP, "").replace("  ", " ").strip()
    themed = PathLine([PathHop("X", style="brand")], mode="powerline").text()
    assert str(themed.spans[-1].style) == "#5eead4"  # the closing edge, brand-filled
    hexed = PathLine([PathHop("X", style="bold #123456")], mode="powerline").text()
    assert str(hexed.spans[-1].style) == "#123456"


def test_auto_mode_follows_the_terminal_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` renders chips exactly when the terminal can draw them."""
    hops = [PathHop("a"), PathHop("b")]
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: True)
    assert POWERLINE_SEP in PathLine(hops).text().plain
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: False)
    assert PathLine(hops).text().plain == "a → b"


def test_cursor_arrow_forces_plain_and_reverse_videos_the_seam() -> None:
    """The composer's insertion cursor needs a seam to sit in, so a cursor render
    is always arrows — even in powerline mode — with the cursor arrow selected."""
    hops = [PathHop("a"), PathHop("b")]
    text = PathLine(hops, mode="powerline").text(cursor_arrow=0)
    assert POWERLINE_SEP not in text.plain
    assert text.plain == "a → b"
    assert _styles(text)["→"] == "selected"


def test_ellipsized_elides_the_middle_and_keeps_both_endpoints() -> None:
    """A route reads origin and destination first, so the fit drops middle hops
    behind ``⋯`` — never the tail a right-edge truncation would amputate."""
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF")]
    line = PathLine(hops, mode="plain")
    assert line.ellipsized(200).plain == line.text().plain  # fits → untouched
    fitted = line.ellipsized(25)
    assert fitted.cell_len <= 25
    assert fitted.plain.startswith("AAAA")
    assert fitted.plain.endswith("FFFF")
    assert "⋯" in fitted.plain


def test_ellipsized_last_resort_truncates_a_single_giant_hop() -> None:
    """When even ``⋯ → last`` overflows, the classic ellipsis truncation steps in."""
    line = PathLine([PathHop("AAAAAAAAAA"), PathHop("BBBBBBBBBB")], mode="plain")
    fitted = line.ellipsized(8)
    assert fitted.cell_len <= 8
    assert fitted.plain.endswith("…")


def test_wrapped_breaks_at_hops_under_a_hanging_indent() -> None:
    """Plain lines that continue end with the ``→`` cue; continuations hang at the
    indent; every line respects the full width."""
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD")]
    lines = PathLine(hops, mode="plain").wrapped(16, indent=2)
    assert [line.plain for line in lines] == ["AAAA → BBBB →", "  CCCC → DDDD"]
    assert all(line.cell_len <= 16 for line in lines)


def test_wrapped_chip_lines_each_close_their_pointed_edge() -> None:
    """Chip wrapping never splits a chip; every line ends on the pointed edge."""
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD")]
    lines = PathLine(hops, mode="powerline").wrapped(16, indent=2)
    assert len(lines) == 2
    assert lines[1].plain.startswith("  ")
    assert all(line.plain.endswith(POWERLINE_SEP) for line in lines)
    assert all(line.cell_len <= 16 for line in lines)
