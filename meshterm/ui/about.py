"""The About MeshTerm pages: the app, the person who wrote it, and how to help it along.

Three read-only pages hung under the main menu's *About MeshTerm* section — the one
section that names a subject rather than a doing, because "what is this thing, and who
made it" is a question the other five can't answer. Each opens as a full screen, scrolls
if the terminal is short, and is left with Esc; nothing here touches the radio or the
database, so a page reads the same with no device attached at all.

Every page is **scaffolding** today. The headings are the real shape — that is the part
worth settling first — and each carries one lowercase muted ``placeholder — …`` line
where its prose will go, in the same empty-state voice the rest of the app uses, so an
unwritten page reads as deliberately unwritten rather than as a screen that failed to
load. Filling one in means replacing its placeholder with prose; the screen, the menu
rows, and the CLI faces need no change to follow.

What is *not* a placeholder: the version, the author, and the copyright span all come
from :mod:`meshterm` itself (:func:`~meshterm.copyright_notice`), so the About page can
never drift from the package it describes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.text import Text

from .. import __author__, __version__, copyright_notice
from .tui import ScrollScreen

if TYPE_CHECKING:
    from ..context import AppContext


#: Cells every page's prose hangs at, under its flush section heading. Applied as
#: :class:`~rich.padding.Padding` rather than a literal two spaces so a paragraph that
#: wraps keeps its block — a continuation line falling back to column 0 would read as a
#: new, unheaded thought (the app's hanging-indent rule, see ``CLAUDE.md``).
_INDENT = 2


def _heading(title: str, note: str = "") -> Text:
    """A page section heading: accent title, optional muted ``·`` note.

    Args:
        title: The section's name, sentence case.
        note: An optional aside chained after it in muted.

    Returns:
        The heading line.
    """
    text = Text(title, style="accent")
    if note:
        text.append(f"  ·  {note}", style="muted")
    return text


def _para(body: str, *, style: str = "") -> RenderableType:
    """One paragraph of page prose, indented under its heading and wrapped as a block."""
    return Padding(Text(body, style=style), (0, 0, 0, _INDENT))


def _placeholder(what: str) -> RenderableType:
    """The stand-in for prose not written yet.

    Written in the app's empty-state voice — lowercase, muted, an em-dash explanation,
    never parenthesized — so an unfinished section is legibly unfinished instead of
    looking like a rendering failure.

    Args:
        what: What will eventually be written here.

    Returns:
        The muted placeholder line, indented like the prose it stands in for.
    """
    return _para(f"placeholder — {what}", style="muted")


def about_meshterm() -> RenderableType:
    """The *About MeshTerm* page: what the app is, where it came from, its terms.

    The lead is live package data — the wordmark, the running version, and the canonical
    copyright line — so this page always describes the build it is running inside.
    """
    lead = Text()
    lead.append("MeshTerm", style="brand")
    lead.append(f" v{__version__}", style="muted")
    return Group(
        lead,
        Text("a terminal companion for MeshCore LoRa mesh devices", style="muted"),
        Text(),
        _heading("What it is"),
        _placeholder("what MeshTerm does, and who it is for"),
        Text(),
        _heading("Where it came from"),
        _placeholder("why it was built, and what it grew out of"),
        Text(),
        _heading("Licence"),
        _placeholder("the terms this ships under"),
        Text(),
        Text(copyright_notice(), style="muted"),
    )


def about_author() -> RenderableType:
    """The *About the author* page: the person behind MeshTerm, on the mesh and off it."""
    return Group(
        Text(__author__),
        Text("wrote MeshTerm", style="muted"),
        Text(),
        _heading("Who"),
        _placeholder("a few lines about the person behind MeshTerm"),
        Text(),
        _heading("On the mesh"),
        _placeholder("call sign, home node, the mesh this was written on"),
        Text(),
        _heading("Elsewhere"),
        _placeholder("where to find the author off the mesh"),
    )


def support_project() -> RenderableType:
    """The *Support this project* page: what keeps it going, and how to chip in.

    Deliberately two-sided — money is one way to help and not the only one, so the page
    keeps a section for the other kind rather than folding it into a donate link.
    """
    return Group(
        _heading("Why it needs support"),
        _placeholder("what keeps the project going, and what it costs"),
        Text(),
        _heading("Chip in"),
        _placeholder("where to sponsor, donate, or buy a coffee"),
        Text(),
        _heading("Other ways to help"),
        _placeholder("report a bug, test on hardware, tell another operator"),
    )


class AboutPage(ScrollScreen):
    """One About page: a read-only body, drawn full-screen, left with Esc.

    A thin name over the shared read-only screen — the pages carry no controls of their
    own, so everything they need (the pager, the derived footer hint that only names the
    pager when there *is* something to page, the PicoCalc F-key lane that dims on the same
    gate) already lives in :class:`~meshterm.ui.tui.screen.ScrollScreen`. Only the framing
    differs: these are full screens rather than floating views, so Esc reads *back*.
    """

    def __init__(self, title: str, body: RenderableType) -> None:
        """Show ``body`` as the page under ``title``.

        Args:
            title: The page's heading — sentence case, no icon (icons live in the menu
                rows that open these pages, never in a title).
            body: The page's content, from one of the builders above.
        """
        super().__init__(body, title=title, floating=False)


async def open_about_page(ctx: "AppContext", title: str, body: RenderableType) -> None:
    """Open one About page full-screen and hold it until the reader backs out.

    Args:
        ctx: The shared application context (must be running the interactive TUI).
        title: The page's heading.
        body: The page's content.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the About pages are only available in the menu")
    await ctx.ui.session.run_screen(AboutPage(title, body))


__all__ = [
    "AboutPage",
    "about_author",
    "about_meshterm",
    "open_about_page",
    "support_project",
]
