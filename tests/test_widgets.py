"""Shared-widget helper tests: the canonical formatting primitives every screen leans on.

Most widgets are exercised through their host screens' tests; what lives here are the
pure text helpers whose exact output *is* the app-wide convention — get these right once
and every caller inherits it.
"""

from __future__ import annotations

from meshterm.ui.widgets import _format_age, format_ago, path_text


def test_format_age_is_the_bare_column_form() -> None:
    """The lane form stays suffix-free at every magnitude, for aligned age columns."""
    assert _format_age(None) == "never"
    assert _format_age(5) == "now"
    assert _format_age(90) == "1m"
    assert _format_age(7200) == "2h"
    assert _format_age(180000) == "2d"
    assert _format_age(1300000) == "2w"


def test_format_ago_speaks_grammatical_prose() -> None:
    """The prose form says "5m ago" but bare "now"/"never" — never "now ago"."""
    assert format_ago(5) == "now"
    assert format_ago(None) == "never"
    assert format_ago(90) == "1m ago"
    assert format_ago(7200) == "2h ago"


def _resolve(hop: str) -> str:
    return {"aa": "Alice", "3d": "YUL"}.get(hop, hop)


def test_path_text_names_hops_and_keeps_hashes_bare() -> None:
    """Named hops read as their name alone; unnamed ones as their bare hash."""
    text = path_text(["aa", "77", "3d"], _resolve)
    assert text.plain == "Alice → 77 → YUL"  # no parenthesized hash after a name


def test_path_text_marks_us_white_and_names_in_their_hue() -> None:
    """Our own node takes the white ``you`` style; other names their palette hue."""
    text = path_text(["aa", "3d"], _resolve, self_name="Alice")
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("Alice") == "you"
    assert "YUL" in styles and styles["YUL"] != "you"


def test_path_text_empty_reads_as_direct() -> None:
    """No hops (or only empty tokens) renders the caller's empty word, muted."""
    assert path_text([], _resolve).plain == "direct"
    assert path_text(["", ""], _resolve).plain == "direct"
    assert path_text([], _resolve, empty="direct — no relays").plain == "direct — no relays"


def test_path_text_lights_the_hash_prefix() -> None:
    """An unnamed hop renders through the shared hash widget, its prefix lit in the
    node's hash-derived hue."""
    from meshterm.ui.theme import node_style

    text = path_text(["77bb"], _resolve, prefix_bytes=1)
    lit = [
        text.plain[s.start : s.end]
        for s in text.spans
        if str(s.style) == node_style("77bb")
    ]
    assert "77" in lit  # the addressed prefix stands out within the hash


def test_path_text_hash_as_name_shows_grey_identity_hash() -> None:
    """An unnamed hop stands in its own hash at the mode width, muted, plus its byte."""
    text = path_text(
        ["e839f2ab"], _resolve, prefix_bytes=3,
        show_hash=True, hash_bytes=1, hash_as_name=True,
    )
    assert text.plain == "e839f2 (e8)"  # mode width identity, then the addressed byte
    styles = {text.plain[s.start : s.end]: str(s.style) for s in text.spans}
    assert styles.get("e839f2") == "muted"  # greyed — colour is the "this is a name" cue
    from meshterm.ui.theme import node_style

    assert not any(str(s.style) == node_style("e839f2ab") for s in text.spans)  # no prefix lit


def test_path_text_hash_as_name_drops_the_byte_when_it_is_the_whole_hash() -> None:
    """When the mode width is one byte, the identity already is the byte — no ``(e8)``."""
    text = path_text(
        ["e839f2ab"], _resolve, prefix_bytes=1,
        show_hash=True, hash_bytes=1, hash_as_name=True,
    )
    assert text.plain == "e8"


def test_path_text_trace_flavour_annotates_hashes_and_brackets_us() -> None:
    """show_hash appends the addressed hash to names; None hops are our device."""
    text = path_text(
        [None, "aa", "77bb", None], _resolve, self_name="Homestead",
        show_hash=True, hash_bytes=1, device_hash="c0ffee",
    )
    assert text.plain == "Homestead (c0) → Alice (aa) → 77 → Homestead (c0)"


def test_path_text_dims_the_tail_from_dim_from() -> None:
    """The mirrored return leg (and the arrows into it) render faint."""
    text = path_text(
        [None, "aa", "3d", "aa", None], _resolve, self_name="us",
        show_hash=True, dim_from=3,
    )
    styles = [(text.plain[s.start : s.end], str(s.style)) for s in text.spans]
    assert ("us", "you") in styles  # the departure keeps the white you
    assert ("us", "faint") in styles  # the landing back on us is faint
    assert any(run.startswith("Alice") and style == "faint" for run, style in styles)
    assert any(run.startswith("YUL") and style != "faint" for run, style in styles)


def test_path_text_cursor_arrow_reads_as_a_reverse_block() -> None:
    """The composer's insertion cursor: that one arrow renders as a selected block."""
    text = path_text(["aa", "77", "3d"], _resolve, cursor_arrow=1)
    assert text.plain == "Alice → 77 → YUL"  # spacing survives the split append
    styles = [(text.plain[s.start : s.end], str(s.style)) for s in text.spans]
    assert styles.count(("→", "selected")) == 1  # only arrow 1, between 77 and YUL
    assert (" → ", "muted") in styles  # the other arrow stays plain
    # The cursor wins over dim_from: the arrow into a dimmed tail still reads selected.
    dimmed = path_text([None, "aa", None], _resolve, self_name="us", dim_from=2, cursor_arrow=1)
    assert any(str(s.style) == "selected" for s in dimmed.spans)
