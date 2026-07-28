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
    CURSOR_GLYPH, ELIDE_HEAD, POWERLINE_ROUND_CLOSE, POWERLINE_ROUND_OPEN, POWERLINE_SEP,
    SELF_GLYPH, WRAP_OFFSET, PathHop, PathLine, _style_hex, path_line,
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


def test_chips_are_separated_by_a_gap_not_an_interlock() -> None:
    """A seam is two points, not one interlock: the previous chip tapers out with no
    background of its own (the page shows through the wedge), and the next chip's left
    edge notches the same point back inward out of its own fill. The closing edge keeps
    its single taper — nothing follows it to notch."""
    alice_fill = node_style("aa").split()[-1]
    line = PathLine(
        [PathHop("Alice", key="aa"), PathHop("you", you=True)], mode="powerline"
    )
    text = line.text()
    assert text.plain == f" Alice {POWERLINE_SEP * 2} you {POWERLINE_SEP}"
    seam_styles = [str(s.style) for s in text.spans
                   if text.plain[s.start:s.end] == POWERLINE_SEP]
    # taper out of Alice, notch into you (reverse = the page's own colour), closing edge
    assert seam_styles == [alice_fill, "#ffffff reverse", "#ffffff"]
    assert all(" on " not in style for style in seam_styles)  # never an interlock


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


def test_the_cursor_is_a_hop_of_its_own_in_either_mode() -> None:
    """The composer's insertion point is a slot *in* the route, not a gap between two
    hops: it renders as a hop, so chips stay chips and arrows stay arrows."""
    hops = [PathHop("a"), PathHop(CURSOR_GLYPH, cursor=True), PathHop("b")]
    plain = PathLine(hops, mode="plain").text()
    assert plain.plain == f"a → {CURSOR_GLYPH} → b"
    assert _styles(plain)[CURSOR_GLYPH] == "selected"  # the reverse-video block

    chips = PathLine(hops, mode="powerline").text()
    assert chips.plain == f" a {POWERLINE_SEP * 2} {CURSOR_GLYPH} {POWERLINE_SEP * 2} b {POWERLINE_SEP}"
    slot = next(s for s in chips.spans if chips.plain[s.start : s.end] == CURSOR_GLYPH)
    assert str(slot.style).endswith(f"on {_style_hex('brand')}")  # the brand accent fill


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


def test_ellipsized_eats_the_head_when_told_to() -> None:
    """A breadcrumb's news is where the walk *is*, so ``ELIDE_HEAD`` spares the origin
    nothing: the fit keeps as much of the tail as the width holds, behind a ``⋯``."""
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF")]
    line = PathLine(hops, mode="plain")
    fits = line.ellipsized(200, elide=ELIDE_HEAD).plain
    assert fits == line.text().plain  # fits → untouched, whichever side would give
    fitted = line.ellipsized(25, elide=ELIDE_HEAD)
    assert fitted.cell_len <= 25
    assert fitted.plain.startswith("⋯")  # the origin goes too, unlike the tail-side fit
    assert fitted.plain.endswith("FFFF")
    assert "AAAA" not in fitted.plain


def test_ellipsized_last_resort_truncates_a_single_giant_hop() -> None:
    """When even ``⋯ → last`` overflows, the classic ellipsis truncation steps in."""
    line = PathLine([PathHop("AAAAAAAAAA"), PathHop("BBBBBBBBBB")], mode="plain")
    fitted = line.ellipsized(8)
    assert fitted.cell_len <= 8
    assert fitted.plain.endswith("…")


def test_wrapped_breaks_at_hops_under_a_hanging_indent() -> None:
    """Plain lines that continue end with the ``→`` cue; continuations hang at the
    indent, stepped in by :data:`WRAP_OFFSET`; every line respects the full width."""
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD")]
    lines = PathLine(hops, mode="plain").wrapped(16, indent=2)
    assert [line.plain for line in lines] == ["AAAA → BBBB →", "    CCCC → DDDD"]
    assert lines[1].plain.startswith(" " * (2 + WRAP_OFFSET))
    assert all(line.cell_len <= 16 for line in lines)


