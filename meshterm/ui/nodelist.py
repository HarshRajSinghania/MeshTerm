"""The shared node list: one sortable, filterable lane layout for every screenful of nodes.

Extracted from the Time Machine's subject picker so the app has exactly one way to draw "a
full-screen list of nodes": aligned ``NAME · HEARD · PKTS · HASH`` lanes under a sort-aware
column header, our own node pinned first, the sort riding the Ctrl+arrows (plain arrows keep
the highlight, letters keep type-to-filter), and the name lane sized to its content so the
columns anchor left while the hash lane soaks up the rest of the terminal. The Time Machine
picker and the Nodes screen both build on :class:`NodeListScreen`; each hands its rows over
as :class:`NodeRow` values — however it learned them (stored history, the device's contact
table) — so the two lists render, sort, and steer identically without sharing a data source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from rich.cells import cell_len
from rich.text import Text

from .map_render import _SELF
from .menus import fit_cells
from .tui.select import Choice, SelectScreen, Separator
from .widgets import (
    _DEFAULT_GLYPH,
    _NODE_GLYPHS,
    _age_seconds,
    _format_age,
    _recency_style,
    highlighted_hash,
    NodesSort,
)

#: The narrowest the name lane shrinks to (a very narrow terminal), so the header's ``NAME``
#: label and its sort triangle always have somewhere to sit.
_NAME_MIN = 6

#: The hash lane's floor in cells — four leading bytes plus an ellipsis, still a recognisable
#: prefix on a very narrow terminal. The lane otherwise flexes to fill whatever width the
#: (content-sized) name lane leaves, so a longer key — our own 64-hex node key, or a full key
#: resolved from a contact — shows as many whole bytes as fit, ellipsized past that (see
#: :func:`~meshterm.ui.widgets.highlighted_hash`).
_HASH_MIN = 8

#: Everything in a row *besides* the name and hash lanes, in cells: the select pointer (2),
#: the type glyph and its space (2), then the two gapped fixed lanes — heard (2 gap + 5) and
#: packets (2 gap + 5) — and the 3-cell gap before the hash. The name lane is content-sized
#: and the hash lane takes the rest (see :meth:`NodeListScreen._lane_widths`).
_LEAD = 2 + 2 + (2 + 5) + (2 + 5) + 3

#: The muted ``(you)`` tag on the own-node lane (see :func:`_lane`), its width folded into
#: the name lane's content sizing so the tag never truncates.
_YOU_TAG = "  (you)"

#: The sort ring and each column's natural opening direction — name A→Z, most-recently-heard
#: first, most packets first, and ``hash`` on the node's key (ascending = ``0`` → ``f``).
#: Callers build their :class:`~meshterm.ui.widgets.NodesSort` over these so the Ctrl+arrows
#: walk the same four columns on every node list.
SORT_COLUMNS: tuple[str, ...] = ("name", "heard", "packets", "hash")
SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True, "heard": True, "packets": False, "hash": True,
}

#: The cyan the active sort column and its triangle are lit in, matching the static Nodes
#: table's header (see :func:`~meshterm.ui.widgets._sort_header`) so sort cues read
#: identically everywhere.
_SORT_ACTIVE = "bold #22d3ee"

#: The default footer: navigation, the Ctrl+arrow sort, filtering, then Esc last.
_HINT = "↑↓ move · ^←→↑↓ sort · type filter · Enter open · Esc back"


@dataclass
class NodeRow:
    """One node's lane data, however the caller learned it.

    Attributes:
        value: What the row's :class:`~meshterm.ui.tui.select.Choice` resolves with —
            also the identity a re-sort uses to keep the highlight on its node, so it
            should be unique across the list.
        name: The display name; ``None`` renders the heat-coloured ``unknown`` (an
            own-node row falls back to a bare ``you`` instead).
        key: The hex the hash lane shows — as full a key as the caller could resolve;
            empty renders a muted ``?``.
        node_type: The node's type for the leading glyph (``None`` = the plain-node
            ``●``; ignored on the own-node row, which always leads with the yellow ``★``).
        last_seen: When the node was last heard (aware UTC) — colours the name by recency
            heat and fills the heard lane; ``None`` reads ``never`` and the cold style.
        count: The packet tally; ``None`` renders a faint ``—`` (never overheard).
        you: Whether this is our own node: the ``★`` marker, the pure-white ``you`` name
            style with a muted ``(you)`` tag, faint ``—`` heard/packet lanes (we never
            overhear ourselves), pinned above the sorted block whatever the sort.
    """

    value: object
    name: Optional[str]
    key: str = ""
    node_type: Optional[int] = None
    last_seen: Optional[datetime] = None
    count: Optional[int] = None
    you: bool = False


def _header(name_w: int, sort: NodesSort) -> Text:
    """Column labels over the node lanes (see :func:`_lane`).

    The lanes read ``NAME · HEARD · PKTS · HASH``, matching the row builder. The four
    leading spaces cover the select screen's pointer column (2 cells) plus the one-cell
    type glyph and its gap, so each label lands over its lane.

    All four columns are sortable, so any one can be the active sort. The active column's
    label *and* its direction triangle (``▲`` ascending, ``▼`` descending) are lit cyan
    together — the same cue the static Nodes table's
    :func:`~meshterm.ui.widgets._sort_header` lights — and the triangle is drawn *into the
    two-cell reserve that already follows every label*, so switching the sort never widens
    a lane and shifts the rest of the row. Returned as a :class:`~rich.text.Text` (not a
    plain string) so just the active column carries the colour while the rest stays muted.
    """

    def column(header: Text, label: str, key: str, *, pad_to: int = 0) -> None:
        """Append one column: its label plus a two-cell mark reserve, lit when it's the sort."""
        if key == sort.column:
            cell = f"{label} " + ("▲" if sort.ascending else "▼")
            header.append(cell, style=_SORT_ACTIVE)
        else:
            cell = f"{label}  "
            header.append(cell, style="muted")
        if pad_to:  # left-justify the flexing name lane; the pad stays muted
            header.append(" " * max(0, pad_to - cell_len(cell)), style="muted")

    header = Text("    ", style="muted")  # pointer (2) + the row's type glyph and gap (2)
    column(header, "NAME", "name", pad_to=name_w)
    header.append("  ", style="muted")  # gap to the HEARD lane
    column(header, "HEARD", "heard")
    column(header, f"{'PKTS':>5}", "packets")
    # HASH sits one cell further out than the other lanes so a packets-sort triangle —
    # packets being the lane just before it — never abuts the hash label.
    header.append(" ", style="muted")
    column(header, "HASH", "hash")
    return header


