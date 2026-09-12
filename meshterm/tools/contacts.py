"""The ``contacts`` tool: list the contacts this node knows about.

The menu opens the sortable Contacts screen, which leads with our own node; the CLI
prints the contact list alone, because our own node is not a contact — ``meshterm info``
reports it, in far more detail than a row could hold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import NODE_TYPE_LABELS
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.models import Contact
    from ..ui.report import Listing


@register
class ContactsTool(Tool):
    """List this node and its known contacts, each with its path-hash prefix."""

    name = "contacts"
    title = "Contacts"
    icon = "👥"
    help = "Known contacts — last heard, packets, type"
    category = "Message"
    order = 40  # the address book the three above pick their recipient from

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render the contacts list.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing the number of known contacts.
        """
        from ..ui.surface import TuiUi
        from ..ui.widgets import ContactsSort, ordered_contacts

        # Read through the session cache: on a busy node the contacts table is a slow
        # round-trip, and re-fetching it (plus self-info) on every menu visit is a chief cause
        # of sluggish navigation. The cache holds them for the session and refreshes contacts
        # in the background (see :class:`~meshterm.services.device_state.DeviceState`).
        info = await ctx.devstate.self_info()
        contacts = await ctx.devstate.contacts()

        # The path-hash prefix — the leading slice of a key a forced path addresses — is
        # ``mode + 1`` bytes wide; highlight it in every key. The mode is an optional read,
        # so fall back to no highlighting when the firmware doesn't report it.
        try:
            mode = await ctx.devstate.path_hash_mode()
        except Exception:  # noqa: BLE001 - optional read; absence just skips highlighting
            mode = None
        prefix_bytes = (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0

        # Overheard-packet tallies from background monitoring, keyed by node hash; a
        # contact we've never passively overheard simply has no entry.
        counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}

        self_name = str(info.get("name") or "this node")
        self_key = str(info.get("public_key") or "")
        # Opens most-recently-heard first: the list's job is "who's out there right now",
        # and a freshest-first order answers that on sight — an A→Z roll call doesn't. The
        # Ctrl+arrows re-sort from there (and ``--sort`` picks the CLI's order).
        sort_name = str(params.get("sort") or "heard")

        # In the menu, hand the list to the interactive screen so the Ctrl+arrows re-sort it
        # live — its ring spans the shared contact list's four columns (hash included); on the
        # scripted CLI, print the plain listing once in the requested order.
        if isinstance(ctx.ui, TuiUi):
            from ..ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
            from ..ui.contacts_screen import open_contacts

            sort = ContactsSort.from_name(sort_name, SORT_COLUMNS, SORT_OPENS_ASCENDING)
            await open_contacts(ctx, self_name, self_key, contacts, prefix_bytes, counts, sort)
            return ToolResult(summary={"contacts": len(contacts)})

        sort = ContactsSort.from_name(sort_name)
        listing = _listing(ordered_contacts(contacts, counts, sort), counts, prefix_bytes)
        return ToolResult(
            summary={"contacts": len(contacts)},
            report=(listing,),
            exit_code=exitcodes.OK if contacts else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``contacts`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command
        from ..ui.widgets import ContactsSort

        @app.command(name=self.name, help=self.help)
        def _contacts(
            sort: str = typer.Option(
                "heard", "--sort", "-s", help="Order contacts by: heard, name, packets"
            ),
        ) -> None:
            # `ContactsSort.from_name` falls back to the first column for an unknown name,
            # deliberately, so a saved preference written by an older build cannot wedge
            # the menu's list. On the command line the same silence hides a typo: the
            # caller asked for one order and got another, with nothing said.
            if sort not in ContactsSort.names():
                choices = ", ".join(ContactsSort.names())
                raise typer.BadParameter(f"--sort must be one of: {choices} (got {sort!r})")
            run_tool_command(self, {"sort": sort})


def _listing(contacts: list[Contact], counts: dict[str, int], prefix_bytes: int) -> Listing:
    """The contact list, stated once for both faces.

    ``TYPE`` is the node's advertised role in words, where the menu draws a coloured glyph:
    a monochrome ``▲`` would need a legend and the CLI has no legends. ``HEARD`` is a
    relative age, because "recently?" is the question this listing is opened to answer.
    ``HASH`` is the token ``--path`` and ``--to`` take — it used to have to be sliced out
    of ``KEY`` by hand — and ``LOCATION`` is a fact the model has always carried and no
    column had room for. ``KEY`` stays full and last, so it runs off the right harmlessly
    and is never elided: a truncated key is not something a caller can hand back.

    Args:
        contacts: The contacts, already in the requested order.
        counts: Overheard-packet tallies keyed by node id (from passive monitoring).
        prefix_bytes: The device's path-hash width, which is what makes ``HASH`` the
            token a forced path addresses this node by rather than an arbitrary slice.

    Returns:
        The listing.
    """
    from ..ui import fields
    from ..ui.report import Listing
    from ..ui.widgets import contact_packets

    rows = []
    for contact in contacts:
        key = (contact.public_key or "").lower()
        rows.append(
            {
                "node": fields.NodeRef(
                    name=contact.name,
                    key=key or None,
                    hash=(key or contact.key_prefix.lower())[: prefix_bytes * 2] or None,
                    type=NODE_TYPE_LABELS.get(contact.node_type),
                ),
                "heard_at": contact.last_seen,
                "packets": contact_packets(contact, counts),
                "position": (
                    fields.Position(contact.lat, contact.lon) if contact.has_location else None
                ),
            }
        )
    return Listing(
        key="contacts",
        columns=(
            fields.node(
                "node",
                lanes=(("name", "NAME"), ("type", "TYPE"), ("hash", "HASH"), ("key", "KEY")),
            ),
            fields.when("heard_at", "HEARD", absent="never"),
            fields.integer("packets", "PKTS"),
            fields.position(),
        ),
        rows=rows,
        # The node's four lanes are not contiguous: what a reader scans for — who, what,
        # how recently, how much — comes first, and the two long hex fields go right.
        order=("NAME", "TYPE", "HEARD", "PKTS", "HASH", "LOCATION", "KEY"),
    )
