"""Courier tests: the outbox store, delivery eligibility, and the attempt flow.

The service is driven synchronously (eligibility) and through stubbed chat sends
(attempts), so no timers or hardware are involved.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from meshterm.core.courier_store import DONE_CAP, QUEUED, CourierStore
from meshterm.core.models import ChatMessage, Contact, utcnow
from meshterm.core.watch_store import WatchStore
from meshterm.services.courier import FRESH_S, MAX_ATTEMPTS, CourierService
from meshterm.ui.courier_screen import parse_clock

NODE = "3d" * 6
CONTACT = Contact(name="YUL", public_key="3d" * 32)


class _StubChat:
    """Scripted chat service: pops the next ack outcome per send."""

    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[tuple[str, str]] = []

    async def send_direct(self, contact: Contact, text: str) -> ChatMessage:
        self.sent.append((contact.name, text))
        acked = self.outcomes.pop(0) if self.outcomes else False
        return ChatMessage(text=text, outbound=True, acked=acked)


class _StubDevice:
    async def get_contacts(self) -> list[Contact]:
        return [CONTACT]


class _StubContext:
    """Minimal stand-in for :class:`~meshterm.context.AppContext`."""

    def __init__(self, tmp_path: Path, outcomes: list[bool]) -> None:
        self.courier_store = CourierStore(tmp_path / "courier.json")
        self.watch_store = WatchStore(tmp_path / "watchtower.json")
        self.chat = _StubChat(outcomes)
        self.is_connected = True
        self.log = logging.getLogger("test.courier")
        self._device = _StubDevice()

    async def device(self) -> _StubDevice:
        return self._device


def _service(tmp_path: Path, outcomes: list[bool]) -> CourierService:
    return CourierService(_StubContext(tmp_path, outcomes))


# --- the store --------------------------------------------------------------------------


def test_store_queue_round_trips_and_persists(tmp_path: Path) -> None:
    """Queued entries survive a fresh store instance with their schedule intact."""
    path = tmp_path / "courier.json"
    store = CourierStore(path)
    when = utcnow() + timedelta(hours=8)
    queued = store.queue(NODE, "YUL", "hello there", not_before=when)
    assert queued.ident == 1 and queued.status == QUEUED

    again = CourierStore(path)
    entry = again.get(1)
    assert entry is not None and entry.text == "hello there"
    assert entry.not_before == when
    assert again.pending_count() == 1


def test_store_lifecycle_attempts_finish_cancel_clear(tmp_path: Path) -> None:
    """Attempt marks, delivery, cancellation, and clearing all behave."""
    store = CourierStore(tmp_path / "courier.json")
    a = store.queue(NODE, "YUL", "one")
    b = store.queue(NODE, "YUL", "two")
    store.note_attempt(a.ident)
    assert store.get(a.ident).attempts == 1
    store.mark_delivered(a.ident)
    assert store.pending() == [store.get(b.ident)]
    assert store.cancel(b.ident) is True
    assert store.pending_count() == 0
    assert store.get(a.ident) is not None  # delivered history remains
    store.clear_done()
    assert store.entries() == []


def test_store_caps_the_finished_history(tmp_path: Path) -> None:
    """Old finished entries fall off; the waiting queue is never trimmed."""
    store = CourierStore(tmp_path / "courier.json")
    keeper = store.queue(NODE, "YUL", "still waiting")
    for i in range(DONE_CAP + 5):
        entry = store.queue(NODE, "YUL", f"m{i}")
        store.mark_delivered(entry.ident)
    done = [m for m in store.entries() if m.status != QUEUED]
    assert len(done) == DONE_CAP
    assert store.get(keeper.ident) is not None


# --- eligibility -------------------------------------------------------------------------


def test_eligibility_waits_for_freshness_and_schedule(tmp_path: Path) -> None:
    """Plain entries need the node heard; scheduled ones hold until their time."""
    service = _service(tmp_path, [])
    store = service._ctx.courier_store
    now = utcnow()

    plain = store.queue(NODE, "YUL", "hi")
    assert not service.eligible(plain, now)  # never heard this session
    service._heard[NODE] = now - timedelta(seconds=FRESH_S + 1)
    assert not service.eligible(plain, now)  # heard, but too long ago
    service._heard[NODE] = now
    assert service.eligible(plain, now)

    scheduled = store.queue(NODE, "YUL", "later", not_before=now + timedelta(hours=1))
    assert not service.eligible(scheduled, now)  # the schedule holds it
    # Past its time, a scheduled entry's *first* shot fires even unheard-of.
    service._heard.clear()
    assert service.eligible(scheduled, now + timedelta(hours=2))


def test_eligibility_backs_off_after_failures(tmp_path: Path) -> None:
    """A failed attempt waits out its (doubling) backoff even when the node is fresh."""
    service = _service(tmp_path, [])
    store = service._ctx.courier_store
    now = utcnow()
    entry = store.queue(NODE, "YUL", "hi")
    service._heard[NODE] = now
    store.note_attempt(entry.ident, when=now)
    entry = store.get(entry.ident)
    assert not service.eligible(entry, now + timedelta(minutes=2))
    assert service.eligible(entry, now + timedelta(minutes=6))  # 5-min base elapsed
    assert service.next_retry_s(entry, now + timedelta(minutes=2)) is not None


# --- the attempt flow ---------------------------------------------------------------------


async def test_attempt_delivers_and_raises_the_good_news(tmp_path: Path) -> None:
    """An acknowledged send settles the entry and lights the Watchtower badge."""
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "YUL", "hello")
    service._heard[NODE] = utcnow()

    await service._pass()
    assert ctx.chat.sent == [("YUL", "hello")]
    settled = ctx.courier_store.get(entry.ident)
    assert settled.status == "delivered" and settled.attempts == 1
    alerts = ctx.watch_store.alerts()
    assert len(alerts) == 1 and alerts[0].kind == "courier"
    assert "delivered" in alerts[0].message


async def test_attempt_gives_up_after_the_budget(tmp_path: Path) -> None:
    """Unacknowledged attempts exhaust the budget and say so, exactly once."""
    service = _service(tmp_path, [False] * MAX_ATTEMPTS)
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "YUL", "hello")
    for _ in range(MAX_ATTEMPTS):
        message = ctx.courier_store.get(entry.ident)
        await service._attempt(message)
    settled = ctx.courier_store.get(entry.ident)
    assert settled.status == "gave-up" and settled.attempts == MAX_ATTEMPTS
    gave = [a for a in ctx.watch_store.alerts() if "gave up" in a.message]
    assert len(gave) == 1


async def test_pass_attempts_at_most_one_entry(tmp_path: Path) -> None:
    """A backlog drains one message per pass — the courier never bursts."""
    service = _service(tmp_path, [True, True])
    ctx = service._ctx
    ctx.courier_store.queue(NODE, "YUL", "first")
    ctx.courier_store.queue(NODE, "YUL", "second")
    service._heard[NODE] = utcnow()
    await service._pass()
    assert len(ctx.chat.sent) == 1
    await service._pass()
    assert len(ctx.chat.sent) == 2
    assert [t for _n, t in ctx.chat.sent] == ["first", "second"]  # oldest first


async def test_unknown_contact_spends_no_budget(tmp_path: Path) -> None:
    """A recipient the device doesn't know yet stays queued, untouched."""
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue("ff" * 6, "Stranger", "hello")
    outcome = await service._attempt(ctx.courier_store.get(entry.ident))
    assert outcome == "unknown contact"
    settled = ctx.courier_store.get(entry.ident)
    assert settled.status == QUEUED and settled.attempts == 0
    assert ctx.chat.sent == []


async def test_attempt_now_forces_a_send(tmp_path: Path) -> None:
    """The screen's Send now works regardless of freshness or schedule."""
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue(
        NODE, "YUL", "hello", not_before=utcnow() + timedelta(hours=8)
    )
    assert await service.attempt_now(entry.ident) == "delivered"
    assert await service.attempt_now(entry.ident) == "gone"  # already settled


# --- the clock parser ---------------------------------------------------------------------


def test_parse_clock_finds_the_next_occurrence() -> None:
    """HH:MM resolves to the next future occurrence, local, returned as UTC."""
    now = utcnow()
    when = parse_clock("07:00", now)
    assert when is not None and when > now
    assert (when - now) <= timedelta(days=1)
    assert when.astimezone().hour == 7 and when.astimezone().minute == 0
    assert parse_clock("7h30", now) is not None
    assert parse_clock("25:00", now) is None
    assert parse_clock("soonish", now) is None
