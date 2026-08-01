"""The Courier screens: the outbox, queueing flow, and per-message actions.

The interactive face of the ``courier`` tool. One select-list screen carries the whole
feature (the persistent-backdrop pattern): the waiting outbox — each entry with its
live state (waiting to hear the contact, scheduled for a time, backing off between
retries) — the finished history (delivered / given-up), and the queueing flow: pick a
contact, write the message, choose when. Enter on a waiting entry offers *Send now*
(one forced attempt, outcome in a dialog) and *Cancel*.

The outbox is **live** while it sits open: every row is a callable title recomputed on
each repaint (so "retry in ~N m" counts down for real), and a once-a-second ticker
compares the store's shape — entry set and statuses — rebuilding the sections in place
when something moved (a delivery lands its row in *Finished* within a second, no
keypress needed), keeping the highlight on its entry.

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

from ..core.courier_store import DELIVERED, QUEUED, QueuedMessage
from ..core.models import Contact, is_direct_messageable, utcnow
from .menus import back_rows, section_heading
from .contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING, ContactListScreen, ContactRow
from .theme import glyph
from .tui import CANCEL, DM_BYTE_LIMIT, Choice, SelectScreen, Separator
from .watchtower_screen import contact_watch_key
from .widgets import ContactsSort, _age_seconds, _contact_pkts, format_ago

if TYPE_CHECKING:
    from ..context import AppContext

# Menu action sentinels (tuples so they never collide with entry ids).
_QUEUE = ("queue",)
_CLEAR = ("clear",)

#: Seconds between the open outbox's refresh ticks (shape check + repaint).
_REFRESH_S = 1.0

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

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the courier is only available in the menu")
    session = ctx.ui.session
    await ctx.courier.start()  # idempotent; normally already running

    contacts: list[Contact] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the outbox renders fine without contacts
        contacts = []

    store = ctx.courier_store
    loop = asyncio.get_running_loop()
    cursor: Any = None
    while True:
        menu = CourierOutboxScreen(ctx, default=cursor)
        menu.future = loop.create_future()
        session.push(menu)

        async def tick(screen: "CourierOutboxScreen" = menu) -> None:
            """Fold store changes in and repaint, once a second, while the list is open."""
            while True:
                await asyncio.sleep(_REFRESH_S)
                screen.refresh()
                session.invalidate()

        ticker = asyncio.ensure_future(tick())
        try:
            choice = await menu.future
            if choice in (None, CANCEL):
                return {"queued": store.pending_count()}
            cursor = choice
            if choice == _QUEUE:
                await _queue_flow(ctx, contacts)
            elif choice == _CLEAR:
                done = store.done_count()
                if await ctx.ui.dialog(
                    f"Clear {done} finished "
                    f"{'entry' if done == 1 else 'entries'} from the history?",
                    [("Cancel", False), ("Clear", True)],
                    title="Clear finished",
                    default=1,
                    destructive=True,
                ):
                    store.clear_done()
                    cursor = None  # the row itself disappears
            elif isinstance(choice, tuple) and choice[0] == "msg":
                await _entry_actions(ctx, int(choice[1]))
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - teardown must never surface a tick hiccup
                pass
            session.pop(menu)


# --- the menu ---------------------------------------------------------------------------


class CourierOutboxScreen(SelectScreen):
    """The live outbox list: row text recomputes per repaint, sections per tick.

    Rows are callable titles (see :attr:`~meshterm.ui.tui.select.Choice.title`), so
    every repaint re-reads each entry's live state — the retry countdown, the fresh/
    waiting note, a finished row's age. Structure changes (an entry moving waiting →
    finished, a new queue, a cleared history) can't be expressed by a row re-rendering
    itself, so the opener's ticker calls :meth:`refresh`: it fingerprints the store's
    shape and recomposes the sections in place only when that changed, keeping the
    highlight on its entry (the :class:`~meshterm.ui.contactlist.ContactListScreen` rebuild
    idiom).
    """

    def __init__(self, ctx: "AppContext", *, default: Any = None) -> None:
        self._ctx = ctx
        self._shape = self._fingerprint()
        super().__init__(
            "Courier — store-and-forward outbox",
            _menu_items(ctx, ctx.courier_store.entries()),
            default=default,
            wrap=False,
            footer_hint="↑↓ move · Enter select · Esc back",
        )

    def _fingerprint(self) -> tuple:
        """The store's shape: which entries exist and what status each is in."""
        return tuple((m.ident, m.status) for m in self._ctx.courier_store.entries())

    def refresh(self) -> None:
        """Recompose the sections if the store's shape changed, keeping the highlight."""
        shape = self._fingerprint()
        if shape == self._shape:
            return
        self._shape = shape
        current = self._current_choice()
        keep = current.value if current is not None else None
        self._items = _menu_items(self._ctx, self._ctx.courier_store.entries())
        self._reselect(keep)

    def _reselect(self, value: Any) -> None:
        """Move the highlight back onto the choice with ``value`` (else clamp in range)."""
        choices = self._choices()
        for i, choice in enumerate(choices):
            if choice.value == value:
                self._index = i
                return
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0


