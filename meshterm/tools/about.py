"""The About MeshTerm tools: the three pages under the menu's last section.

*About MeshTerm*, *About the author*, *Support this project* — in that order, which is
the order a stranger asks the questions in: what is this, who made it, how do I help.
Each is a page rather than a feature: it reads nothing, transmits nothing, and needs no
device, so it opens straight to its screen (see :mod:`meshterm.ui.about`) instead of
prompting for anything first.

Each also keeps a CLI face — ``meshterm about``, ``meshterm about-author``,
``meshterm support`` — printing the same page to the terminal, so the answers are
reachable from a shell without launching the full-screen session.

The pages themselves are written in markdown under ``meshterm/assets/pages`` (see
:mod:`meshterm.ui.about`); nothing here knows what any of them say.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..context import AppContext
from .base import Tool, ToolResult, register

if TYPE_CHECKING:
    from ..ui.markdown import MarkdownDoc


class _AboutTool(Tool):
    """Shared behaviour for the three About pages: open the screen, or print the page.

    The pages differ only in their title, icon, and content, so everything else — the
    menu-only screen open, the printing CLI face, the run row — lives here once. A
    subclass supplies :meth:`page`, and nothing more.
    """

    category = "About MeshTerm"

    @staticmethod
    def page() -> "MarkdownDoc":
        """Build this page's content.

        Overridden by each page. Subclasses import their builder *inside* the override
        rather than at module scope: the registry imports every tool module at startup
        (see :func:`~meshterm.tools.load_all_tools`), and a page's content is only ever
        wanted once someone actually opens it.
        """
        raise NotImplementedError

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Open the page; returning ``None`` completes the invocation with no run row.

        The page transmits nothing and stores nothing, so — like the Trophy case and the
        mesh walk — it lives here rather than in :meth:`run`, and logs no run of its own.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.about import open_about_page

        await open_about_page(ctx, self.title, self.page())
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print the page — only reachable from the CLI (the menu opens the screen).

        Args:
            ctx: Shared application context.
            params: Unused beyond the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` naming the page that was shown.
        """
        ctx.ui.show(self.page())
        return ToolResult(summary={"page": self.name})


@register
class AboutMeshTermTool(_AboutTool):
    """What MeshTerm is, where it came from, and the terms it ships under."""

    name = "about"
    title = "About MeshTerm"
    icon = "📖"
    help = "What this is, where it came from, and the terms it ships under"
    order = 10  # the question a stranger asks first

    @staticmethod
    def page() -> "MarkdownDoc":
        """The *About MeshTerm* page."""
        from ..ui.about import about_meshterm

        return about_meshterm()


@register
class AboutAuthorTool(_AboutTool):
    """The person behind MeshTerm, on the mesh and off it."""

    name = "about-author"
    title = "About the author"
    icon = "👤"
    help = "The person behind MeshTerm, on the mesh and off it"
    order = 20  # who made the thing you just read about

    @staticmethod
    def page() -> "MarkdownDoc":
        """The *About the author* page."""
        from ..ui.about import about_author

        return about_author()


@register
class SupportProjectTool(_AboutTool):
    """What keeps MeshTerm going, and the ways — paid and unpaid — to help it along."""

    name = "support"
    title = "Support this project"
    icon = "💰"
    help = "What keeps MeshTerm going, and the ways to help it along"
    order = 30  # the ask, and only once the first two have earned it

    @staticmethod
    def page() -> "MarkdownDoc":
        """The *Support this project* page."""
        from ..ui.about import support_project

        return support_project()
