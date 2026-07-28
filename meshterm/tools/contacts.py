"""The ``contacts`` tool: list this node and the contacts it knows about."""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class ContactsTool(Tool):
    """List this node and its known contacts, each with its path-hash prefix."""

    name = "contacts"
    title = "Contacts"
    icon = "👥"
    help = "List this node and known contacts (last heard, packets, type, key)"
    category = "Mesh"
    order = 10  # right under the Dashboard: the "who's out there" view

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device and render the contacts list.

        Args:
            ctx: Shared application context.
            params: Unused.

        Returns:
            A :class:`ToolResult` summarizing the number of known contacts.
        """
        from ..ui.surface import TuiUi
        from ..ui.widgets import ContactsSort, contacts_table

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
        # scripted CLI, render the static table once in the requested order.
        if isinstance(ctx.ui, TuiUi):
            from ..ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
            from ..ui.contacts_screen import open_contacts

            sort = ContactsSort.from_name(sort_name, SORT_COLUMNS, SORT_OPENS_ASCENDING)
            await open_contacts(ctx, self_name, self_key, contacts, prefix_bytes, counts, sort)
        else:
            sort = ContactsSort.from_name(sort_name)
            ctx.ui.show(contacts_table(self_name, self_key, contacts, prefix_bytes, counts, sort))

        return ToolResult(summary={"contacts": len(contacts)})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``contacts`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _contacts(
            sort: str = typer.Option(
                "heard", "--sort", "-s", help="Order contacts by: heard, name, packets"
            ),
        ) -> None:
            run_tool_command(self, {"sort": sort})