def _menu_items(ctx: "AppContext", entries: list[QueuedMessage]) -> list:
    """Build the screen's rows: the waiting outbox, then the finished history.

    Entry rows are zero-arg callables so their live state re-renders every repaint;
    the fixed action rows stay plain strings.
    """
    waiting = [m for m in entries if m.status == QUEUED]
    done = [m for m in entries if m.status != QUEUED]

    items: list = [section_heading("Outbox")]
    if not waiting:
        items.append(Separator("  empty — queued messages wait here for their moment"))
    for message in waiting:
        items.append(
            Choice(lambda m=message: _waiting_row(ctx, m), ("msg", message.ident))
        )
    items.append(Separator(" "))  # space the action off the outbox rows above it
    items.append(Choice(f"{glyph('📨')} Queue a message…", _QUEUE))

    if done:
        items.append(Separator(" "))
        items.append(section_heading("Finished"))
        for message in done[:15]:
            items.append(Choice(lambda m=message: _done_row(m), ("msg", message.ident)))
        items.append(Choice(f"{glyph('🗑')} Clear finished", _CLEAR))

    items.extend(back_rows())  # a visible exit beside Esc
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
            row.append("contact is fresh — next pass", style="ok")
        else:
            row.append("waiting to hear the contact", style="muted")
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
        f"  ·  {verb} {format_ago(_age_seconds(when))}"
        f" · {message.attempts} attempt{'s' if message.attempts != 1 else ''}",
        style="muted",
    )
    return row


# --- the flows --------------------------------------------------------------------------


#: The recipient picker's footer: the shared contact-list grammar with a committing Enter,
#: Esc cancelling the queueing step it sits in.
_PICK_HINT = "↑↓ move · ^←→↑↓ sort · type to filter · Enter select · Esc cancel"


class CourierRecipientScreen(ContactListScreen):
    """The recipient picker on the shared contact list.

    The full ``NAME · HEARD · PKTS · KEY`` lanes, the Ctrl+arrow sort ring, and
    type-to-filter, exactly as the Contacts screen and the Time Machine picker draw
    contacts — names in their key-derived hue, heard ages in recency heat. Unlike the
    Contacts screen, Enter *commits*: the shared list's Enter resolves the highlighted
    row's value, which is the :class:`~meshterm.core.models.Contact` itself.

    Only companion contacts are ever passed in — a courier message is a direct message,
    and direct messages go to companions only (see
    :func:`~meshterm.core.models.is_direct_messageable`); the caller filters before
    building the picker.
    """

    def __init__(
        self,
        *,
        contacts: list[Contact],
        prefix_bytes: int,
        counts: dict[str, int],
        sort: ContactsSort,
    ) -> None:
        """Build the picker over the device's companion contacts.

        Args:
            contacts: The candidate recipients — companion contacts only (the caller
                filters non-companions out).
            prefix_bytes: The hash width in bytes to light at the head of each key.
            counts: Overheard-packet tallies keyed by lowercased 12-hex node id.
            sort: The sort state (defaults open on ``name``, A→Z).
        """
        rows = [
            ContactRow(
                value=c,
                name=c.name,
                key=c.public_key or c.key_prefix or "",
                node_type=c.node_type,
                last_seen=c.last_seen,
                count=_contact_pkts(c, counts),
            )
            for c in contacts
        ]
        super().__init__(
            "Courier — recipient",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            prompt="The message waits in the outbox until this contact can take it:",
            footer_hint=_PICK_HINT,
        )


