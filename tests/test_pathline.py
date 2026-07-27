"""Path-line widget tests: same words in both modes, three overflow shapes.

The widget's contract is exactness — plain mode must reproduce the app-wide arrow
presentation glyph for glyph, powerline mode must interlock chip fills through the
separator's foreground/background trick, and every overflow shape must respect its
cell budget — so these tests assert rendered strings and span styles, not vibes.
"""

from __future__ import annotations

import random

import pytest

import meshterm.ui.pathline as pathline
from meshterm.ui.pathline import (
    POWERLINE_CAP, POWERLINE_SEP, PathHop, PathLine, _style_hex, path_line,
)
from meshterm.ui.theme import node_style
from meshterm.ui.widgets import path_text


def _styles(text) -> dict[str, str]:  # noqa: ANN001
    """Map each styled slice of a Text to its style string, for spot checks."""
    return {text.plain[s.start:s.end]: str(s.style) for s in text.spans}


def _char_styles(text) -> list[tuple[str, str]]:  # noqa: ANN001
    """Every character paired with its effective span style — exact-parity checks.

    Span *boundaries* may legally differ between two builders (one appends a hash in
    two pieces, the other in one); what must agree is the style each character lands
    under, so the comparison is per cell, not per span.
    """
    styles = [""] * len(text.plain)
    for span in text.spans:
        for i in range(span.start, span.end):
            styles[i] = str(span.style)
    return list(zip(text.plain, styles))


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


def test_wrapped_evens_the_lines_instead_of_widowing_the_tail() -> None:
    """A hop that misses the first line by a cell doesn't get stranded alone below it.

    Greedy packing would cram ``us → a → b →`` and widow ``us``; the fold takes the
    same number of lines either way, so it spreads the hops across them instead.
    """
    hops = [PathHop(label) for label in ("us", "alpha", "bravo", "us")]
    lines = PathLine(hops, mode="plain").wrapped(20, indent=0)
    assert [line.plain for line in lines] == ["us → alpha →", "bravo → us"]


def test_wrapped_folds_a_faded_return_leg_at_its_turn() -> None:
    """A boomerang breaks where it turns — composed leg above, its echo below.

    The dimmed hops are the mirrored return, so folding there gives the wrapped route
    a shape instead of an arbitrary mid-leg seam. It is only taken when free: here the
    turn fold costs no more lines than the greedy break would.
    """
    out = [PathHop(n) for n in ("me", "north-relay", "east-relay", "dest")]
    back = [PathHop(n, dim=True) for n in ("east-relay", "north-relay", "me")]
    lines = PathLine(out + back, mode="plain").wrapped(40, indent=0)
    assert [line.plain for line in lines] == [
        "me → north-relay → east-relay → dest →",  # out, ending on the target
        "east-relay → north-relay → me",  # and the mirror it comes home by
    ]


def test_wrapped_folds_a_walked_boomerang_at_its_turn_too() -> None:
    """A trace that answered dims nothing, but its route is still its own mirror.

    So the turn is found from the hop sequence itself, and the walk folds exactly
    where the plan that armed it folded — one route, one shape.
    """
    walked = [PathHop(n) for n in ("me", "north-relay", "east-relay", "dest")]
    walked += [PathHop(n) for n in ("east-relay", "north-relay", "me")]
    lines = PathLine(walked, mode="plain").wrapped(46, indent=0)
    assert [line.plain for line in lines] == [
        "me → north-relay → east-relay → dest →",
        "east-relay → north-relay → me",
    ]


def test_wrapped_never_folds_before_a_lone_faded_landing() -> None:
    """A path whose only dim hop is the automatic landing home keeps it on a real line.

    Path-mode walks dim just that last ``us``; folding at "the fade" there would widow
    it, so the seam needs two hops on each side to count as a turn.
    """
    hops = [PathHop(label) for label in ("us", "alpha", "bravo", "charlie")]
    hops.append(PathHop("us", dim=True))
    lines = PathLine(hops, mode="plain").wrapped(26, indent=0)
    assert len(lines) == 2
    assert lines[-1].plain != "us"
    assert lines[-1].plain.endswith("→ us")


def test_wrapped_lines_always_fit_their_width() -> None:
    """No mix of hops, mode, width, or indent ever produces a line past the budget.

    Lines are fitted arithmetically from per-hop measurements rather than by rendering
    every candidate group, so this sweeps that model against what actually gets drawn —
    over-wide lone hops (truncated, cue included) and chip lines (which carry a closing
    edge no arrow line has) are where the two would drift apart.
    """
    rng = random.Random(7)
    for _ in range(200):
        hops = [
            PathHop(
                "N" * rng.randint(1, 16),
                key=f"{i:02x}aa",
                annotation=f"{i:02x}" if rng.random() < 0.5 else None,
                dim=rng.random() < 0.3,
                you=rng.random() < 0.2,
                lit_bytes=rng.choice((0, 1, 2)),
            )
            for i in range(rng.randint(1, 9))
        ]
        for mode in ("plain", "powerline"):
            for width, indent in ((20, 0), (34, 2), (47, 7), (72, 16)):
                lines = PathLine(hops, mode=mode).wrapped(width, indent=indent)
                assert all(line.cell_len <= width for line in lines)


