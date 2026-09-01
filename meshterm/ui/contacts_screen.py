"""The interactive Contacts screen: the device's contacts in the shared sortable contact list.

The Time Machine picker's presentation pointed at the companion's contact table: the same
aligned ``NAME · HEARD · PKTS · KEY`` lanes, our own node pinned first, the same Ctrl+arrow
sort (now including the key column) and type-to-filter — one contact list app-wide, whatever
the data source (see :mod:`~meshterm.ui.contactlist`). Enter opens the highlighted node's
full-screen Node detail page (:mod:`~meshterm.ui.node_detail_screen`) — its identity, a
location preview, the observed routes to it, and the ways in (trace, map, time machine) —
and backing out of that page returns to the list right where it stood. The one-shot CLI
(``meshterm contacts --sort …``) still renders the static
:func:`~meshterm.ui.widgets.contacts_table`; only the menu gets the live list.

Below the contacts sits the list's own maintenance action — **Purge contacts** — the bulk
counterpart, which exists because a companion's contact table is finite and a busy mesh
fills it with nodes heard once in passing until there is no room left to discover anyone
new. It ranks the whole table (see :mod:`~meshterm.core.contact_score`) and archives the
weakest off the device while MeshTerm keeps them; the flow, its ladder and its preview live
in :mod:`~meshterm.ui.purge_screen`. Our own node, pinned above the list, is never a
candidate — it isn't in the device's contact table.

The contacts a sweep archived are listed under their own **Archived** section at the foot of
this screen. They are shown rather than hidden because silent archiving is how a reader
loses track of what they archived, and because the section is the route back: Enter opens
the node's detail page, which offers to restore it.

Removing *one* named contact for good still belongs to that contact rather than to the list:
it is the last action on its Node detail page, where the reader is already looking at the
node they mean to drop. That one is a real deletion — it lands in both halves of what the
list shows, the device's own table and the cross-session store (see
:mod:`~meshterm.core.contact_store`), so a firmware-less bridge doesn't merge it back.
Either way, only the *contact* goes: each node's reception history and overheard traffic in
MeshTerm are untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .contactlist import ContactListScreen, ContactRow
from .menus import marked_label
from .purge_screen import archived_rows, purge_contacts
from .tui import Choice, Separator
from .tui.screen import CANCEL
from .widgets import ContactsSort, _contact_pkts

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: A stable identity for the own-node row (its lane data has no key to stand on when no
#: device answered) — Enter on it opens our own node's detail page.
YOU = ("you",)

#: The contact-list tail sentinels (distinct from any :class:`~meshterm.core.models.Contact`
#: and from :data:`YOU`): the purge action row and the bare ``Back`` exit row.
_PURGE = ("purge",)


class ContactsScreen(ContactListScreen):
    """A full-screen, Ctrl+arrow-sortable list of this node and its known contacts.

    Enter resolves the highlighted row — our own node (:data:`YOU`) or a
    :class:`~meshterm.core.models.Contact` — for :func:`open_contacts` to open in the
    Node detail page; the shared list handles the sort, filter, and navigation.
    """

    def __init__(
        self,
        self_name: str,
        self_key: str,
        contacts: "list[Contact]",
        prefix_bytes: int,
        counts: dict[str, int],
        sort: ContactsSort,
        archived: Optional[list] = None,
    ) -> None:
        """Create the contacts screen over already-fetched contact data.

        Args:
            self_name: This node's advertised name.
            self_key: This node's full public key (hex); blank renders a muted ``?``.
            contacts: Known contacts to list under our own node.
            prefix_bytes: Path-hash width in bytes to highlight in every key.
            counts: Overheard-packet counts keyed by lowercased 12-hex node id; a contact
                with no entry shows a faint ``—``.
            sort: The initial sort; mutated in place by the Ctrl+arrows. Its ring should
                span :data:`~meshterm.ui.contactlist.SORT_COLUMNS` so the hash sort is
                reachable.
            archived: Pre-built rows for the ``Archived`` section (see
                :func:`~meshterm.ui.purge_screen.archived_rows`), appended after the purge
                action. ``None`` draws no section at all.
        """
        archived = list(archived or [])
        rows = [ContactRow(value=YOU, name=self_name, key=self_key, you=True)]
        for c in contacts:
            rows.append(
                ContactRow(
                    # The contact itself is the row's identity, so Enter hands the whole
                    # record straight to the detail page — no re-lookup by name/key.
                    value=c,
                    name=c.name,
                    key=c.public_key,
                    node_type=c.node_type,
                    last_seen=c.last_seen,
                    count=_contact_pkts(c, counts),
                )
            )
        # The maintenance action closes the list, past every contact whatever the sort: a
        # blank spacer, then the err-tinted purge row (a `…` — it opens further prompts).
        # Offered only when there are contacts to purge. Anything already archived follows
        # it in its own section, so the two halves of the sweep — what it can still take,
        # and what it has taken — sit together at the foot of the list.
        tail: list = []
        if contacts:
            tail = [
                Separator(" "),
                Choice(
                    title=marked_label("🗑", "Purge contacts…", "err"),
                    value=_PURGE,
                ),
            ]
        tail += archived
        super().__init__(
            f"Contacts · {len(contacts)} known",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            tail=tail,
        )


async def open_contacts(
    ctx: "AppContext",
    self_name: str,
    self_key: str,
    contacts: "list[Contact]",
    prefix_bytes: int,
    counts: dict[str, int],
    sort: ContactsSort,
) -> None:
    """Open the interactive contacts list; Enter opens Node detail, Esc leaves.

    The list stays pushed for the whole visit, so whichever node's detail page Enter commits
    (or the purge flow the tail action opens) nests *above* it and Esc from there is one pop
    back onto the row it was opened from — same sort, same filter, same scroll. Anything that
    removes a contact — a purge here, or the detail page's own single-contact
    ``Remove contact…`` — ends the visit, re-reads the device and starts a fresh one, so the
    contacts that went are gone from the list. That rebuild is the deliberate exception to
    keeping the screen: the rows it was holding a place in no longer exist. A row under the
    ``Archived`` section is a rebuilt :class:`~meshterm.core.models.Contact` like any other,
    so Enter on one opens the same detail page — where it can be restored to the device.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        self_name: This node's advertised name.
        self_key: This node's full public key (hex).
        contacts: Known contacts to list under our own node.
        prefix_bytes: Path-hash width in bytes to highlight in every key.
        counts: Overheard-packet counts keyed by lowercased 12-hex node id.
        sort: The initial sort (column + direction) over the shared list's ring — mutated
            in place, so a re-sort survives visiting a detail page and coming back.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .node_detail_screen import open_node_detail
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive contacts list is only available in the menu")
    session = ctx.ui.session
    screen = ContactsScreen(
        self_name, self_key, contacts, prefix_bytes, counts, sort,
        archived=archived_rows(ctx, self_key),
    )
    while True:
        rebuild = False
        async with session.stay(screen) as visit:
            while True:
                chosen = await visit.result()
                if chosen is CANCEL or chosen is None:  # Esc
                    return
                if chosen == _PURGE:
                    # The purge ladder, its preview and its confirm all float over the list,
                    # which is already the backdrop. A sweep that archived anything
                    # invalidates the contacts cache, so the list is re-read and rebuilt —
                    # the one deliberate reset in this screen's life, because the rows it was
                    # keeping a place in are genuinely gone from the device.
                    if await purge_contacts(ctx, self_key):
                        rebuild = True
                        break
                    continue
                # The own-node sentinel opens our own node's page; any other value is a
                # Contact. The page nests above this list and can also *delete* the contact
                # it details (its ``Remove contact…`` row); when it does, it says so on the
                # way out and the list rebuilds without the row — the same re-read the purge
                # does, one contact at a time.
                if await open_node_detail(ctx, None if chosen == YOU else chosen):
                    rebuild = True
                    break
        if not rebuild:
            return
        contacts = await ctx.devstate.contacts()
        screen = ContactsScreen(
            self_name, self_key, contacts, prefix_bytes, counts, sort,
            archived=archived_rows(ctx, self_key),
        )