async def _queue_flow(ctx: "AppContext", contacts: list[Contact]) -> None:
    """Float the queueing flow: recipient, message, schedule."""
    from .timemachine_screen import _routing_prefix_bytes

    session = ctx.ui.session
    # A courier message is a direct message, so only companions can receive one — a
    # repeater, room, or sensor is never a recipient (the app-wide DM rule, see
    # is_direct_messageable). Filter before the picker so non-companions never appear.
    companions = [c for c in contacts if is_direct_messageable(c.node_type)]
    if not companions:
        await session.message_dialog(
            Text(
                "No companion contacts available — connect a device that knows a "
                "companion to message first.",
                style="muted",
            ),
            title="Queue a message",
        )
        return

    # The shared contact-list presentation (see CourierRecipientScreen), opened A→Z by
    # name — the default of a re-sortable list, with heard/packets/key a Ctrl+arrow away.
    counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}
    prefix_bytes = await _routing_prefix_bytes(ctx)
    picker = CourierRecipientScreen(
        contacts=companions,
        prefix_bytes=prefix_bytes,
        counts=counts,
        sort=ContactsSort.from_name("name", SORT_COLUMNS, SORT_OPENS_ASCENDING),
    )
    contact = await session.run_screen(picker)
    if not isinstance(contact, Contact):  # Esc (CANCEL) or anything else backs out
        return
    text = await session.text(
        f"Message for {contact.name}",
        prompt="Delivered as a normal direct message when its moment comes.",
        byte_limit=DM_BYTE_LIMIT,
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
            title="Queue a message",
        )
        return
    ctx.courier_store.queue(key, contact.name, text, not_before=not_before)


#: Sentinel: the schedule picker was cancelled (distinct from "no schedule").
CANCEL_SCHEDULE = object()

#: Sentinel: "send when it's next heard" — the no-schedule choice. It carries its own
#: value (never ``None``) because :meth:`session.select` already returns ``None`` for a
#: cancel; sharing that value made picking this row read as a cancel and silently drop the
#: message instead of queueing it.
WHEN_HEARD = object()


async def _pick_schedule(ctx: "AppContext", name: str):
    """Float the when-to-send picker; return an aware UTC time, ``None``, or cancel.

    ``None`` means "no schedule — send on the next sign of life" (the explicit
    *When it's next heard* row); an aware UTC datetime holds until then;
    :data:`CANCEL_SCHEDULE` means the user backed out and nothing should queue.
    """
    session = ctx.ui.session
    now = utcnow()
    tomorrow_7 = parse_clock("07:00", now)
    items = [
        Choice("When it's next heard  (recommended)", WHEN_HEARD),
        Choice("In 1 h", now + timedelta(hours=1)),
        Choice("In 3 h", now + timedelta(hours=3)),
        Choice("In 8 h", now + timedelta(hours=8)),
        Choice(f"Next 07:00  ({_local_stamp(tomorrow_7)})", tomorrow_7),
        Choice("At a time… (HH:MM, next occurrence)", "custom"),
    ]
    picked = await session.select(
        f"When should {name} get it?", items, filterable=False,
        footer_hint="↑↓ move · Enter select · Esc cancel",
    )
    if picked is None:
        # Esc on the picker cancels the queueing; "when next heard" is its own explicit
        # row (WHEN_HEARD below), so backing out never silently queues something.
        return CANCEL_SCHEDULE
    if picked is WHEN_HEARD:
        return None  # no schedule constraint — delivered on the next pass
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
        await session.message_dialog(body, title=message.node_name)
        return
    items = [
        Choice("📤 Send now — one forced attempt", "send"),
        Choice(Text.assemble(("✗ ", "err"), "Cancel this message"), "cancel"),
    ]
    picked = await session.select(
        f"{message.node_name} — “{_shorten(message.text, 28)}”",
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
                Text(f"send failed: {exc}", style="err"), title="Courier"
            )
            return
        notes = {
            "delivered": Text("✓ delivered — acknowledged by the contact", style="ok"),
            "no ack": Text(
                "sent, but no acknowledgement — it stays queued and the courier "
                "will retry with backoff", style="warn",
            ),
            "gave up": Text("no acknowledgement — the retry budget is spent", style="err"),
            "unknown contact": Text(
                "the device's contact list doesn't know this contact yet; "
                "it stays queued", style="warn",
            ),
            "busy": Text("another delivery is in flight — try again in a moment", style="muted"),
            "gone": Text("this entry is no longer queued", style="muted"),
        }
        await session.message_dialog(
            notes.get(outcome, Text(outcome)), title="Courier"
        )
