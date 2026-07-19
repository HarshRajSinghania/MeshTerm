"""The interactive Contacts screen: the device's contacts in the shared sortable contact list.

The Time Machine picker's presentation pointed at the companion's contact table: the same
aligned ``NAME · HEARD · PKTS · KEY`` lanes, our own node pinned first, the same Ctrl+arrow
sort (now including the key column) and type-to-filter — one contact list app-wide, whatever
the data source (see :mod:`~meshterm.ui.contactlist`). Enter is deliberately inert for now: the
highlight is a cursor, not yet a selection — a per-contact action will land on it later. The
one-shot CLI (``meshterm contacts --sort …``) still renders the static
:func:`~meshterm.ui.widgets.contacts_table`; only the menu gets the live list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contactlist import ContactListScreen, ContactRow
from .widgets import ContactsSort, _contact_pkts

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: A stable identity for the own-node row (its lane data has no key to stand on when no
#: device answered), so a re-sort can keep the highlight on it.
YOU = ("you",)

#: The Contacts list's footer: the shared list's grammar minus the Enter atom — pressing it
#: does nothing yet, so the hint doesn't advertise it.
_HINT = "↑↓ move · ^←→↑↓ sort · type filter · Esc back"


class ContactsScreen(ContactListScreen):
    """A full-screen, Ctrl+arrow-sortable list of this node and its known contacts."""

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
                    value=c.public_key or c.name,
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
            footer_hint=_HINT,
        )

    def handle(self, action: str, data: str = "") -> None:
        """Swallow Enter; every other key is the shared list's.

        The highlight is only a cursor until per-contact actions land, so committing it
        must neither act nor dismiss the screen — only Esc leaves.
        """
        if action == "enter":
            return
        super().handle(action, data)


async def open_contacts(
    ctx: "AppContext",
    self_name: str,
    self_key: str,
    contacts: "list[Contact]",
    prefix_bytes: int,
    counts: dict[str, int],
    sort: ContactsSort,
) -> None:
    """Open the interactive, sortable contacts list and run until dismissed with Esc.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        self_name: This node's advertised name.
        self_key: This node's full public key (hex).
        contacts: Known contacts to list under our own node.
        prefix_bytes: Path-hash width in bytes to highlight in every key.
        counts: Overheard-packet counts keyed by lowercased 12-hex node id.
        sort: The initial sort (column + direction) over the shared list's ring.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive contacts list is only available in the menu")
    screen = ContactsScreen(self_name, self_key, contacts, prefix_bytes, counts, sort)
    await ctx.ui.session.run_screen(screen)
