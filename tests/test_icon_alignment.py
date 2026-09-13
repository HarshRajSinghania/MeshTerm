# SPDX-License-Identifier: Apache-2.0
"""The icon-column guard: in any one list, every icon-led row starts its words in one cell.

The terminal draws some of the app's icons in one cell (``🗑 ✎ ⚙ ▶ ★ ↻ ↕ ⇄ ⌨ # ✓ ✗ ⚠``)
and most emoji in two, so a row written ``icon + " " + label`` starts its words a column
left of its two-cell siblings. The fault is invisible in review — the source reads the same
either way — and it kept coming back screen by screen (the node page's ``🗑 Remove
contact…`` under ``⏳ Time machine``, the main menu, the repeater admin) until the column
was measured once in :mod:`meshterm.ui.menus`: :func:`~meshterm.ui.menus.align_icons` (which
:func:`~meshterm.ui.menus.menu_rows` applies for you), or
:func:`~meshterm.ui.menus.icon_lane` with :func:`~meshterm.ui.menus.icon_mark` /
:func:`~meshterm.ui.menus.marked_label`. This file is what keeps a new list from skipping
them.

**Coverage comes from the gallery, not from a list kept here.** :mod:`tests.test_gallery`
already builds every screen the menu can open, through its real builders, on both
platforms; this sweep imports that inventory (``_ENTRIES`` × ``_COMBOS``) so a screen added
there is guarded here with no edit. Two readings, because the gallery's screens hold their
rows two ways:

* A :class:`~meshterm.ui.tui.select.SelectScreen` holds :class:`~meshterm.ui.tui.select.Choice`
  items, so its rows are read as *data*: every choice's natural title, filter or viewport
  notwithstanding, including the rows scrolled below the fold.
* A screen that draws its own action rows (the node page, Trace, the TX sweep, a trophy
  card) has no items to read, so its body is rendered — under a viewport tall enough that
  nothing is clipped — and the list is found by its ``❯`` cursor: the contiguous run of
  pointer-column lines (``❯ `` or two spaces, then content) around it, blank lines included.

"Leads with an icon" is :func:`meshterm.ui.menus._icon_head` — the one definition the
aligner and the icon-dropping :func:`~meshterm.ui.menus.command_label` share — and a row's
words start where :func:`meshterm.ui.menus._words_start` says, measured in display cells
with :func:`rich.cells.cell_len`, never characters.

Glyphs that are *data* rather than decoration — a node's ``● ▲ ■ ◉ ○ ★``, a channel's
``＃ 🌐 🔒`` — get no exemption. They sit in the same column as the command icons around
them (the chat picker pads ``●`` out to ``🌐`` so every conversation name lines up), and a
data lane that went ragged is the same fault the reader sees. The one allowance is structural
and small: in a Choice list, a **blank** :class:`~meshterm.ui.tui.select.Separator` starts a
new group. That blank line is how :func:`~meshterm.ui.menus.exit_rows` sets its ``✓ Apply``
/ ``✗ Back — discard`` pair apart, and how a table sets its command tail apart (the Contacts
list's rows under its maintenance rows, the Courier queue row under its outbox) — the space
says "a different list", and each side is still held to its own column. A section heading
(``── Label ──``) does not break a group: sections of one list read as one column.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pytest
from rich.cells import cell_len
from rich.text import Text

from meshterm.platforms import REGULAR, Platform, set_platform
from meshterm.ui.menus import _icon_head, _words_start, align_icons, exit_rows, menu_rows
from meshterm.ui.tui import Choice, Screen, SelectScreen, Separator
from tests.conftest import plain as _plain
from tests.test_gallery import _COMBOS, _ENTRIES, _Entry

#: The pointer column every list draws before its rows: the cursor row's ``❯ ``, or two
#: spaces for the rest (see :class:`~meshterm.ui.tui.select.SelectScreen`). One cell of
#: glyph and one of space, so two cells wherever it appears.
_CURSOR = "❯ "
_GUTTER = "  "

#: A viewport taller than any gallery screen's content, so a hand-drawn list pushed below
#: the fold is still in the rendered body the sweep reads.
_TALL_VIEWPORT = 400


@dataclass(frozen=True)
class _IconRow:
    """One icon-led row of a list: its label as the reader sees it, and where its words start.

    Attributes:
        label: The row's plain text, pointer column removed.
        start: The display cell (0-based, from the label's first cell) its words begin in.
    """

    label: str
    start: int


def _icon_row(label: str) -> _IconRow | None:
    """``label`` measured as an icon row, or ``None`` when it does not lead with an icon."""
    head = _icon_head(label)
    if not head:
        return None
    return _IconRow(label, cell_len(label[: _words_start(label, head)]))


def _choice_groups(items: Iterable) -> list[list[_IconRow]]:
    """A Choice list's icon rows, grouped — a blank separator line starts a new group.

    See the module docstring for why the blank line (and only the blank line) separates
    two lists: it is the seam :func:`~meshterm.ui.menus.exit_rows` draws above its pair.
    """
    groups: list[list[_IconRow]] = [[]]
    for item in items:
        if isinstance(item, Separator):
            title = item.title(_TALL_VIEWPORT) if callable(item.title) else item.title
            text = title.plain if isinstance(title, Text) else title
            if not text.strip():
                groups.append([])
            continue
        if not isinstance(item, Choice):
            continue
        label = item.label
        row = _icon_row(label.plain if isinstance(label, Text) else label)
        if row is not None:
            groups[-1].append(row)
    return [group for group in groups if group]


def _in_pointer_column(line: str) -> bool:
    """Whether a rendered line belongs to a hand-drawn list: blank, or a pointer + content."""
    if not line.strip():
        return True
    return line.startswith((_CURSOR, _GUTTER)) and line[2:3] not in ("", " ")


def _rendered_groups(lines: list[str]) -> list[list[_IconRow]]:
    """The icon rows of every hand-drawn list in a rendered body, one group per list.

    A list is anchored by its ``❯`` row and runs, both ways, over every contiguous line in
    the pointer column. Blank lines stay inside it: a screen that spaces its actions into
    clusters (Trace's compose/explore, width/samples, run) still measures one icon column
    for all of them, and the reader reads them as one.
    """
    groups: list[list[_IconRow]] = []
    for anchor, line in enumerate(lines):
        if not line.startswith(_CURSOR):
            continue
        first = anchor
        while first > 0 and _in_pointer_column(lines[first - 1]):
            first -= 1
        last = anchor
        while last + 1 < len(lines) and _in_pointer_column(lines[last + 1]):
            last += 1
        rows = [_icon_row(row[2:]) for row in lines[first : last + 1] if row.strip()]
        groups.append([row for row in rows if row is not None])
    return [group for group in groups if group]


def _screen_groups(screen: Screen, cols: int) -> list[list[_IconRow]]:
    """Every list on ``screen``, read as items where it has them and as rendering where not."""
    if isinstance(screen, SelectScreen):
        return _choice_groups(screen._items)
    screen.note_viewport(_TALL_VIEWPORT)
    return _rendered_groups(_plain(screen.render_body(cols)).splitlines())


def _misalignments(where: str, groups: list[list[_IconRow]]) -> list[str]:
    """One failure sentence per group whose icon rows start their words in different cells."""
    problems = []
    for number, group in enumerate(groups, start=1):
        if len({row.start for row in group}) < 2:
            continue
        rows = "\n".join(f"    cell {row.start:>2}: {row.label.rstrip()!r}" for row in group)
        problems.append(
            f"{where}, list {number} of {len(groups)}: icon rows start their words in "
            f"different cells —\n{rows}\n"
            "  Pad the marks to one column: build the labels through menus.align_icons "
            "(menu_rows does it for you), or icon_lane + icon_mark / marked_label(lane=...)."
        )
    return problems


def _cases():
    for entry in _ENTRIES:
        for platform, cols, rows in _COMBOS:
            yield pytest.param(
                entry, platform, cols, rows, id=f"{entry.name}-{platform.name}-{cols}x{rows}"
            )


@pytest.mark.parametrize("entry,platform,cols,rows", list(_cases()))
def test_icon_rows_share_one_column(
    entry: _Entry, platform: Platform, cols: int, rows: int
) -> None:
    """Within one list, every icon-led row starts its words in the same display cell.

    Swept over the whole gallery on both platforms. The PicoCalc drops a command row's icon
    lane outright, but its data glyphs (a node's type, a channel's openness) stay and fold to
    one-cell stand-ins, so the rule still has something to hold there.
    """
    set_platform(platform)
    screen = entry.factory(cols, rows)
    where = f"{entry.name} on {platform.name} {cols}x{rows}"
    problems = _misalignments(where, _screen_groups(screen, cols))
    assert not problems, "\n\n".join(problems)


@pytest.mark.parametrize("platform", sorted({p for p, _, _ in _COMBOS}, key=lambda p: p.name))
def test_the_sweep_measures_something_on_every_platform(platform: Platform) -> None:
    """The guard is not vacuous: each platform compares real lists, on each reading it can.

    A gallery refactor that hid a screen's items, or a render change that moved the ``❯``
    pointer, would otherwise leave every case above passing because it found nothing to
    measure. On the desktop both readings must reach a list of two or more icon rows; on the
    PicoCalc the hand-drawn action lists carry no icons at all (the lane is dropped), so only
    the Choice reading — node-type and channel glyphs, the exit pair — is required there.
    """
    set_platform(platform)
    cols, rows = next((c, r) for p, c, r in _COMBOS if p is platform)
    measured = {"items": 0, "rendered": 0}
    for entry in _ENTRIES:
        screen = entry.factory(cols, rows)
        reading = "items" if isinstance(screen, SelectScreen) else "rendered"
        measured[reading] += sum(len(g) > 1 for g in _screen_groups(screen, cols))
    assert measured["items"] > 0, f"no Choice list with 2+ icon rows on {platform.name}"
    if platform is REGULAR:
        assert measured["rendered"] > 0, "no hand-drawn list with 2+ icon rows on regular"


def test_guard_catches_a_hand_written_icon_prefix() -> None:
    """A list written ``icon + " " + label`` fails, naming both rows and both start cells.

    ``🗑`` is one cell and ``💾`` two: the exact pair that put the node page's delete row out
    of line under its archive row. If this stops failing, the guard has stopped guarding.
    """
    screen = SelectScreen("Node", [Choice("💾 Archive contact", 1), Choice("🗑 Delete contact…", 2)])
    problems = _misalignments("node page", _screen_groups(screen, 72))
    assert len(problems) == 1
    assert "cell  3: '💾 Archive contact'" in problems[0]
    assert "cell  2: '🗑 Delete contact…'" in problems[0]

    aligned = SelectScreen(
        "Node",
        [
            Choice(label, n)
            for n, label in enumerate(align_icons(["💾 Archive contact", "🗑 Delete contact…"]))
        ],
    )
    assert not _misalignments("node page", _screen_groups(aligned, 72))


def test_guard_catches_a_hand_drawn_list_through_its_rendering() -> None:
    """A screen with no items is read off its rendered body, the pointer column removed.

    The cursor row and its gutter siblings are one list across a blank line, and the text
    above (a card, not in the pointer column) is not part of it.
    """
    lines = [
        "route  ★ → 3d63 → ★",
        "",
        "  ✎ Compose path",
        "  ⚡ Explore paths",
        "",
        "❯ ▶ Trace — one transmission",
        "",
        "Per-hop medians",
        "  ↓ 2 more",
    ]
    groups = _rendered_groups(lines)
    assert [[row.start for row in group] for group in groups] == [[2, 3, 2]]
    assert _misalignments("trace", groups)


def test_exit_pair_is_its_own_group() -> None:
    """The staged-changes pair after its blank line is measured apart from the rows above it.

    ``✓``/``✗`` are one-cell status marks on a commit row, never padded to a list's emoji
    column (see :func:`~meshterm.ui.menus.exit_rows`); the blank separator is the seam. The
    pair is still held to its own column, and so are the rows above it.
    """
    items = menu_rows([("📡 Send advert", "Flood it", 1), ("⌨ Command line", "Raw CLI", 2)])
    items += exit_rows(2, apply_value="apply", back_value="back")
    groups = _choice_groups(items)
    assert [[row.start for row in group] for group in groups] == [[3, 3], [2, 2]]
    assert not _misalignments("editor", groups)