def test_wrapped_evens_the_lines_instead_of_widowing_the_tail() -> None:
    """A hop that misses the first line by a cell doesn't get stranded alone below it.

    Greedy packing would cram ``us → a → b →`` and widow ``us``; the fold takes the
    same number of lines either way, so it spreads the hops across them instead.
    """
    hops = [PathHop(label) for label in ("us", "alpha", "bravo", "us")]
    lines = PathLine(hops, mode="plain").wrapped(20, indent=0)
    assert [line.plain for line in lines] == ["us → alpha →", "  bravo → us"]


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
        "  east-relay → north-relay → me",  # and the mirror it comes home by
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
        "  east-relay → north-relay → me",
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


def test_path_line_factory_splices_the_cursor_slot_at_a_hop_index() -> None:
    """``cursor`` names the position a chosen hop would take, counted over the rendered
    hops and applied after them — so the slot never shifts what its neighbours show."""
    hops = [None, "aa11bb", "3d63ab", None]

    def built(cursor) -> str:  # noqa: ANN001
        line = path_line(hops, self_name="Me", bare_self=True, mode="plain", cursor=cursor)
        return line.text().plain

    assert built(None) == f"{SELF_GLYPH} → aa11bb → 3d63ab → {SELF_GLYPH}"
    assert built(0) == f"{CURSOR_GLYPH} → {SELF_GLYPH} → aa11bb → 3d63ab → {SELF_GLYPH}"
    assert built(2) == f"{SELF_GLYPH} → aa11bb → {CURSOR_GLYPH} → 3d63ab → {SELF_GLYPH}"
    assert built(99).endswith(f"{SELF_GLYPH} → {CURSOR_GLYPH}")  # clamped to the tail


def test_wrapped_carries_the_cursor_like_any_other_hop() -> None:
    """A folded route can't strand its insertion point: the slot is a hop, so it lands
    on a line of its own accord — wherever it stands, and whatever the mode."""
    labels = ("AAAA", "BBBB", "CCCC", "DDDD")
    for at in range(len(labels) + 1):
        hops = [PathHop(label) for label in labels]
        hops.insert(at, PathHop(CURSOR_GLYPH, cursor=True))
        lines = PathLine(hops, mode="plain").wrapped(16, indent=2)
        assert sum(text.plain.count(CURSOR_GLYPH) for text in lines) == 1
        assert all(text.cell_len <= 16 for text in lines)
        carried = next(t for t in lines if CURSOR_GLYPH in t.plain)
        assert any(str(s.style) == "selected" for s in carried.spans)


def test_wrapped_chip_lines_open_on_the_break_they_continue() -> None:
    """Chip wrapping never splits a chip; every line ends on the pointed edge, and
    every line *but the first* opens on the break's other half — the point notched out
    of its own fill in reverse video, so the page shows through and not a trace of the
    line above bleeds down. The point always faces the way the path flows."""
    hops = [PathHop(label, key=key) for label, key in
            (("AAAA", "aa"), ("BBBB", "77"), ("CCCC", "3d"), ("DDDD", "f2"))]
    lines = PathLine(hops, mode="powerline").wrapped(20, indent=2)
    assert len(lines) == 2
    assert lines[0].plain.startswith(" AAAA")  # the square edge: this is the start
    step = 2 + WRAP_OFFSET
    assert lines[1].plain[:step].isspace() and lines[1].plain[step] == POWERLINE_SEP
    carried = next(s for s in lines[1].spans if s.start == step)
    assert str(carried.style) == f"{_style_hex(node_style('3d'))} reverse"
    assert all(line.plain.endswith(POWERLINE_SEP) for line in lines)
    assert all(line.cell_len <= 20 for line in lines)


