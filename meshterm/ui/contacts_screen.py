# SPDX-License-Identifier: Apache-2.0
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

What a sweep took is never hidden: **View archived contacts** sits directly under the purge
action and opens the archived list (see :mod:`~meshterm.ui.archived_screen`) — its own
screen, in the same lane layout, sorted on its own ``NAME · ARCHIVED · KEY`` columns. It
earns a screen rather than a section at the foot of this one because it is a different list
with different columns: an archived contact has no live heard-age or packet tally worth a
lane, and what it does have — when it left — has no column here to sit in.

Removing *one* named contact for good still belongs to that contact rather than to the list:
it is the last action on its Node detail page, where the reader is already looking at the
node they mean to drop. That one is a real deletion — it lands in both halves of what the
list shows, the device's own table and the cross-session store (see
:mod:`~meshterm.core.contact_store`), so a firmware-less bridge doesn't merge it back.
Either way, only the *contact* goes: each node's reception history and overheard traffic in
MeshTerm are untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .archived_screen import open_archived
from .contactlist import ContactListScreen, ContactRow
from .menus import icon_lane, marked_label
from .purge_screen import purge_contacts
from .tui import Choice, Separator
from .tui.screen import CANCEL
from .widgets import ContactsSort, contact_packets

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: A stable identity for the own-node row (its lane data has no key to stand on when no
#: device answered) — Enter on it opens our own node's detail page.
YOU = ("you",)

#: The contact-list tail sentinels (distinct from any :class:`~meshterm.core.models.Contact`
#: and from :data:`YOU`): the sweep, and the way in to what it has already taken.
_PURGE = ("purge",)
_ARCHIVED = ("archived",)

#: Every mark the tail's maintenance rows lead with — the set the icon column is measured
#: over (see :func:`~meshterm.ui.menus.icon_lane`). Declared rather than inferred so the
#: column is one width for the whole tail whichever of the two rows a given visit draws.
_TAIL_ICONS = ("🗑", "📂")


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
        contacts: list[Contact],
        prefix_bytes: int,
        counts: dict[str, int],
        sort: ContactsSort,
        archived: int = 0,
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
            archived: How many contacts are archived off this device. Non-zero adds the
                ``View archived contacts`` row and shows the count on it, so the tally is
                visible without opening the list; zero draws no row, since a screen should
                not offer a way into an empty list.
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
                    count=contact_packets(c, counts),
                )
            )
        # The maintenance actions close the list, past every contact whatever the sort: a
        # blank spacer, then the err-tinted sweep (a `…` — it opens further prompts), then
        # the way in to what the sweep has already taken. The two sit together because they
        # are the two halves of one idea, and the archived row is *under* the purge for the
        # same reason: it is where the purge's output went.
        # One icon column for both rows: 🗑 draws a single cell where 📂 draws two, so an
        # unpadded mark would start the purge row's label a column left of the other's.
        lane = icon_lane(_TAIL_ICONS)
        tail: list = []
        if contacts:
            tail = [
                Separator(" "),
                Choice(
                    title=marked_label("🗑", "Purge contacts…", "err", lane=lane),
                    value=_PURGE,
                ),
            ]
        if archived:
            tail = tail or [Separator(" ")]
            # The tally rides the row muted, as a status atom rather than as part of the
            # verb: it is what the row leads to, not what the row does.
            label = marked_label("📂", "View archived contacts", "", lane=lane)
            label.append(f"  ·  {archived}", style="muted")
            tail.append(Choice(title=label, value=_ARCHIVED))
        super().__init__(
            f"Contacts · {len(contacts)} known",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            tail=tail,
        )


async def open_contacts(
    ctx: AppContext,
    self_name: str,
    self_key: str,
    contacts: list[Contact],
    prefix_bytes: int,
    counts: dict[str, int],
    sort: ContactsSort,
) -> None:
    """Open the interactive contacts list; Enter opens Node detail, Esc leaves.

    The list stays pushed for the whole visit, so whichever node's detail page Enter commits
    (or the purge flow, or the archived list, that the tail actions open) nests *above* it
    and Esc from there is one pop back onto the row it was opened from — same sort, same
    filter, same scroll. Anything that changes which contacts the device holds — a sweep
    here, a single archive, restore or delete on a detail page, or a restore made from
    inside the archived list — ends the visit, re-reads the device and starts a fresh one.
    That rebuild is the deliberate exception to keeping the screen: the rows it was holding a
    place in no longer exist.

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

    def build() -> ContactsScreen:
        """The screen over the current contacts, with the live archived tally on its tail."""
        return ContactsScreen(
            self_name,
            self_key,
            contacts,
            prefix_bytes,
            counts,
            sort,
            archived=_archived_count(ctx, self_key),
        )

    screen = build()
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
                if chosen == _ARCHIVED:
                    # The archived list is a screen, not a section: it nests above this one
                    # and Esc comes back to this row. It rebuilds us only if something was
                    # restored or deleted in there, since that is when this list is stale.
                    if await open_archived(ctx, self_key, prefix_bytes):
                        rebuild = True
                        break
                    continue
                # The own-node sentinel opens our own node's page; any other value is a
                # Contact. The page nests above this list and carries the single-contact
                # management verbs — archive, restore, delete; when one of them runs, the
                # page says so on its way out and this list rebuilds without the row, the
                # same re-read the sweep does, one contact at a time.
                if await open_node_detail(ctx, None if chosen == YOU else chosen):
                    rebuild = True
                    break
        if not rebuild:
            return
        contacts = await ctx.devstate.contacts()
        screen = build()


def _archived_count(ctx: AppContext, self_key: str) -> int:
    """How many contacts are archived off this device — the tail row's tally and its gate."""
    store = getattr(ctx, "contact_store", None)
    dev_pub = (self_key or "").lower().removeprefix("0x")
    if store is None or not dev_pub:
        return 0
    return len(store.archived(dev_pub))
