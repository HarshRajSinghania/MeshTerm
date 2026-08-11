"""About MeshTerm tests: the three pages, their placeholders, and their menu section.

The pages carry no state and no controls, so what is worth pinning is what they *say*
and how they are framed: the live package facts they must never drift from, the
empty-state voice their unwritten sections speak in, the derived footer that only names
the pager when there is something to page, and the section they hang under in the main
menu. Width discipline on both platforms is the gallery's job (``test_gallery``).
"""

from __future__ import annotations

import pytest

from meshterm import __author__, __version__, copyright_notice
from meshterm.ui.about import (
    AboutPage,
    about_author,
    about_meshterm,
    support_project,
)

from tests.conftest import plain


def _page(builder, *, viewport: int = 40, width: int = 72) -> str:
    """Render one page as a reader sees it, with the frame's viewport already recorded."""
    screen = AboutPage("About MeshTerm", builder())
    screen.note_viewport(viewport)
    lines = screen.render_body(width)
    screen.note_metrics(len(lines), viewport)
    return plain(lines)


def test_about_meshterm_leads_with_live_package_facts() -> None:
    """The version and copyright come from the package, so the page can't describe a
    build it isn't running inside."""
    text = _page(about_meshterm)

    assert f"MeshTerm v{__version__}" in text
    assert copyright_notice() in text


def test_about_author_names_the_author_from_the_package() -> None:
    """The author line is package metadata too — one spelling, one place to change it."""
    assert __author__ in _page(about_author)


@pytest.mark.parametrize(
    ("builder", "headings"),
    [
        (about_meshterm, ("What it is", "Where it came from", "Licence")),
        (about_author, ("Who", "On the mesh", "Elsewhere")),
        (
            support_project,
            ("Why it needs support", "Chip in", "Other ways to help"),
        ),
    ],
)
def test_each_page_shows_its_sections(builder, headings) -> None:  # noqa: ANN001
    """The headings are the settled part of a scaffolded page — they render, in order."""
    text = _page(builder)

    at = [text.index(heading) for heading in headings]
    assert at == sorted(at), f"{headings} are out of order in the rendered page"


@pytest.mark.parametrize("builder", [about_meshterm, about_author, support_project])
def test_unwritten_sections_speak_the_empty_state_voice(builder) -> None:  # noqa: ANN001
    """Placeholders read as deliberately unwritten: lowercase, em-dashed, unparenthesized.

    The app's empty-state rule (see the UX standards in ``CLAUDE.md``) is what keeps a
    scaffolded page from looking like a screen that failed to load.
    """
    text = _page(builder)
    placeholders = [line for line in text.splitlines() if "placeholder" in line]

    assert placeholders, "a scaffolded page must say so"
    for line in placeholders:
        body = line.strip()
        assert body.startswith("placeholder — "), body
        assert "(" not in body and ")" not in body, body


def test_prose_wraps_into_its_own_indented_block() -> None:
    """A wrapped placeholder hangs under its heading rather than falling to column 0."""
    screen = AboutPage("Support this project", support_project())
    screen.note_viewport(40)
    # Narrow enough to force every placeholder to wrap at least once.
    lines = plain(screen.render_body(28)).splitlines()

    wrapped = [line for line in lines if line.strip() and not line.startswith(" ")]
    assert all(
        "placeholder" not in line for line in wrapped
    ), "prose escaped its indent block"


def test_footer_names_the_pager_only_when_there_is_something_to_page() -> None:
    """The derived hint obeys the rule that a footer never advertises a dead key."""
    screen = AboutPage("About MeshTerm", about_meshterm())

    screen.note_metrics(12, 40)  # the whole page fits — nothing to scroll
    assert screen.footer_hint == "Esc back"

    screen.note_metrics(60, 20)  # taller than the viewport — the pager is live
    assert screen.footer_hint == "↑↓ PgUp/PgDn scroll · Esc back"


def test_page_is_a_full_screen_not_a_floating_view() -> None:
    """These are pages, so Esc reads *back*; a floating read-only view would say *close*."""
    assert AboutPage("About MeshTerm", about_meshterm()).floating is False


def test_menu_rows_carry_the_icon_and_the_titles_do_not() -> None:
    """Each page has a menu icon; none of it leaks into the title the screen draws."""
    from meshterm.tools import get_tool

    for name in ("about", "about-author", "support"):
        tool = get_tool(name)
        assert tool is not None
        assert tool.icon, f"{name} has no menu icon"
        assert tool.icon not in tool.title
        assert tool.title.isascii(), f"{name}'s title carries a non-ascii glyph"


def test_every_page_icon_has_a_picocalc_glyph() -> None:
    """No emoji reaches the console: each icon folds to a font character (CLAUDE.md)."""
    from meshterm.platforms import PICOCALC, REGULAR, set_platform
    from meshterm.tools import get_tool
    from meshterm.ui.fontset import FONT_CODEPOINTS
    from meshterm.ui.theme import glyph

    set_platform(PICOCALC)
    try:
        for name in ("about", "about-author", "support"):
            tool = get_tool(name)
            assert tool is not None
            compact = glyph(tool.icon)
            assert compact and compact != tool.icon, f"{name}'s icon is unmapped"
            assert all(ord(ch) in FONT_CODEPOINTS for ch in compact), compact
    finally:
        set_platform(REGULAR)
