"""The Courier screens: the outbox, queueing flow, and per-message actions.

The interactive face of the ``courier`` tool. One select-list screen carries the whole
feature (the persistent-backdrop pattern): the waiting outbox — each entry with its
live state (waiting to hear the node, scheduled for a time, backing off between
retries) — the finished history (delivered / given-up), and the queueing flow: pick a
contact, write the message, choose when. Enter on a waiting entry offers *Send now*
(one forced attempt, outcome in a dialog) and *Cancel*.

Delivery itself happens in the background service (:mod:`meshterm.services.courier`),
which the menu starts with the other always-on services — this screen never needs to
stay open for a queued message to go out.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from rich.text import Text

from ..core.courier_store import DELIVERED, GAVE_UP, QUEUED, QueuedMessage
from ..core.models import Contact, utcnow
from .tui import Choice, Separator
from .watchtower_screen import contact_watch_key
from .widgets import _DEFAULT_GLYPH, _NODE_GLYPHS, _age_seconds, _format_age, _recency_style

if TYPE_CHECKING:
    from ..context import AppContext

# Menu action sentinels (tuples so they never collide with entry ids).
_QUEUE = ("queue",)
_CLEAR = ("clear",)

#: The widest a message body renders in a row before it is ellipsized.
_TEXT_W = 36


def _shorten(text: str, width: int = _TEXT_W) -> str:
    """The message body shortened for row display."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _local_stamp(when: datetime) -> str:
    """A compact local timestamp: ``Jul 12 07:00``."""
    return when.astimezone().strftime("%b %d %H:%M")