def test_wrapped_continuation_cue_follows_the_separator() -> None:
    """A comma-joined line (the trace screen's wire spec) continues on a comma, not an
    arrow — the cue is the separator's own mark, so the spec stays verbatim."""
    hops = [PathHop(label) for label in ("3d63ab99", "7f21cd01", "27aa1122")]
    lines = PathLine(hops, mode="plain", separator=",").wrapped(20, indent=2)
    assert lines[0].plain.endswith(",")
    assert "→" not in "".join(line.plain for line in lines)
    assert "".join(line.plain.strip() for line in lines) == "3d63ab99,7f21cd01,27aa1122"


def test_path_line_factory_matches_path_text_character_for_character() -> None:
    """The migration bridge: the trace flavour — device endpoints with annotated
    hashes, a named hop, a prefix-lit unnamed hop, a dimmed tail — renders through
    ``path_line`` exactly as ``path_text`` renders it, character and style alike."""
    names = {"aa11bb": "Alice", "3d63ab": "YUL"}
    hops = [None, "aa11bb", "77ccddee", "3d63ab", None]
    kwargs = dict(
        prefix_bytes=2, self_name="Me", show_hash=True, hash_bytes=3,
        device_hash="A1B2C3D4", dim_from=3,
    )
    old = path_text(hops, lambda h: names.get(h, h), **kwargs)
    new = path_line(hops, lambda h: names.get(h, h), mode="plain", **kwargs).text()
    assert _char_styles(new) == _char_styles(old)


def test_path_line_factory_matches_path_text_hash_as_name_flavour() -> None:
    """The message-paths flavour: an unnamed hop standing as its own muted identity
    hash, annotated with its addressed byte — same parity guarantee."""
    names = {"3d63abcdef00": "YUL"}
    hops = ["3d63abcdef00", "e839f2aabb11"]
    kwargs = dict(prefix_bytes=3, show_hash=True, hash_bytes=1, hash_as_name=True)
    old = path_text(hops, lambda h: names.get(h, h), **kwargs)
    new = path_line(hops, lambda h: names.get(h, h), mode="plain", **kwargs).text()
    assert _char_styles(new) == _char_styles(old)
    assert path_line([], lambda h: h).text().plain == path_text([], lambda h: h).plain


def test_wrapped_carries_the_cursor_even_onto_a_line_break() -> None:
    """A cursor render is plain even for a powerline line; a cursor sitting mid-group
    reverse-videos its arrow, and one sitting exactly on the break rides that line's
    trailing cue — the insertion point is always visible."""
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD")]
    line = PathLine(hops, mode="powerline")
    mid = line.wrapped(16, indent=2, cursor_arrow=0)
    assert [text.plain for text in mid] == ["AAAA → BBBB →", "  CCCC → DDDD"]
    assert any(str(s.style) == "selected" for s in mid[0].spans)
    assert all(POWERLINE_SEP not in text.plain for text in mid)
    on_break = line.wrapped(16, indent=2, cursor_arrow=1)  # the seam that broke
    assert str(on_break[0].spans[-1].style) == "selected"
    assert not any(str(s.style) == "selected" for s in on_break[1].spans)


def test_wrapped_chip_lines_close_male_and_open_female() -> None:
    """Chip wrapping never splits a chip; every line ends on the pointed edge, and
    every line *but the first* opens on its mirror — the continuation's own mark, so
    only the path's very first chip shows a square left edge."""
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD")]
    lines = PathLine(hops, mode="powerline").wrapped(18, indent=2)
    assert len(lines) == 2
    assert lines[0].plain.startswith(" AAAA")  # the square edge: this is the start
    assert lines[1].plain.startswith("  " + POWERLINE_CAP)  # …and this is a carry-on
    assert all(line.plain.endswith(POWERLINE_SEP) for line in lines)
    assert all(line.cell_len <= 18 for line in lines)


def test_bare_hops_draw_as_their_seam_alone() -> None:
    """An empty label spends no cells: arrow mode drops the padding it would have sat
    in, chip mode gives it no chip at all — the arrow into the page *is* the hop."""
    hops = [PathHop("", you=True), PathHop("Alice", key="aa"), PathHop("", you=True)]
    assert PathLine(hops, mode="plain").text().plain == "→ Alice →"

    chips = PathLine(hops, mode="powerline").text()
    assert chips.plain == POWERLINE_SEP + " Alice " + POWERLINE_SEP
    styles = [str(span.style) for span in chips.spans]
    # The leading seam is us pointing into Alice's field; the trailing one is Alice's
    # own point into the page — the closing edge a named tail would have added.
    assert styles[0] == f"#ffffff on {_style_hex(node_style('aa'))}"
    assert styles[-1] == _style_hex(node_style("aa"))


def test_bare_self_endpoints_survive_wrapping() -> None:
    """``bare_self`` strips our name and hash from a route's ends; a line that wraps
    still opens on the female point, and a widowed bare end still draws one."""
    hops = [None, *(f"{i:02x}aa" for i in range(6)), None]
    line = path_line(hops, prefix_bytes=2, self_name="Me", show_hash=True,
                     device_hash="a1b2", bare_self=True, mode="plain")
    assert line.text().plain.startswith("→ 00aa")
    assert line.text().plain.endswith("05aa →")
    assert "Me" not in line.text().plain

    lines = path_line(hops, prefix_bytes=2, self_name="Me", bare_self=True,
                      mode="powerline").wrapped(28, indent=2)
    assert len(lines) > 1
    assert all(line.plain.startswith("  " + POWERLINE_CAP) for line in lines[1:])
    assert all(line.cell_len <= 28 for line in lines)
