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

Removing *one* named contact belongs to that contact, not to the list: it is the last
action on its Node detail page (see :mod:`~meshterm.ui.node_detail_screen`), where the
reader is already looking at the node they mean to drop. The list only rebuilds afterwards.

Below the contacts sits the list's own maintenance action — **Purge stale contacts** — the
bulk counterpart, sweeping out the contacts a device has accumulated but no longer hears. It
offers an age ladder (a day up to a year, plus a *never heard* bucket), each rung showing a
live count of how many contacts it would remove, so the impact is visible before anything is
chosen; the pick is then gated behind a typed ``delete`` confirm. Either way — one contact
or a sweep — the removal lands in both halves of what the list shows: the device's own
table, and the cross-session store (see :mod:`~meshterm.core.contact_store`), so a
firmware-less bridge doesn't merge them back. Only the *contact* is dropped: each node's
reception history and overheard traffic in MeshTerm are untouched. Our own node, pinned above
the list, is never a purge candidate (it isn't in the device's contact table).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from ..core.connection import ContactNotOnDeviceError
from .contactlist import ContactListScreen, ContactRow
from .menus import marked_label, menu_rows
from .tui import Choice, Separator
from .tui.screen import CANCEL
from .widgets import ContactsSort, _age_seconds, _contact_pkts

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: A stable identity for the own-node row (its lane data has no key to stand on when no
#: device answered) — Enter on it opens our own node's detail page.
YOU = ("you",)

#: The contact-list tail sentinels (distinct from any :class:`~meshterm.core.models.Contact`
#: and from :data:`YOU`): the purge action row and the bare ``Back`` exit row.
_PURGE = ("purge",)

#: A day in seconds, for the purge age ladder.
_DAY = 86400

#: The age rungs the purge picker offers, ``(label, min_age_seconds)``. A contact qualifies
#: for a rung when its last-heard age is *at least* that long; a never-heard contact — one
#: with no advert time at all — is excluded here and swept only by the separate never-heard
#: bucket, so an age rung never quietly deletes a freshly-added contact that hasn't had time
#: to advert yet.
PURGE_RUNGS: tuple[tuple[str, int], ...] = (
    ("Older than 1 day", _DAY),
    ("Older than 1 week", 7 * _DAY),
    ("Older than 2 weeks", 14 * _DAY),
    ("Older than 1 month", 30 * _DAY),
    ("Older than 3 months", 90 * _DAY),
    ("Older than 6 months", 180 * _DAY),
    ("Older than 1 year", 365 * _DAY),
)

#: The picker value for the never-heard bucket (contacts with no advert time at all).
_NEVER = "__never__"


def stale_past(contacts: "list[Contact]", min_age_seconds: float) -> "list[Contact]":
    """Contacts last heard at least ``min_age_seconds`` ago.

    Never-heard contacts (no last-heard time) are *excluded* — they have their own bucket
    (see :func:`never_heard`), so an age rung sweeps only contacts that were once heard and
    have since gone quiet, never a contact that has simply not adverted yet.
    """
    out: list["Contact"] = []
    for contact in contacts:
        secs = _age_seconds(contact.last_seen)
        if secs is not None and secs >= min_age_seconds:
            out.append(contact)
    return out


def never_heard(contacts: "list[Contact]") -> "list[Contact]":
    """Contacts with no advert time at all — ones we have never heard broadcasting."""
    return [c for c in contacts if _age_seconds(c.last_seen) is None]


def _count_desc(n: int) -> str:
    """The count shown against a purge rung: ``no contacts`` / ``1 contact`` / ``n contacts``."""
    if n == 0:
        return "no contacts"
    return f"{n} contact{'' if n == 1 else 's'}"


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
        # The maintenance action closes the list, past every contact whatever the sort: a
        # blank spacer, then the err-tinted purge row (a `…` — it opens further prompts).
        # Offered only when there are contacts to purge.
        tail: list = []
        if contacts:
            tail = [
                Separator(" "),
                Choice(
                    title=marked_label("🗑", "Purge stale contacts…", "err"),
                    value=_PURGE,
                ),
            ]
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
    keeping the screen: the rows it was holding a place in no longer exist.

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
    screen = ContactsScreen(self_name, self_key, contacts, prefix_bytes, counts, sort)
    while True:
        rebuild = False
        async with session.stay(screen) as visit:
            while True:
                chosen = await visit.result()
                if chosen is CANCEL or chosen is None:  # Esc
                    return
                if chosen == _PURGE:
                    # The purge picker and its confirm float over the list, which is already
                    # the backdrop. A purge that removed anything invalidates the contacts
                    # cache, so the list is re-read and rebuilt — the one deliberate reset in
                    # this screen's life, because the rows it was keeping a place in are gone.
                    if await _purge_stale(ctx, self_key):
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
        screen = ContactsScreen(self_name, self_key, contacts, prefix_bytes, counts, sort)


async def _purge_stale(ctx: "AppContext", self_key: str) -> int:
    """Pick an age threshold, confirm, and remove every contact past it. Returns the count removed.

    Shows the age ladder (each rung carrying a live count of the contacts it would sweep,
    plus a never-heard bucket), gates the pick behind a typed ``delete`` confirm, then deletes
    each victim from the device's contact table and forgets it from the cross-session store so
    a forgetful bridge doesn't merge it back. Only the *contact* is dropped — each node's
    reception history in MeshTerm stays. Runs over the pushed contacts list as its backdrop.

    The sweep itself runs under the app's progress dialog — one command per victim, so the
    bar has a real total to count down — and its outcome lands in a dialog the reader
    dismisses, rather than in a note they would only meet on their way off the screen.

    Args:
        ctx: Shared application context (interactive menu).
        self_key: The device's own public key (hex) — how the contact store scopes this
            device's remembered contacts.

    Returns:
        How many contacts were removed (``0`` if cancelled or nothing matched).
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # guaranteed by open_contacts
    session = ctx.ui.session

    # Read fresh so the counts match the device right now (own node is never in this list, so
    # a purge can't touch it).
    contacts = await ctx.devstate.contacts()

    rungs = [(label, secs, stale_past(contacts, secs)) for label, secs in PURGE_RUNGS]
    never = never_heard(contacts)
    rows = [(label, _count_desc(len(victims)), secs) for label, secs, victims in rungs]
    rows.append(("Never heard", _count_desc(len(never)), _NEVER))

    from .tui import SelectScreen

    items = menu_rows(rows)
    picked = await session.run_screen(
        SelectScreen(
            f"Purge stale contacts — {len(contacts)} known",
            items,
            prompt="Remove contacts you haven't heard from in the chosen span.",
            footer_hint="↑↓ move · Enter select · Esc back",
            filterable=False,
            wrap=False,
        )
    )
    if picked is CANCEL or picked is None:
        return 0

    if picked == _NEVER:
        victims = never
        warning = (
            f"This deletes {_count_desc(len(victims))} you've never heard an advert from, "
            "removing them from this device's contact list."
        )
    else:
        span = next(label for label, secs in PURGE_RUNGS if secs == picked).lower()
        victims = stale_past(contacts, int(picked))
        warning = (
            f"This deletes {_count_desc(len(victims))} {span}, removing them from this "
            "device's contact list. Their reception history in MeshTerm is kept."
        )
    if not victims:
        ctx.ui.note("[muted]no contacts that stale — nothing to purge[/muted]")
        return 0

    if not await ctx.ui.typed_confirm(warning, "delete", title="Purge stale contacts"):
        return 0

    device = await ctx.device()
    dev_pub = (self_key or "").lower().removeprefix("0x")
    store = ctx.contact_store
    removed = 0
    failed = 0
    # A companion-local command per contact (no LoRa transmission), so a straight sequential
    # sweep — not paced radio traffic. It runs under the app's progress dialog rather than
    # the busy overlay (JP, 2026-08-10): the sweep is one command per victim over a link that
    # can be slow, so its end is a *count* away and the reader deserves to watch it come
    # down. The overlay only ever said "working", and only in the gaps between screens —
    # over a pushed list it drew nothing at all, so a hundred-contact purge looked like a
    # screen that had simply stopped answering while the arrow keys still moved a cursor
    # nothing was being done with. The dialog swallows every key for its whole lifetime, so
    # the list underneath holds still until the work is actually finished.
    with ctx.ui.progress("Purge stale contacts") as progress:
        task = progress.add_task("Purging", total=len(victims))
        for contact in victims:
            try:
                await device.remove_contact(contact)
            except ContactNotOnDeviceError:
                # The device has no such contact, so the sweep's work for it is already
                # done: this one is listed only because MeshTerm remembers it for the
                # device. It still counts as purged — forgetting it below is what makes
                # the row go away — and there is nothing here for the reader to fix.
                ctx.log.debug("contacts: %s was not on the device; purging ours", contact.name)
            except Exception as exc:  # noqa: BLE001 - one bad removal shouldn't abort the sweep
                failed += 1
                ctx.log.debug("contacts: purge failed for %s: %s", contact.name, exc)
                continue
            finally:
                progress.advance(task)
            if store is not None and dev_pub and contact.public_key:
                store.forget(dev_pub, contact.public_key)
            removed += 1
    # Drop the cached contact list so the rebuilt screen (and the dashboard) re-read the
    # device without the swept contacts.
    ctx.devstate.invalidate_contacts()

    # The outcome lands in a dialog the reader dismisses, not in a note they would only
    # meet on their way out of the screen: they asked for this sweep and waited on its bar,
    # so what it did belongs in front of them while the list they purged is still behind it.
    if removed and not failed:
        outcome = Text(f"✓ purged {_count_desc(removed)}", style="ok")
    elif removed:
        outcome = Text(
            f"purged {_count_desc(removed)}; {failed} could not be removed", style="warn"
        )
    else:
        outcome = Text("no contacts could be removed", style="err")
    await session.message_dialog(outcome, title="Purge stale contacts")
    return removed
