"""The interactive nodes list: the node table with arrow-key sorting.

This wraps :func:`~meshterm.ui.widgets.nodes_table` in a full-screen TUI layer whose sort the
user steers with the arrows — left/right pick the column, up/down set ascending/descending —
so the table re-renders in place. The one-shot CLI (``meshterm nodes --sort …``) renders the
same table statically; only the menu gets the live sorting.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .tui.render import render_lines
from .tui.screen import Screen
from .widgets import NodesSort, nodes_table

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

    def render_body(self, width: int) -> list[str]:
        """Render the table+legend at the current sort, pinning the column header on scroll."""
        table = nodes_table(
            self._self_name,
            self._self_key,
            self._contacts,
            self._prefix_bytes,
            self._counts,
            self._sort,
        )
        lines = render_lines(table, width)
        self._scroll_total = max(1, len(lines))
        # Pin the ``Name / Heard / Pkts / Key`` labels so they stay in view once the rows scroll
        # past — the base Screen.sticky_header re-draws the recorded line as the top row. The
        # header sits directly above the rule the table draws under it (box.SIMPLE_HEAD).
        self._sticky_headers = self._column_header(lines)
        return lines

    @staticmethod
    def _column_header(lines: list[str]) -> list[tuple[int, str]]:
        """Locate the column-header row (the line just above the table's header rule).

        The table renders a full-width rule of ``─`` under its column labels; the first such
        line marks the header, so the row above it carries the labels. Returns a single-entry
        sticky-header list, or empty if no rule is found (e.g. a degenerate narrow render).
        """
        for idx in range(1, len(lines)):
            bare = _ANSI_RE.sub("", lines[idx]).strip()
            if bare and set(bare) == {"─"}:
                return [(idx - 1, lines[idx - 1])]
        return []

    def handle(self, action: str, data: str = "") -> None:
        """Re-sort with the arrows, scroll with PageUp/PageDown/Home/End, or dismiss."""
        if action == "left":
            self._sort.move(-1)
        elif action == "right":
            self._sort.move(1)
        elif action == "up":
            self._sort.ascending = True
        elif action == "down":
            self._sort.ascending = False
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
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
