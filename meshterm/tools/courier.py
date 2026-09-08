"""The ``courier`` tool: store-and-forward messaging for contacts that aren't there yet.

Interactively it opens the outbox screen (see :mod:`meshterm.ui.courier_screen`):
queued messages with their live state, the queueing flow (recipient → message →
when), forced sends, and the delivered/given-up history. Delivery runs in the
background for the whole session (see :mod:`meshterm.services.courier`): a queued
message goes out when its contact is next heard — or at its scheduled time — as a normal
direct message with acknowledgement tracking and polite exponential backoff, and the
outcome lights the header's Watchtower badge.

On the CLI the same outbox is scriptable through subcommands: ``queue`` adds a message
(reading the contact list to address it — nothing is transmitted) for the next
interactive session's courier to deliver, ``send`` forces one delivery attempt right now,
and ``list`` / ``cancel`` / ``clear`` inspect and prune the outbox — the scriptable face
of the outbox screen's own Send-now, Cancel, and Clear-finished actions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.connection import DeviceCommandError
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.courier_store import QueuedMessage
    from ..ui.report import Facts, Listing


@register
class CourierTool(Tool):
    """Queue messages for offline contacts; they go out when the contact is next heard."""

    name = "courier"
    title = "Courier"
    icon = "📨"
    help = "Outbox that delivers when a contact is heard"
    category = "Message"
    order = 30  # after Chat and Channels: the same conversations, minus the waiting around

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the outbox screen; there are never parameters to collect.

        The screen presents everything itself and returns when dismissed, so
        returning ``None`` tells the menu the invocation is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.courier_screen import open_courier

        await open_courier(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Dispatch a scripted CLI action (the menu path lives in :meth:`prompt_params`).

        Args:
            ctx: Shared application context.
            params: A ``cli_action`` naming the outbox action, plus its arguments.

        Returns:
            A :class:`ToolResult` describing the action's outcome.
        """
        action = params.get("cli_action", "queue")
        if action == "list":
            return await self._cli_list(ctx)
        if action == "send":
            return await self._cli_send(ctx, params)
        if action == "cancel":
            return await self._cli_cancel(ctx, params)
        if action == "clear":
            return await self._cli_clear(ctx)
        return await self._cli_queue(ctx, params)

    # -- CLI --------------------------------------------------------------------

    async def _cli_queue(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Queue one message from the CLI (transmits nothing).

        Args:
            ctx: Shared application context.
            params: ``contact`` (contact name), ``text`` (the message body), and an
                optional local ``at`` clock time (``HH:MM``, next occurrence).

        Returns:
            A :class:`ToolResult` describing the queued entry.
        """
        from ..ui.courier_screen import parse_clock
        from ..ui.watchtower_screen import contact_watch_key

        device = await ctx.device()
        contacts = await device.get_contacts()
        name = str(params["contact"])
        needle = name.casefold()
        contact = next((c for c in contacts if c.name.casefold() == needle), None)
        if contact is None:
            raise DeviceCommandError(f"unknown contact: {name!r}")
        key = contact_watch_key(contact)
        if key is None:
            raise DeviceCommandError(f"{name!r} has no usable key to address")

        not_before = None
        if params.get("at"):
            not_before = parse_clock(str(params["at"]))
            if not_before is None:
                raise DeviceCommandError(
                    f"--at wants a clock time like 07:00, not {params['at']!r}"
                )
        message = ctx.courier_store.queue(
            key, contact.name, str(params["text"]), not_before=not_before
        )
        when = (
            f"at {not_before.astimezone().strftime('%b %d %H:%M')}"
            if not_before is not None
            else "when the contact is next heard"
        )
        ctx.ui.ack(
            f"[ok]queued[/ok] #{message.ident} for [brand]{contact.name}[/brand] — "
            f"delivers {when} (an interactive session's courier does the sending)"
        )
        return ToolResult(
            summary={"queued": message.ident, "contact": contact.name, "at": params.get("at")},
            report=(_queued(message, contact.name, not_before),),
        )

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """State the outbox — waiting entries then finished ones.

        A read-only view over the stored outbox: it needs no device, so it works the same
        whether or not a radio is attached (the scriptable face of the outbox screen).
        """
        entries = ctx.courier_store.entries()
        return ToolResult(
            summary={"entries": len(entries)},
            report=(_outbox(entries),),
            exit_code=exitcodes.OK if entries else exitcodes.NO_RESULT,
        )

    async def _cli_send(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Force one delivery attempt for a waiting entry, right now.

        The scriptable ``Send now`` — one forced attempt through the same courier the
        outbox screen uses, skipping the "wait until the contact is heard" eligibility check.
        """
        ident = int(params["id"])
        if ctx.courier_store.get(ident) is None:
            raise DeviceCommandError(f"no outbox entry #{ident}")
        outcome = await ctx.courier.attempt_now(ident)
        notes = {
            "delivered": "[ok]✓ delivered[/ok] — acknowledged by the contact",
            "no ack": "[warn]sent, but no acknowledgement[/warn] — it stays queued for retry",
            "gave up": "[err]✗ gave up[/err] — the retry budget is spent",
            "unknown contact": (
                "[warn]the device's contacts don't know this contact yet[/warn] — it stays queued"
            ),
            "busy": "[muted]another delivery is in flight — try again in a moment[/muted]",
            "gone": "[muted]that entry is no longer waiting[/muted]",
        }
        ctx.ui.ack(notes.get(outcome, outcome))
        return ToolResult(
            summary={"id": ident, "outcome": outcome},
            report=(_attempted(ident, outcome),),
        )

    async def _cli_cancel(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Remove a waiting entry from the outbox (finished ones use ``clear``)."""
        ident = int(params["id"])
        if ctx.courier_store.cancel(ident):
            ctx.ui.ack(f"[warn]cancelled outbox entry #{ident}[/warn]")
            return ToolResult(
                summary={"id": ident, "cancelled": True},
                report=(_did(("id", ident), ("cancelled", True)),),
            )
        ctx.ui.ack(f"[muted]no waiting entry #{ident} to cancel[/muted]")
        return ToolResult(
            summary={"id": ident, "cancelled": False},
            report=(_did(("id", ident), ("cancelled", False)),),
            exit_code=exitcodes.NO_RESULT,
        )

    async def _cli_clear(self, ctx: AppContext) -> ToolResult:
        """Drop every finished (delivered / given-up) entry, leaving the queue untouched."""
        before = len(ctx.courier_store.entries())
        ctx.courier_store.clear_done()
        cleared = before - len(ctx.courier_store.entries())
        if cleared:
            ctx.ui.ack(f"[ok]cleared {cleared} finished entr{'y' if cleared == 1 else 'ies'}[/ok]")
        else:
            ctx.ui.ack("[muted]no finished entries to clear[/muted]")
        return ToolResult(
            summary={"cleared": cleared},
            report=(_did(("cleared", cleared)),),
            exit_code=exitcodes.OK if cleared else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``courier`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        courier_app = typer.Typer(help=self.help, no_args_is_help=True, rich_markup_mode=None)

        @courier_app.command(
            "queue", help="Queue a message for delivery when the contact is next heard"
        )
        def _queue_cmd(
            contact: str = typer.Argument(..., help="The recipient contact's name"),
            text: list[str] = typer.Argument(..., help="The message to queue"),
            at: str | None = typer.Option(
                None, "--at", help="Hold until this local time (HH:MM, next occurrence)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {
                "cli_action": "queue",
                "contact": contact,
                "text": " ".join(text),
            }
            if at is not None:
                tool_params["at"] = at
            run_tool_command(self, tool_params)

        @courier_app.command("list", help="Show the outbox — waiting and finished entries")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @courier_app.command("send", help="Force one delivery attempt for a waiting entry now")
        def _send_cmd(
            id: int = typer.Argument(..., help="Outbox entry id (from `courier list`)"),
        ) -> None:
            run_tool_command(self, {"cli_action": "send", "id": id})

        @courier_app.command("cancel", help="Remove a waiting entry from the outbox")
        def _cancel_cmd(
            id: int = typer.Argument(..., help="Outbox entry id (from `courier list`)"),
        ) -> None:
            run_tool_command(self, {"cli_action": "cancel", "id": id})

        @courier_app.command("clear", help="Drop finished (delivered / given-up) entries")
        def _clear_cmd() -> None:
            run_tool_command(self, {"cli_action": "clear"})

        app.add_typer(courier_app, name=self.name)


def _recipient(name: str | None, node_key: str | None) -> object:
    """The outbox's addressee as the shared node shape.

    The outbox stores a name and the 12-hex id it addresses, so three of the five fields
    are honestly ``null`` — and the id goes in ``hash`` rather than ``key``, because that
    is what it is. A key is the full value and is never truncated by this app's choosing;
    calling a six-byte prefix one would hand a caller something ``--to`` cannot take.
    """
    from ..ui.fields import NodeRef

    return NodeRef(name=name, hash=(node_key or "").lower() or None)


def _queued(message: QueuedMessage, name: str, not_before: object) -> Facts:
    """What ``courier queue`` put in the outbox.

    The plain face prints the **id and nothing else**, because the id is the one fact a
    caller has to keep — it is what ``send`` and ``cancel`` take — and everything else on
    the line is what they just typed. The document carries the rest, because a caller
    logging the queue has no other record of it.
    """
    from ..ui import fields
    from ..ui.report import Facts

    return Facts(
        key="queued",
        fields=(
            fields.integer("id", "id"),
            fields.node("node", lanes=()),
            fields.hidden("scheduled_at"),
            fields.hidden("text"),
        ),
        values={
            "id": message.ident,
            "node": _recipient(name, message.node_key),
            "scheduled_at": not_before,
            "text": message.text,
        },
    )


def _outbox(entries: list) -> Listing:
    """The outbox, waiting entries then finished ones.

    The deliberate asymmetry between the two time columns: ``SCHEDULED`` is an
    *appointment* the caller set with ``--at``, so it stays a wall-clock instant, and
    ``FINISHED`` is an age, because "how long ago did it go" is what anyone reading a
    delivered row wants. The mixed widths are the price of both columns being right, and
    ``--absolute`` makes them uniform for anyone who dislikes it.
    """
    from ..core.courier_store import DELIVERED, QUEUED
    from ..ui import fields
    from ..ui.report import Listing

    rows = []
    for m in entries:
        if m.status == QUEUED:
            state = "waiting"
        elif m.status == DELIVERED:
            state = "delivered"
        else:
            state = "gave up"
        rows.append(
            {
                "id": m.ident,
                "state": state,
                "node": _recipient(m.node_name, m.node_key),
                # An entry with no scheduled time goes as soon as the contact is heard,
                # which is not a time and so is not one here either.
                "scheduled_at": m.not_before,
                "finished_at": m.finished,
                # Not shortened the way the outbox screen shortens it: nothing wraps here.
                "text": m.text,
            }
        )
    return Listing(
        key="outbox",
        columns=(
            fields.integer("id", "ID"),
            fields.word("state", "STATE"),
            fields.node("node", lanes=(("name", "NODE"),)),
            fields.instant("scheduled_at", "SCHEDULED"),
            fields.when("finished_at", "FINISHED"),
            fields.free("text", "TEXT"),
        ),
        rows=rows,
    )


def _attempted(ident: int, outcome: str) -> Facts:
    """What one forced delivery attempt came to.

    ``outcome`` is one of the closed set the help lists, kept verbatim — spaces and all —
    so both faces say the same word and a script matching ``no ack`` matches what a person
    reads.
    """
    from ..ui import fields
    from ..ui.report import Facts

    return Facts(
        key="attempt",
        fields=(fields.hidden("id"), fields.word("outcome", "outcome")),
        values={"id": ident, "outcome": outcome},
    )


def _did(*facts: tuple[str, object]) -> Facts:
    """An acknowledgement as data: nothing plain, a document for whoever is logging.

    ``cancel`` and ``clear`` say everything they have to say in their exit status, which
    is the right plain answer — but a caller that wants to record *what* was cancelled has
    nowhere to read it, and that is what the document is for.
    """
    from ..ui import fields
    from ..ui.report import SILENT, Facts

    def column(key: str, value: object):  # noqa: ANN202 - one of two shapes
        return fields.flag(key, key) if isinstance(value, bool) else fields.integer(key, key)

    return Facts(
        key="outcome",
        fields=tuple(column(key, value) for key, value in facts),
        values=dict(facts),
        shape=SILENT,
    )
