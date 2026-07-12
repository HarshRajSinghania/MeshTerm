"""The interactive nodes list: the node table with arrow-key sorting.

This wraps :func:`~meshterm.ui.widgets.nodes_table` in a full-screen TUI layer whose sort the
user steers with the arrows — left/right pick the column, up/down set ascending/descending —
so the table re-renders in place. The rows scroll in a window inside the fixed screen: the
title, column header, and bottom legend hold still, faint ``↑/↓ n more`` markers bracket the
window, and PgUp/PgDn slide it. The one-shot CLI (``meshterm nodes --sort …``) renders the
same table statically; only the menu gets the live sorting.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .tui.render import render_lines, render_to_ansi
from .tui.screen import ListWindow, Screen
from .widgets import NodesSort, _nodes_legend, nodes_table

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: Strips SGR colour codes so the header-rule line can be recognized by its bare glyphs.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class NodesScreen(Screen):
    """A full-screen, keyboard-sortable list of this node and its known contacts."""

    floating = False
    footer_hint = "←→ column · ↑ asc ↓ desc · PgUp/PgDn scroll · Esc back"

    def __init__(
        self,
        self_name: str,
        self_key: str,
        contacts: list[Contact],
        prefix_bytes: int,
        counts: dict[str, int],
        sort: NodesSort,
    ) -> None:
        """Create the nodes screen over already-fetched contact data.

        Args:
            self_name: This node's advertised name.
            self_key: This node's full public key (hex).
            contacts: Known contacts to list under our own node.
            prefix_bytes: Path-hash width in bytes to highlight in every key.
            counts: Overheard-packet counts keyed by lowercased 12-hex node id.
            sort: The initial sort; mutated in place as the user presses the arrows.
        """
        super().__init__()
        self.title = "Nodes"
        self._self_name = self_name
        self._self_key = self_key
        self._contacts = contacts
        self._prefix_bytes = prefix_bytes
        self._counts = counts
        self._sort = sort
        #: The row window inside the fixed screen (title, header, and legend pinned).
        self._rows_window = ListWindow()
        #: Whether the last render actually windowed the rows — degenerate renders
        #: (no header rule found, or a viewport too short) fall back to whole-body
        #: scrolling, and the page keys follow suit.
        self._windowed = False

    def render_body(self, width: int) -> list[str]:
        """Render the table at the current sort, windowing its rows in place.

        The title, column header, and rule pin above the window; the glyph legend
        pins beneath it; only the node rows scroll between them. A render whose
        header rule can't be found (degenerate narrow width) or whose viewport
        leaves the window no room falls back to the plain whole-body scroll.
        """
        table = nodes_table(
            self._self_name,
            self._self_key,
            self._contacts,
            self._prefix_bytes,
            self._counts,
            self._sort,
        )
        lines = render_lines(table, width)
        rule = self._rule_index(lines)
        legend_h = len(render_lines(_nodes_legend(), width)) + 1  # + its blank spacer
        win = self._scroll_viewport - (rule + 1) - legend_h if rule is not None else 0
        self._windowed = rule is not None and win >= 3
        if not self._windowed:
            self._scroll_total = max(1, len(lines))
            return lines

        head, rows, tail = (
            lines[: rule + 1], lines[rule + 1 : len(lines) - legend_h],
            lines[len(lines) - legend_h :],
        )
        top, count = self._rows_window.fit(len(rows), win)
        out = list(head)
        if top > 0:
            out.append(render_to_ansi(ListWindow.marker(top, "above"), width))
        out.extend(rows[top : top + count])
        below = len(rows) - top - count
        if below > 0:
            out.append(render_to_ansi(ListWindow.marker(below, "below"), width))
        out.extend(tail)
        self._scroll_total = max(1, len(out))
        return out

    @staticmethod
    def _rule_index(lines: list[str]) -> "int | None":
        """Locate the rule the table draws under its column labels.

        The table renders a full-width run of ``─`` there (box.SIMPLE_HEAD); the
        first such line marks where the pinned head ends and the rows begin.
        ``None`` if no rule is found (e.g. a degenerate narrow render).
        """
        for idx in range(1, len(lines)):
            bare = _ANSI_RE.sub("", lines[idx]).strip()
            if bare and set(bare) == {"─"}:
                return idx
        return None

    def handle(self, action: str, data: str = "") -> None:
        """Re-sort with the arrows, slide the row window with the page keys, or dismiss."""
        if action == "left":
            self._sort.move(-1)
        elif action == "right":
            self._sort.move(1)
        elif action == "up":
            self._sort.ascending = True
        elif action == "down":
            self._sort.ascending = False
        elif action == "pageup":
            if self._windowed:
                self._rows_window.top -= self._rows_window.page
            else:
                self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            if self._windowed:
                self._rows_window.top += self._rows_window.page
            else:
                self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            if self._windowed:
                self._rows_window.top = 0
            else:
                self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            if self._windowed:
                self._rows_window.to_end()
            else:
                self.scroll_to_bottom()
        elif action in ("escape", "enter"):
            self.resolve(None)


async def open_nodes(
    ctx: AppContext,
    self_name: str,
    self_key: str,
    contacts: list[Contact],
    prefix_bytes: int,
    counts: dict[str, int],
    sort: NodesSort,
) -> None:
    """Open the interactive, arrow-sortable nodes list and run until dismissed.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        self_name: This node's advertised name.
        self_key: This node's full public key (hex).
        contacts: Known contacts to list under our own node.
        prefix_bytes: Path-hash width in bytes to highlight in every key.
        counts: Overheard-packet counts keyed by lowercased 12-hex node id.
        sort: The initial sort (column + direction).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive nodes list is only available in the menu")
    screen = NodesScreen(self_name, self_key, contacts, prefix_bytes, counts, sort)
    await ctx.ui.session.run_screen(screen)