def test_rounded_caps_finish_a_path_only_where_the_font_has_them(monkeypatch) -> None:  # noqa: ANN001
    """A full Nerd Font rounds the path's two *outer* ends into a lozenge; a core-only
    terminal squares the opening and points the close, never drawing tofu. Interior
    breaks stay angled either way — a rounded end would read as the path stopping."""
    hops = [PathHop(label, key=key) for label, key in
            (("AAAA", "aa"), ("BBBB", "77"), ("CCCC", "3d"), ("DDDD", "f2"))]
    line = PathLine(hops, mode="powerline")

    monkeypatch.setattr(pathline, "powerline_full", lambda: False)
    assert line.text().plain.startswith(" AAAA")
    assert line.text().plain.endswith(POWERLINE_SEP)

    monkeypatch.setattr(pathline, "powerline_full", lambda: True)
    assert line.text().plain.startswith(POWERLINE_ROUND_OPEN + " AAAA")
    assert line.text().plain.endswith(POWERLINE_ROUND_CLOSE)
    wrapped = line.wrapped(20, indent=2)
    assert len(wrapped) == 2
    assert wrapped[0].plain.startswith(POWERLINE_ROUND_OPEN)  # the path opens here…
    assert wrapped[0].plain.endswith(POWERLINE_SEP)  # …but does not end here
    assert wrapped[1].plain[2 + WRAP_OFFSET] == POWERLINE_SEP  # picked up mid-path…
    assert wrapped[1].plain.endswith(POWERLINE_ROUND_CLOSE)  # …and closed off
    assert all(text.cell_len <= 20 for text in wrapped)


def test_every_seam_opens_onto_the_page_whatever_the_fills() -> None:
    """One gap rule, no exceptions: a seam never takes a background, so two chips of
    one colour — a hue collision, a run of dimmed hops — can't fuse into a single block,
    and two chips of different colours are still visibly two chips."""
    hue = _style_hex(node_style("aa"))
    same = PathLine([PathHop("A", key="aa"), PathHop("B", key="aa")], mode="powerline")
    seam = next(s for s in same.text().spans if same.text().plain[s.start] == POWERLINE_SEP)
    assert str(seam.style) == hue  # foreground only: the page shows through the taper

    apart = PathLine([PathHop("A", key="aa"), PathHop("B", key="77")], mode="powerline")
    text = apart.text()
    seams = [str(s.style) for s in text.spans if text.plain[s.start] == POWERLINE_SEP]
    assert seams[0] == hue  # the fill it tapers out of, and nothing behind it

    dimmed = PathLine([PathHop("A", dim=True), PathHop("B", dim=True)], mode="powerline")
    text = dimmed.text()
    gaps = [str(s.style) for s in text.spans if text.plain[s.start] == POWERLINE_SEP]
    assert " on " not in gaps[0]  # a dimmed return leg reads as hops, not one bar


def test_bare_self_stands_us_on_a_star_and_fades_both_ends() -> None:
    """``bare_self`` replaces our name and hash with the app-wide ★ at both ends, and
    both fade: setting out from us and landing back on us are fixtures of the route,
    not choices, so they wear the same automatic grey as a mirrored return leg —
    and, in chips, the same dark slate rather than the loud you white."""
    hops = [None, *(f"{i:02x}aa" for i in range(6)), None]
    line = path_line(hops, prefix_bytes=2, self_name="Me", show_hash=True,
                     device_hash="a1b2", bare_self=True, mode="plain")
    text = line.text()
    body = " → ".join(f"{i:02x}aa" for i in range(6))
    assert text.plain == f"{SELF_GLYPH} → {body} → {SELF_GLYPH}"
    assert "Me" not in text.plain and "a1b2" not in text.plain
    stars = [s for s in text.spans if text.plain[s.start : s.end] == SELF_GLYPH]
    assert [str(s.style) for s in stars] == ["faint", "faint"]
    chips = PathLine(line.hops, mode="powerline").text()
    star_fills = [str(s.style) for s in chips.spans
                  if chips.plain[s.start : s.end] == SELF_GLYPH]
    assert all("#ffffff" not in fill for fill in star_fills)  # never the you white

    wrapped = path_line(hops, prefix_bytes=2, self_name="Me", bare_self=True,
                        mode="powerline").wrapped(28, indent=2)
    assert len(wrapped) > 1
    assert all(text.plain[2 + WRAP_OFFSET] == POWERLINE_SEP for text in wrapped[1:])
    assert all(text.cell_len <= 28 for text in wrapped)

    lines = path_line(hops, prefix_bytes=2, self_name="Me", bare_self=True,
                      mode="powerline").wrapped(28, indent=2)
    assert len(lines) > 1
    assert all(line.plain[2 + WRAP_OFFSET] == POWERLINE_SEP for line in lines[1:])
    assert all(line.cell_len <= 28 for line in lines)
