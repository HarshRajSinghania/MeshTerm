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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contactlist import ContactListScreen, ContactRow
from .tui.screen import CANCEL
from .widgets import ContactsSort, _contact_pkts

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: A stable identity for the own-node row (its lane data has no key to stand on when no
#: device answered) — Enter on it opens our own node's detail page.
YOU = ("you",)


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
        """
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
        super().__init__(
            f"Contacts · {len(contacts)} known",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
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

    Runs the show/open/reshow loop: the sortable list, then whichever node's detail page
    Enter commits, then the list again (same sort, the highlight kept on its row) — until
    Esc backs out of the list itself.

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
    # One screen for the whole visit: re-running it keeps the highlight (and any sort or
    # filter) on the row the user just opened a detail for, rather than snapping to the top.
    screen = ContactsScreen(self_name, self_key, contacts, prefix_bytes, counts, sort)
    while True:
        chosen = await session.run_screen(screen)
        if chosen is CANCEL or chosen is None:
            return
        # The own-node sentinel opens our own node's page; any other value is a Contact.
        await open_node_detail(ctx, None if chosen == YOU else chosen)
