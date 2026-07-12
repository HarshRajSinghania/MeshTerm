"""The admin-node picker: choose a remote node you hold (or will enter) credentials for.

Shared by the tools that administer remote nodes over the mesh — TX optimize and the
repeater admin screen — so "pick the node to manage" reads identically everywhere.
Credentialed nodes lead the list in their own 🔑 section (those are the nodes previously
administered, the likeliest picks), repeaters and room servers follow, then everything
else; each section orders by how recently the node was heard. Any contact with a public
key is offerable, since holding a password is a fact about the *user*, not the node.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rich.text import Text

from ..core.models import NODE_TYPE_REPEATER, NODE_TYPE_ROOM, Contact

if TYPE_CHECKING:
    from ..context import AppContext


async def pick_admin_node(
    ctx: "AppContext",
    contacts: list[Contact],
    *,
    title: str,
    prompt: str,
) -> Optional[Contact]:
    """Pick a remote node to administer, credentialed and infrastructure nodes first.

    Args:
        ctx: Shared application context (for the UI surface and the admin store).
        contacts: The device's known contacts.
        title: The select screen's heading (names the calling feature).
        prompt: One line above the list saying what the pick is for.

    Returns:
        The chosen contact, or ``None`` if cancelled (or there is nothing to pick).
    """
    from .menus import back_rows, section_heading
    from .tui import Choice
    from .widgets import _DEFAULT_GLYPH, _NODE_GLYPHS

    candidates = [c for c in contacts if (c.public_key or c.key_prefix).strip()]
    if not candidates:
        ctx.ui.note("[err]no contacts with a key — receive an advert first[/err]")
        await ctx.ui.present(title=title)
        return None

    def row(contact: Contact) -> Choice:
        glyph, style = _NODE_GLYPHS.get(contact.node_type, _DEFAULT_GLYPH)
        label = Text(glyph, style=style)
        label.append(f" {contact.name}")
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
    items.extend(back_rows())

    choice = await ctx.ui.select(title, items, prompt=prompt, wrap=False)
    if choice is None:
        return None
    return next((c for c in candidates if c.name == choice), None)