def parse_clock(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a local ``HH:MM`` into its *next* occurrence, as aware UTC.

    ``07:00`` typed at 23:40 means tomorrow morning; typed at 06:00 it means an hour
    from now. Returns ``None`` for anything that isn't a plausible clock time.

    Args:
        text: The typed time.
        now: The reference time (defaults to the current time; tests inject).

    Returns:
        The next occurrence as an aware UTC datetime, or ``None``.
    """
    match = re.fullmatch(r"\s*(\d{1,2})[:h](\d{2})\s*", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    now = now or utcnow()
    local = now.astimezone()
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


async def open_courier(ctx: "AppContext") -> Optional[dict[str, Any]]:
    """Run the Courier screen until dismissed.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Returns:
        A summary of what happened (for the tool's log), or ``None`` on plain exit.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi
    from .tui import CANCEL, SelectScreen

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the courier is only available in the menu")
    session = ctx.ui.session
    await ctx.courier.start()  # idempotent; normally already running

    contacts: list[Contact] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            device = await ctx.device()
            contacts = await device.get_contacts()
    except Exception:  # noqa: BLE001 - the outbox renders fine without contacts
        contacts = []

    store = ctx.courier_store
    loop = asyncio.get_running_loop()
    cursor: Any = None
    while True:
        items = _menu_items(ctx, store.entries())
        menu = SelectScreen(
            "📨 Courier — store-and-forward outbox",
            items,
            default=cursor,
            wrap=False,
            footer_hint="↑↓ move · Enter select · Esc back",
        )
        menu.future = loop.create_future()
        session.push(menu)
        try:
            choice = await menu.future
            if choice in (None, CANCEL):
                return {"queued": store.pending_count()}
            cursor = choice
            if choice == _QUEUE:
                await _queue_flow(ctx, contacts)
            elif choice == _CLEAR:
                store.clear_done()
                cursor = None  # the row itself disappears
            elif isinstance(choice, tuple) and choice[0] == "msg":
                await _entry_actions(ctx, int(choice[1]))
        finally:
            session.pop(menu)


# --- the menu ---------------------------------------------------------------------------


def _menu_items(ctx: "AppContext", entries: list[QueuedMessage]) -> list:
    """Build the screen's rows: the waiting outbox, then the finished history."""
    waiting = [m for m in entries if m.status == QUEUED]
    done = [m for m in entries if m.status != QUEUED]

    items: list = [Separator("Outbox", style="accent")]
    if not waiting:
        items.append(Separator("  empty — queued messages wait here for their moment"))
    for message in waiting:
        items.append(Choice(_waiting_row(ctx, message), ("msg", message.ident)))
    items.append(Choice("✉ Queue a message…", _QUEUE))

    if done:
        items.append(Separator(""))
        items.append(Separator("Finished", style="accent"))
        for message in done[:15]:
            items.append(Choice(_done_row(message), ("msg", message.ident)))
        items.append(Choice("🗑 Clear finished", _CLEAR))
    return items


def _waiting_row(ctx: "AppContext", message: QueuedMessage) -> Text:
    """One waiting entry: recipient, body, and what it is waiting for."""
    row = Text()
    row.append("⏳ ", style="warn")
    row.append(message.node_name)
    row.append(f'  “{_shorten(message.text)}”', style="muted")
    row.append("  ·  ", style="muted")
    now = utcnow()
    if message.not_before is not None and now < message.not_before:
        row.append(f"scheduled {_local_stamp(message.not_before)}", style="brand")
    else:
        retry = ctx.courier.next_retry_s(message, now)
        if retry is not None:
            row.append(
                f"try {message.attempts} · retry in ~{max(1, round(retry / 60))} m",
                style="warn",
            )
        elif message.attempts > 0:
            row.append(f"try {message.attempts} · waiting to hear it again", style="muted")
        elif ctx.courier.heard_recently(message.node_key, now):
            row.append("node is fresh — next pass", style="ok")
        else:
            row.append("waiting to hear the node", style="muted")
    return row


def _done_row(message: QueuedMessage) -> Text:
    """One finished entry: outcome marker, recipient, body, and when it settled."""
    row = Text()
    if message.status == DELIVERED:
        row.append("✓ ", style="ok")
    else:
        row.append("✗ ", style="err")
    row.append(message.node_name, style="muted")
    row.append(f'  “{_shorten(message.text)}”', style="muted")
    when = message.finished or message.created
    verb = "delivered" if message.status == DELIVERED else "gave up"
    row.append(
        f"  ·  {verb} {_format_age(_age_seconds(when))} ago"
        f" · {message.attempts} attempt{'s' if message.attempts != 1 else ''}",
        style="muted",
    )
    return row


# --- the flows --------------------------------------------------------------------------


async def _queue_flow(ctx: "AppContext", contacts: list[Contact]) -> None:
    """Float the queueing flow: recipient, message, schedule."""
    session = ctx.ui.session
    if not contacts:
        await session.message_dialog(
            Text(
                "No contacts available — connect a device that knows some nodes first.",
                style="muted",
            ),
            title="queue a message",
        )
        return

    def recency(contact: Contact) -> float:
        seen = _age_seconds(contact.last_seen)
        return seen if seen is not None else float("inf")

    # The picker's shared presentation: the per-type glyph in the app's marker
    # palette, the name coloured by recency heat (hotter = heard more recently),
    # exactly as the Nodes list and the Time Machine picker draw contacts.
    items: list = []
    for contact in sorted(contacts, key=recency):
        glyph, glyph_style = _NODE_GLYPHS.get(contact.node_type, _DEFAULT_GLYPH)
        secs = _age_seconds(contact.last_seen)
        row = Text()
        row.append(glyph + " ", style=glyph_style)
        row.append(contact.name, style=_recency_style(secs))
        row.append(f"   heard {_format_age(secs)}", style="muted")
        items.append(Choice(row, contact))
    contact = await session.select(
        "✉ Courier — recipient",
        items,
        prompt="The message waits in the outbox until this node can take it:",
    )
    if contact is None:
        return
    text = await session.text(
        f"Message for {contact.name}",
        prompt="Delivered as a normal direct message when its moment comes.",
    )
    if not text:
        return
    not_before = await _pick_schedule(ctx, contact.name)
    if not_before is CANCEL_SCHEDULE:
        return
    key = contact_watch_key(contact)
    if key is None:
        await session.message_dialog(
            Text(f"{contact.name!r} has no usable key to address.", style="err"),
            title="queue a message",
        )
        return
    ctx.courier_store.queue(key, contact.name, text, not_before=not_before)


#: Sentinel: the schedule picker was cancelled (distinct from "no schedule").
CANCEL_SCHEDULE = object()


async def _pick_schedule(ctx: "AppContext", name: str):
    """Float the when-to-send picker; return an aware UTC time, ``None``, or cancel."""
    session = ctx.ui.session
    now = utcnow()
    tomorrow_7 = parse_clock("07:00", now)
    items = [
        Choice("When it's next heard  (recommended)", None),
        Choice("In 1 h", now + timedelta(hours=1)),
        Choice("In 3 h", now + timedelta(hours=3)),
        Choice("In 8 h", now + timedelta(hours=8)),
        Choice(f"Next 07:00  ({_local_stamp(tomorrow_7)})", tomorrow_7),
        Choice("At a time… (HH:MM, next occurrence)", "custom"),
    ]
    picked = await session.select(
        f"When should {name} get it?", items, filterable=False,
        footer_hint="↑↓ move · Enter choose · Esc cancel",
    )
    if picked == "custom":
        while True:
            typed = await session.text(
                "Send at (local HH:MM)",
                prompt="A time already past today means tomorrow.",
            )
            if not typed:
                return CANCEL_SCHEDULE
            when = parse_clock(typed)
            if when is not None:
                return when
    if picked is None:
        # Esc on the picker cancels the queueing; "when next heard" is the explicit
        # first row, so backing out never silently queues something.
        return CANCEL_SCHEDULE
    return picked


async def _entry_actions(ctx: "AppContext", ident: int) -> None:
    """Float one entry's action menu: send now, cancel, or just look at it."""
    session = ctx.ui.session
    store = ctx.courier_store
    message = store.get(ident)
    if message is None:
        return
    if message.status != QUEUED:
        # A finished entry has no actions; show its full text instead.
        body = Text(message.text)
        body.append(
            f"\n\n{message.status} · {message.attempts} attempt"
            f"{'s' if message.attempts != 1 else ''}",
            style="muted",
        )
        await session.message_dialog(body, title=f"📨 {message.node_name}")
        return
    items = [
        Choice("📤 Send now — one forced attempt", "send"),
        Choice("✖ Cancel this message", "cancel"),
    ]
    picked = await session.select(
        f"📨 {message.node_name} — “{_shorten(message.text, 28)}”",
        items, filterable=False,
        footer_hint="↑↓ move · Enter select · Esc back",
    )
    if picked == "cancel":
        store.cancel(ident)
    elif picked == "send":
        try:
            async with ctx.ui.busy_overlay(f"sending to {message.node_name}…"):
                outcome = await ctx.courier.attempt_now(ident)
        except Exception as exc:  # noqa: BLE001 - surface the failure, keep the queue
            await session.message_dialog(
                Text(f"send failed: {exc}", style="err"), title="courier"
            )
            return
        notes = {
            "delivered": Text("✓ delivered — acknowledged by the node", style="ok"),
            "no ack": Text(
                "sent, but no acknowledgement — it stays queued and the courier "
                "will retry with backoff", style="warn",
            ),
            "gave up": Text("no acknowledgement — the retry budget is spent", style="err"),
            "unknown contact": Text(
                "the device's contact list doesn't know this node yet; "
                "it stays queued", style="warn",
            ),
            "busy": Text("another delivery is in flight — try again in a moment", style="muted"),
            "gone": Text("this entry is no longer queued", style="muted"),
        }
        await session.message_dialog(
            notes.get(outcome, Text(outcome)), title="courier"
        )
