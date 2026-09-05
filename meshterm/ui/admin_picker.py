"""The admin-node picker: choose a remote node you hold (or will enter) credentials for.

Shared by the tools that administer remote nodes over the mesh — TX optimize and the
repeater admin screen — so "pick the node to manage" reads identically everywhere.
Credentialed nodes lead the list in their own 🔑 section (those are the nodes previously
administered, the likeliest picks), repeaters and room servers follow, then everything
else; each section orders by how recently the node was heard. Any contact with a public
key is offerable, since holding a password is a fact about the *user*, not the node.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from rich.text import Text

from ..core.models import NODE_TYPE_REPEATER, NODE_TYPE_ROOM, Contact

if TYPE_CHECKING:
    from ..context import AppContext


def admin_picker_rows(
    ctx: "AppContext", contacts: list[Contact]
) -> tuple[list, list[Contact]]:
    """Build the admin-node picker's grouped rows and the contacts they map to.

    Shared so the pick and any later redraw are guaranteed identical: :func:`pick_admin_node`
    runs these rows, and a caller can rebuild the same list as a static backdrop to float a
    follow-up dialog (the admin login) over the very list the node was picked from. Rows carry
    ``value = contact.name``, so a picked name (or a backdrop's ``default``) resolves through
    ``candidates``.

    Args:
        ctx: Shared application context (for the admin store, which sorts remembered nodes up).
        contacts: The device's known contacts.

    Returns:
        ``(rows, candidates)`` — the grouped select rows and the offerable contacts
        (those holding a key). Both are empty when nothing is offerable; the caller
        notes that and never opens a list at all.
    """
    from .menus import section_heading
    from .theme import name_style
    from .tui import Choice
    from .widgets import _DEFAULT_GLYPH, _NODE_GLYPHS

    candidates = [c for c in contacts if (c.public_key or c.key_prefix).strip()]
    if not candidates:
        return [], candidates

    def row(contact: Contact) -> Choice:
        # The type mark keeps its own fixed hue; the *name* takes the node's key-derived
        # colour like every other list of nodes (a style on the Text itself would be the
        # row's base and would paint the name the type's colour too).
        glyph, glyph_style = _NODE_GLYPHS.get(contact.node_type, _DEFAULT_GLYPH)
        label = Text()
        label.append(f"{glyph} ", style=glyph_style)
        label.append(
            contact.name,
            style=name_style(contact.name, contact.public_key or contact.key_prefix),
        )
        return Choice(title=label, value=contact.name)

    def recency(contact: Contact) -> float:
        return -(contact.last_seen.timestamp() if contact.last_seen else 0.0)

    remembered = [c for c in candidates if ctx.admin_store.get(c) is not None]
    infrastructure = [
        c for c in candidates
        if c not in remembered and c.node_type in (NODE_TYPE_REPEATER, NODE_TYPE_ROOM)
    ]
    others = [c for c in candidates if c not in remembered and c not in infrastructure]

    items: list = []
    if remembered:
        items.append(section_heading("Remembered admins"))
        items.extend(row(c) for c in sorted(remembered, key=recency))
    if infrastructure:
        items.append(section_heading("Repeaters & rooms"))
        items.extend(row(c) for c in sorted(infrastructure, key=recency))
    if others:
        items.append(section_heading("Other contacts"))
        items.extend(row(c) for c in sorted(others, key=recency))
    return items, candidates


async def pick_admin_node(
    ctx: "AppContext",
    contacts: list[Contact],
    *,
    title: str,
    prompt: str,
) -> Optional[Contact]:
    """Pick a remote node to administer, credentialed and infrastructure nodes first.

    The one-shot form: the list comes down as soon as a node is picked. A caller whose next
    step belongs *over* the list — the admin login, which asks about the node just picked —
    wants :func:`admin_node_visit` instead.

    Args:
        ctx: Shared application context (for the UI surface and the admin store).
        contacts: The device's known contacts.
        title: The select screen's heading (names the calling feature).
        prompt: One line above the list saying what the pick is for.

    Returns:
        The chosen contact, or ``None`` if cancelled (or there is nothing to pick).
    """
    items, candidates = admin_picker_rows(ctx, contacts)
    if not candidates:
        await _note_nothing_to_pick(ctx, title)
        return None

    choice = await ctx.ui.select(title, items, prompt=prompt)
    return _resolve(candidates, choice)


@asynccontextmanager
async def admin_node_visit(
    ctx: "AppContext",
    contacts: list[Contact],
    *,
    title: str,
    prompt: str,
) -> "AsyncIterator[Optional[_AdminNodePicker]]":
    """Keep the node picker pushed for a whole visit, yielding a picker to draw from.

    The list stays on the stack for the duration of the block, so everything the caller does
    with a picked node — the password prompt, a rejection, the admin session itself — nests
    *above* the very list the pick came from, with the picked node still highlighted, instead
    of floating over a blank frame or being drawn again as a lookalike backdrop. Esc from any
    of it is one pop back onto the list.

    Yields ``None`` when there is nothing to pick (the caller has already been told).

    Args:
        ctx: Shared application context (for the UI surface and the admin store).
        contacts: The device's known contacts.
        title: The select screen's heading (names the calling feature).
        prompt: One line above the list saying what the pick is for.
    """
    from .surface import TuiUi
    from .tui import SelectScreen

    items, candidates = admin_picker_rows(ctx, contacts)
    if not candidates or not isinstance(ctx.ui, TuiUi):
        if not candidates:
            await _note_nothing_to_pick(ctx, title)
        yield None
        return
    screen = SelectScreen(title, items, prompt=prompt)
    async with ctx.ui.session.stay(screen) as visit:
        yield _AdminNodePicker(visit, candidates)


class _AdminNodePicker:
    """The visited node list: :meth:`pick` is one round of it, resolved to a contact."""

    __slots__ = ("_visit", "_candidates")

    def __init__(self, visit: Any, candidates: list[Contact]) -> None:
        """Bind the picker to the list's visit and the contacts its rows stand for."""
        self._visit = visit
        self._candidates = candidates

    async def pick(self) -> Optional[Contact]:
        """Await one pick: the chosen contact, or ``None`` on Esc."""
        from .tui.screen import CANCEL

        choice = await self._visit.result()
        return _resolve(self._candidates, None if choice is CANCEL else choice)


async def _note_nothing_to_pick(ctx: "AppContext", title: str) -> None:
    """Say there is no offerable node and show it — no list is opened at all."""
    ctx.ui.note("[err]no contacts with a key — receive an advert first[/err]")
    await ctx.ui.present(title=title)


def _resolve(candidates: list[Contact], choice: Any) -> Optional[Contact]:
    """The contact a picked row's name stands for, or ``None`` when nothing was picked."""
    if choice is None:
        return None
    return next((c for c in candidates if c.name == choice), None)