def _you_lane(row: NodeRow, name_w: int, prefix_bytes: int, hash_w: int) -> Text:
    """Our own node's lane — laid out exactly like :func:`_lane`'s regular rows.

    Drawn like the map and the static table draw us: the ``★`` self marker (yellow), the
    name in the pure-white ``you`` style with a muted ``(you)`` tag, and our key hash lit
    at the routing width, filling the flexing hash lane — our 64-hex key is longer than
    any lane, so it shows as many digits as fit and ellipsizes. The heard and packet lanes
    read a faint ``—``: we never overhear ourselves, so there is no reception age or count
    to show. With no reachable device to name us, the name falls back to a bare ``you``
    and the hash to ``?``.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(_SELF[0], style=_SELF[1])
    text.append(" ")
    # The name lane, exactly name_w cells: the name (white) with a snug muted "(you)" tag —
    # padded out to fill the lane — or, when the name alone would crowd out the tag, the
    # name fit to the lane with the tag dropped. The lane is sized to hold the tag (see
    # NodeListScreen._widest_name), so the drop is a very-long-name guard.
    used = cell_len(row.name) + cell_len(_YOU_TAG) if row.name else 0
    if row.name and used <= name_w:
        text.append(row.name, style="you")
        text.append(_YOU_TAG, style="muted")
        text.append(" " * (name_w - used))
    else:
        text.append(fit_cells(row.name or "you", name_w), style="you")
    text.append("  ")
    text.append(f"{'—':>5}", style="faint")  # heard: we don't hear ourselves
    text.append("  ")
    text.append(f"{'—':>5}", style="faint")  # packets: nothing to count
    text.append("   ")  # the wider gap the header's HASH lane keeps (see _header)
    if row.key:
        text.append_text(highlighted_hash(row.key, prefix_bytes, width=hash_w))
    else:
        text.append("?", style="muted")
    return text


def _lane(row: NodeRow, name_w: int, prefix_bytes: int, hash_w: int) -> Text:
    """One node as fixed, colour-coded lanes under :func:`_header`'s columns.

    The type glyph leads (the app's shared marker palette), so the mark reads ``▲`` for a
    repeater, ``■`` for a room, ``◉`` for a sensor, ``●`` for a plain node. The name is
    coloured by recency heat, ``unknown`` included, so a freshly heard mystery node still
    reads hot, and takes the flexing name lane (``name_w`` cells). Last-heard age and
    packet count follow, right-aligned under their headers — a node never overheard reads
    a faint ``—`` in the packets lane; the hash closes the row in the shared hash widget,
    its path-hash prefix lit at the device's routing width, or a muted ``?`` when no key
    is known at all. An own-node row (:attr:`NodeRow.you`) takes its own drawing —
    see :func:`_you_lane`.

    Args:
        row: The node's lane data.
        name_w: The name lane's width in cells (content-sized across the whole list).
        prefix_bytes: Path-hash width in bytes to light in the hash.
        hash_w: The flexing hash lane's width in cells (a short key pads out to it; see
            :func:`~meshterm.ui.widgets.highlighted_hash`).
    """
    if row.you:
        return _you_lane(row, name_w, prefix_bytes, hash_w)
    glyph, glyph_style = _NODE_GLYPHS.get(row.node_type, _DEFAULT_GLYPH)
    secs = _age_seconds(row.last_seen)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(glyph, style=glyph_style)
    text.append(" ")
    text.append(fit_cells(row.name or "unknown", name_w), style=_recency_style(secs))
    text.append("  ")
    text.append(f"{_format_age(secs):>5}", style="muted")
    text.append("  ")
    if row.count is None:
        text.append(f"{'—':>5}", style="faint")
    else:
        text.append(f"{min(row.count, 99999):>5}", style="muted")
    text.append("   ")  # the wider gap the header's HASH lane keeps (see _header)
    if row.key:
        text.append_text(highlighted_hash(row.key, prefix_bytes, width=hash_w))
    else:
        text.append("?", style="muted")
    return text


def _ordered(rows: list[NodeRow], sort: NodesSort) -> list[NodeRow]:
    """Order the sortable rows by the active sort (own-node rows are pinned elsewhere).

    The four columns' natural metrics: name A→Z, ``heard`` by age so ascending is
    most-recently-heard first (a never-heard row gathers at the old end), ``packets`` by
    count, ``hash`` by the displayed key (ascending = ``0`` → ``f``). A name-ascending
    pre-sort is the stable tiebreak, so two nodes sharing a metric keep an A→Z order
    under both directions rather than flipping with the primary key.
    """

    def key_name(row: NodeRow) -> str:
        return (row.name or "unknown").casefold()

    def metric(row: NodeRow):  # noqa: ANN202 - homogeneous per sort
        if sort.column == "name":
            return key_name(row)
        if sort.column == "packets":
            return row.count or 0
        if sort.column == "hash":
            return row.key
        secs = _age_seconds(row.last_seen)  # "heard": ascending = freshest first
        return secs if secs is not None else float("inf")

    ordered = sorted(rows, key=key_name)
    ordered.sort(key=metric, reverse=not sort.ascending)
    return ordered


class NodeListScreen(SelectScreen):
    """A full-screen, sortable, filterable node list in the shared lane layout.

    The rows run in aligned lanes — name (coloured by recency heat), last-heard age,
    packet count, and key hash — own-node rows first, then the rest in the active sort
    order. The lanes anchor to the left: the name lane is sized to its widest name (not
    the terminal), so the columns stay put as the window widens and the freed width flows
    to the hash lane, which shows each key as fully as it fits (see :meth:`_lane_widths`).
    It is a full-screen list, not a floating popup.

    The sort rides the Ctrl+arrows, leaving the plain arrows for the highlight and the
    letters for type-to-filter: **Ctrl+←/→** pick the column (name → heard → packets →
    hash, each adopting its natural direction) and **Ctrl+↑/↓** force ascending/
    descending. Every change re-sorts the node block in place — the lead rows and headers
    stay pinned, the highlight rides its node, and any active filter holds — the same
    :class:`~meshterm.ui.widgets.NodesSort` model and cyan triangle cue the static Nodes
    table uses, only its keys moved off the plain arrows that a filterable list already
    spends.
    """

    floating = False

    def __init__(
        self,
        title: str,
        *,
        rows: list[NodeRow],
        prefix_bytes: int,
        sort: NodesSort,
        prompt: str = "",
        lead: Optional[list] = None,
        footer_hint: str = _HINT,
    ) -> None:
        """Build the list over already-resolved rows.

        Args:
            title: Short heading shown in the border.
            rows: Every node's lane data; :attr:`NodeRow.you` rows are pinned first (in
                the order given), the rest re-sorted here per ``sort``.
            prefix_bytes: Path-hash width in bytes to light in each hash.
            sort: The sort state, mutated in place by the Ctrl+arrows — pass the same
                instance across re-opens so the chosen order persists. Its ring should
                span :data:`SORT_COLUMNS` for the hash column to be reachable.
            prompt: An optional instruction shown above the list.
            lead: Rows (choices/separators) drawn above the column header — the Time
                Machine's whole-mesh row and its section heading; ``None`` for none.
            footer_hint: Footer key hint; the default advertises the full grammar.
        """
        self._node_rows = rows
        self._prefix_bytes = prefix_bytes
        self._sort = sort
        self._lead = list(lead) if lead else []
        # Provisional until the first render learns the true width (see render_body).
        self._name_w = _NAME_MIN
        self._hash_w = _HASH_MIN
        super().__init__(
            title,
            self._compose_items(),
            prompt=prompt,
            footer_hint=footer_hint,
            wrap=False,
        )

    def _compose_items(self) -> list:
        """The lead rows, the sort-aware column header, then the node lanes.

        Own-node rows lead the lanes and stay first whatever the sort: only the block
        below them reorders (see :func:`_ordered`)."""
        items: list = list(self._lead)
        items.append(Separator(_header(self._name_w, self._sort)))
        pinned = [row for row in self._node_rows if row.you]
        rest = [row for row in self._node_rows if not row.you]
        for row in (*pinned, *_ordered(rest, self._sort)):
            items.append(
                Choice(
                    _lane(row, self._name_w, self._prefix_bytes, self._hash_w),
                    row.value,
                )
            )
        return items

    def _rebuild(self) -> None:
        """Recompose the rows for the current sort/width, keeping the highlight on its node."""
        current = self._current_choice()
        keep = current.value if current is not None else None
        self._items = self._compose_items()
        self._reselect(keep)

    def _reselect(self, value: object) -> None:
        """Move the highlight back onto the choice with ``value`` (else clamp it in range)."""
        choices = self._choices()
        for i, choice in enumerate(choices):
            if choice.value == value:
                self._index = i
                return
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0

    def _widest_name(self) -> int:
        """The widest rendered name in cells across every lane the list draws.

        The name lane is sized to its content, not the terminal, so the columns anchor to
        the left instead of drifting apart as the window widens. The measure spans the
        regular rows (``unknown`` for the nameless, as the row renders them) and any
        own-node row's name plus its ``(you)`` tag, so the tag always fits.
        """
        widths = [cell_len("unknown")]
        for row in self._node_rows:
            if row.you:
                widths.append(
                    cell_len(row.name) + cell_len(_YOU_TAG) if row.name else cell_len("you")
                )
            else:
                widths.append(cell_len(row.name or "unknown"))
        return max(widths)

    def _lane_widths(self, width: int) -> tuple[int, int]:
        """The name and hash lane widths for a terminal ``width`` cells wide.

        The name lane is content-sized (see :meth:`_widest_name`) so it stays put as the
        window grows; it only yields when a very long name would starve the hash lane past
        its :data:`_HASH_MIN` floor. The hash lane then takes all the width the fixed
        lanes and the name lane leave, so keys show as fully as they fit — a heard id's 12
        hex digits with room to spare, a 64-hex key ellipsized to the lane.
        """
        name_cap = max(_NAME_MIN, width - _LEAD - _HASH_MIN)
        name_w = max(_NAME_MIN, min(self._widest_name(), name_cap))
        hash_w = max(_HASH_MIN, width - _LEAD - name_w)
        return name_w, hash_w

    def render_body(self, width: int) -> list[str]:
        """Size the name lane to content and flex the hash lane, then render the list."""
        name_w, hash_w = self._lane_widths(width)
        if (name_w, hash_w) != (self._name_w, self._hash_w):
            self._name_w, self._hash_w = name_w, hash_w
            self._rebuild()
        return super().render_body(width)

    def handle(self, action: str, data: str = "") -> None:
        """Steer the sort with the Ctrl+arrows; everything else is the base list's."""
        if action == "ctrl_left":
            self._sort.move(-1)
            self._rebuild()
        elif action == "ctrl_right":
            self._sort.move(1)
            self._rebuild()
        elif action == "ctrl_up":
            if not self._sort.ascending:
                self._sort.ascending = True
                self._rebuild()
        elif action == "ctrl_down":
            if self._sort.ascending:
                self._sort.ascending = False
                self._rebuild()
        else:
            super().handle(action, data)
