# SPDX-License-Identifier: Apache-2.0
"""The Archived contacts list: what the sweep took off the device, and when.

An archive sweep (see :mod:`~meshterm.ui.sweep_screen`) removes a contact from the *device*
— the scarce resource — and keeps it here in full. This screen is where those contacts live:
the same sortable, filterable lane layout as the Contacts list, in its own lane set
(``NAME · ARCHIVED · KEY``, :data:`~meshterm.ui.contactlist.ARCHIVED_LANES`), with its own
sort ring over exactly those three columns.

The ``ARCHIVED`` lane is drawn as ``HEARD`` is on the main list — a relative age under
recency heat — because it answers the same shape of question about a different event, and a
second grammar for "how long ago" would have been one for the reader to learn for nothing.
It opens sorted by it, freshest first: the question this screen answers is *what did that
sweep just take?*, and the answer belongs at the top.

**It carries no actions.** Not a select list with the verbs removed — a list with nothing to
commit, which Esc leaves. Everything you might do to an archived contact belongs to that one
contact and lives on its Node detail page, which Enter opens: restoring it to the device, or
deleting it for good. Putting a restore verb on this screen as well would have been a second
route to the same write, and the page is where the reader can actually see which node they
are about to act on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .contactlist import (
    ARCHIVED_LANES,
    ARCHIVED_SORT_COLUMNS,
    ARCHIVED_SORT_OPENS_ASCENDING,
    ContactListScreen,
    ContactRow,
)
from .tui.screen import CANCEL
from .widgets import ContactsSort

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.contact_store import RememberedContact

#: The list's footer: navigation, the sort, filtering, then Esc last. ``Enter open`` because
#: a row pushes the node's detail page — the same verb the Contacts list uses for the same
#: gesture.
_HINT = "↑↓ move · ^←→↑↓ sort · type to filter · Enter open · Esc back"


def _archived_time(stamp: int | None) -> datetime | None:
    """A stored archive stamp as an aware UTC datetime, or ``None`` if it carries none.

    Stored as unix seconds (see :class:`~meshterm.core.contact_store.RememberedContact`) and
    rendered as a local-time age by the lane, so it crosses into the list's own currency
    here rather than in three places downstream.
    """
    if not stamp:
        return None
    try:
        return datetime.fromtimestamp(int(stamp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None  # a corrupt stamp reads "never", not a crash


def archived_rows(archived: list[RememberedContact]) -> list[ContactRow]:
    """Turn remembered contacts into the shared list's row data.

    Args:
        archived: The device's archived contacts, from
            :meth:`~meshterm.core.contact_store.ContactStore.archived`.

    Returns:
        One :class:`~meshterm.ui.contactlist.ContactRow` each, carrying the rebuilt
        :class:`~meshterm.core.models.Contact` as its value so Enter hands the whole record
        to the detail page with no re-lookup.
    """
    return [
        ContactRow(
            value=remembered.to_contact(),
            name=remembered.name,
            key=remembered.public_key,
            node_type=remembered.node_type,
            archived_at=_archived_time(remembered.archived_at),
        )
        for remembered in archived
    ]


class ArchivedScreen(ContactListScreen):
    """A full-screen, sortable list of the contacts a sweep took off this device."""

    def __init__(self, rows: list[ContactRow], prefix_bytes: int, sort: ContactsSort) -> None:
        """Build the list over already-resolved rows.

        Args:
            rows: The archived contacts' lane data (see :func:`archived_rows`).
            prefix_bytes: Path-hash width in bytes to highlight in every key.
            sort: The sort, mutated in place by the Ctrl+arrows. Its ring should span
                :data:`~meshterm.ui.contactlist.ARCHIVED_SORT_COLUMNS`.
        """
        super().__init__(
            f"Archived contacts · {len(rows)} kept",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            prompt=("Off the device, kept here with their history. Open one to restore it."),
            footer_hint=_HINT,
            lanes=ARCHIVED_LANES,
        )


async def open_archived(ctx: AppContext, self_key: str, prefix_bytes: int) -> bool:
    """Open the archived list; Enter opens Node detail, Esc leaves. ``True`` if anything changed.

    The list stays pushed for the whole visit, so a detail page nests above it and Esc from
    there lands back on the row it was opened from — same sort, same filter, same scroll.
    A page that *restored* or *deleted* the contact it detailed ends the visit and the list
    is rebuilt without the row, which is the same deliberate exception the Contacts list
    makes: the row it was holding a place in no longer exists.

    Args:
        ctx: Shared application context (interactive menu).
        self_key: The device's own public key (hex) — how the store scopes this device's
            remembered contacts.
        prefix_bytes: Path-hash width in bytes to highlight in every key.

    Returns:
        ``True`` if any contact was restored or deleted, so the caller's own contact list
        knows to re-read.
    """
    from .node_detail_screen import open_node_detail
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # guaranteed by the Contacts screen
    session = ctx.ui.session
    store = ctx.contact_store
    dev_pub = (self_key or "").lower().removeprefix("0x")
    sort = ContactsSort.from_name("archived", ARCHIVED_SORT_COLUMNS, ARCHIVED_SORT_OPENS_ASCENDING)
    changed = False
    while True:
        rows = archived_rows(store.archived(dev_pub) if store and dev_pub else [])
        if not rows:
            # Everything was restored or deleted while the reader was in here; there is no
            # list left to show, and an empty screen they would have to Esc out of says
            # less than the note does on the way back.
            ctx.ui.note("[muted]no archived contacts[/muted]")
            return changed
        screen = ArchivedScreen(rows, prefix_bytes, sort)
        rebuild = False
        async with session.stay(screen) as visit:
            while True:
                chosen = await visit.result()
                if chosen is CANCEL or chosen is None:  # Esc
                    return changed
                # The page manages the contact it details — restore it, or delete it for
                # good — and says so on the way out, at which point this list is stale.
                if await open_node_detail(ctx, chosen):
                    changed = rebuild = True
                    break
        if not rebuild:
            return changed
