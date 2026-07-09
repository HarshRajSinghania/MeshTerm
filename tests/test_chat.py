"""Tests for the chat feature: device send, persistence, and the chat service.

All run against the :class:`MockDevice` simulator and a temporary database; no hardware.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest

#: Strip ANSI SGR escapes so rendered transcript lines can be asserted as plain text.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI color escapes from rendered lines for plain-text assertions."""
    return _ANSI.sub("", text)

from meshterm.core.channels import DEFAULT_PUBLIC_SECRET, derive_secret
from meshterm.core.connection import MockDevice
from meshterm.core.events import MeshEvent
from meshterm.core.models import (
    ChatMessage,
    Contact,
    Conversation,
    Message,
    conversation_key,
)
from meshterm.persistence.repository import Repository
from meshterm.services.chat_service import ChatService
from meshterm.services.event_hub import EventHub
from meshterm.tools.chat import _PREVIEW_WIDTH, _LiveLasts, _preview_text, _title
from meshterm.ui.chat import ChatScreen
from meshterm.ui.tui.screen import CANCEL


class _StubSession:
    """A session stand-in that just counts repaint requests."""

    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1


class _StubContext:
    """Minimal :class:`~meshterm.context.AppContext` stand-in for chat tests."""

    def __init__(self, device: MockDevice, repo: Repository) -> None:
        self._device = device
        self.repo = repo
        self.log = logging.getLogger("test.chat")
        self.profile_name = None
        self.events = EventHub(self)

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    r = Repository(tmp_path / "chat.db")
    yield r
    r.close()


# -- domain models ------------------------------------------------------------


def test_conversation_key_distinguishes_channels_and_directs() -> None:
    """Channel keys use the channel's identity; direct keys are case-insensitive on the peer."""
    assert conversation_key(True, "deadbeef", None) == "chan:deadbeef"
    assert conversation_key(False, None, "AABBCC") == "dm:aabbcc"

    contact = Contact(name="Alice", public_key="d4e5" + "0" * 60, key_prefix="d4e5f6a7")
    conv = Conversation(label="Alice", is_channel=False, contact=contact)
    assert conv.key == "dm:d4e5f6a7"
    assert conv.peer == "d4e5f6a7"

    chan = Conversation(label="#public", is_channel=True, channel_idx=2, channel_id="deadbeef")
    assert chan.key == "chan:deadbeef"  # keyed by identity, not the slot index
    assert chan.peer is None


def test_chat_message_from_inbound_message() -> None:
    """An inbound direct Message maps to a received ChatMessage on the right thread."""
    message = Message(text="hi", sender="a1b2c3d4", is_channel=False, snr=5.5)
    chat = ChatMessage.from_message(message, peer_name="Yagi")
    assert chat.outbound is False
    assert chat.is_channel is False
    assert chat.peer == "a1b2c3d4"
    assert chat.peer_name == "Yagi"
    assert chat.snr == 5.5
    assert chat.key == "dm:a1b2c3d4"


# -- device send --------------------------------------------------------------


async def test_mock_send_direct_returns_ack() -> None:
    """The simulator acknowledges direct messages so they show as delivered."""
    device = MockDevice()
    await device.connect()
    contact = (await device.get_contacts())[0]
    ack = await device.send_direct_message(contact, "hello")
    assert ack is not None
    await device.send_channel_message(0, "hi channel")  # no return, must not raise
    await device.disconnect()


async def test_message_pump_drains_until_empty() -> None:
    """The RX pump pulls get_msg() until the queue is empty (the pull model).

    MeshCore never pushes message bodies, so the client must pull them; without this,
    sending works but nothing is received. Guards the immediate drain, the loop until
    NO_MORE_MSGS, the MESSAGES_WAITING subscription, and clean teardown.
    """
    from meshcore import EventType

    from meshterm.core.connection import MeshCoreDevice

    class _Ev:
        def __init__(self, t) -> None:  # noqa: ANN001
            self.type = t

    class _FakeCommands:
        def __init__(self, script: list) -> None:
            self.calls = 0
            self._script = script

        async def get_msg(self, timeout=None):  # noqa: ANN001, ANN201
            i = min(self.calls, len(self._script) - 1)
            self.calls += 1
            return _Ev(self._script[i])

    class _FakeMC:
        def __init__(self, script: list) -> None:
            self.commands = _FakeCommands(script)
            self.subs: list = []

        def subscribe(self, etype, cb):  # noqa: ANN001, ANN201
            self.subs.append(etype)
            return object()

    # One real message, then the empty sentinel: the drain loop pulls twice.
    mc = _FakeMC([EventType.CONTACT_MSG_RECV, EventType.NO_MORE_MSGS])
    device = MeshCoreDevice(port="COM_TEST")
    subs: list = []
    stop = device._message_pump(mc, subs, mc.subscribe)
    try:
        await asyncio.sleep(0.01)  # let the immediate drain task run
        assert mc.commands.calls == 2  # CONTACT_MSG_RECV then NO_MORE_MSGS
        assert EventType.MESSAGES_WAITING in mc.subs  # low-latency push subscription
    finally:
        stop()


# -- persistence --------------------------------------------------------------


def test_record_and_load_direct_conversation(repo: Repository) -> None:
    """Direct messages round-trip and come back in chronological order."""
    repo.record_chat_message(ChatMessage(text="hi", outbound=True, peer="D4E5F6A7", acked=True))
    repo.record_chat_message(ChatMessage(text="yo", outbound=False, peer="d4e5f6a7", snr=3.0))

    got = repo.recent_chat_messages(is_channel=False, peer="d4e5f6a7")
    assert [m.text for m in got] == ["hi", "yo"]
    assert got[0].outbound is True and got[0].acked is True
    assert got[1].outbound is False and got[1].snr == 3.0


def test_record_and_load_channel_conversation(repo: Repository) -> None:
    """Channel messages are keyed by channel identity, isolated from direct messages."""
    repo.record_chat_message(ChatMessage(text="c0", is_channel=True, channel_id="aa00"))
    repo.record_chat_message(ChatMessage(text="c1", is_channel=True, channel_id="bb11"))

    assert [m.text for m in repo.recent_chat_messages(is_channel=True, channel_id="aa00")] == ["c0"]
    assert [m.text for m in repo.recent_chat_messages(is_channel=True, channel_id="bb11")] == ["c1"]


def test_last_chat_messages_returns_latest_per_conversation(repo: Repository) -> None:
    """The picker preview shows the newest message in each conversation."""
    repo.record_chat_message(ChatMessage(text="old", peer="aa"))
    repo.record_chat_message(ChatMessage(text="new", peer="aa"))
    repo.record_chat_message(ChatMessage(text="chan", is_channel=True, channel_id="aa00"))

    lasts = repo.last_chat_messages()
    assert lasts["dm:aa"].text == "new"
    assert lasts["chan:aa00"].text == "chan"


# -- picker row rendering -----------------------------------------------------


class _FakeChat:
    """Stand-in for the chat service exposing just the unread lookup a row title reads."""

    def __init__(self, unread: dict[str, int]) -> None:
        self._unread = unread

    def unread(self, key: str) -> int:
        return self._unread.get(key, 0)


class _RowCtx:
    """Minimal ctx exposing only what the picker-row helpers touch (repo + chat)."""

    def __init__(self, repo: Repository, unread: dict[str, int] | None = None) -> None:
        self.repo = repo
        self.chat = _FakeChat(unread or {})


def test_live_lasts_refreshes_preview_after_ttl(repo: Repository) -> None:
    """A new message becomes visible through _LiveLasts once the cache TTL lapses."""
    repo.record_chat_message(ChatMessage(text="first", is_channel=True, channel_id="c0"))
    live = _LiveLasts(_RowCtx(repo), seed=repo.last_chat_messages(), ttl=0)  # 0 => always fresh
    assert live.get("chan:c0").text == "first"
    repo.record_chat_message(ChatMessage(text="second", is_channel=True, channel_id="c0"))
    assert live.get("chan:c0").text == "second"  # picked up live, not stuck on the seed


def test_live_lasts_serves_seed_within_ttl(repo: Repository) -> None:
    """Within the TTL the seeded snapshot is served without re-querying the repository."""
    live = _LiveLasts(_RowCtx(repo), seed={"chan:c0": ChatMessage(text="seed")}, ttl=999)
    repo.record_chat_message(ChatMessage(text="later", is_channel=True, channel_id="c0"))
    assert live.get("chan:c0").text == "seed"


def test_preview_prefixes_own_messages_only() -> None:
    """Only outbound messages get a ``you:`` prefix; inbound text is shown verbatim.

    Channel senders are embedded inline in the message text by the firmware, so no author is
    synthesized (that would double it), and a direct message's author is the row label.
    """
    chan_in = ChatMessage(text="Bob: hi", is_channel=True, channel_idx=0)  # sender inline
    assert _preview_text(chan_in).plain == "Bob: hi"
    dm_in = ChatMessage(text="hey", peer="aa", peer_name="Bob")
    assert _preview_text(dm_in).plain == "hey"
    mine = ChatMessage(text="yo", outbound=True, is_channel=True, channel_idx=0)
    assert _preview_text(mine).plain == "you: yo"


def test_preview_colours_channel_sender_and_mentions() -> None:
    """A channel preview lights its inline sender name and any ``@[Name]`` mention in a hue."""
    from meshterm.ui.chat import _sender_hue

    msg = ChatMessage(text="Bob: hi @[Alice]", is_channel=True, channel_idx=0)
    preview = _preview_text(msg)
    assert preview.plain == "Bob: hi @Alice"  # the mention's brackets are dropped for display
    styles = {span.style for span in preview.spans}
    assert _sender_hue("Bob") in styles and _sender_hue("Alice") in styles


def test_preview_ellipsizes_long_text() -> None:
    """An over-long preview is clipped to the width budget with a trailing ellipsis."""
    long = ChatMessage(text="x" * 100, is_channel=True, channel_idx=0)
    out = _preview_text(long).plain
    assert len(out) == _PREVIEW_WIDTH and out.endswith("…")


def test_title_shows_badge_and_author_preview(repo: Repository) -> None:
    """A channel row renders its live unread badge and its author-prefixed preview."""
    conv = Conversation(label="General", is_channel=True, channel_idx=0, channel_id="c0")
    ctx = _RowCtx(repo, unread={"chan:c0": 3})
    last = ChatMessage(text="Bob: hi there", is_channel=True, channel_id="c0")  # sender inline
    title = _title(ctx, conv, {"chan:c0": last})
    line = title.plain  # a Text, since there is unread
    assert line.startswith("🔒 General")  # a private channel leads with its openness glyph
    assert "● 3" in line
    assert "Bob: hi there" in line


def test_title_reddens_only_the_unread_dot(repo: Repository) -> None:
    """With unread the row's ``●`` glyph (only) is styled red; with none there is no dot."""
    from rich.text import Text

    conv = Conversation(label="General", is_channel=True, channel_idx=0, channel_id="c0")
    unread = _title(_RowCtx(repo, unread={"chan:c0": 2}), conv, {})
    assert isinstance(unread, Text)
    dot = unread.plain.index("●")
    reddened = [
        span for span in unread.spans if span.style == "err" and span.start <= dot < span.end
    ]
    assert reddened and all(span.end - span.start == 1 for span in reddened)  # just the glyph

    read = _title(_RowCtx(repo, unread={}), conv, {})
    assert isinstance(read, Text)  # always a Text now, so its spans can carry the row's colour
    assert "●" not in read.plain  # nothing unread -> no badge dot
    assert not any(span.style == "err" for span in read.spans)


def test_title_preview_column_aligns_regardless_of_label_length(repo: Repository) -> None:
    """The preview starts at the same column whether the label is short or (clipped) long."""
    ctx = _RowCtx(repo)
    short = Conversation(label="A", is_channel=True, channel_idx=0, channel_id="c0")
    long = Conversation(label="A much longer channel name here", is_channel=True, channel_idx=1, channel_id="c1")
    m0 = ChatMessage(text="X: hello", is_channel=True, channel_id="c0")
    m1 = ChatMessage(text="Y: hello", is_channel=True, channel_id="c1")
    l0 = _title(ctx, short, {"chan:c0": m0}).plain
    l1 = _title(ctx, long, {"chan:c1": m1}).plain
    assert l0.index("X: hello") == l1.index("Y: hello")


def test_title_leads_with_openness_glyph(repo: Repository) -> None:
    """Channel rows lead with an openness glyph: ＃ name-derived, 🌐 fixed-key public, 🔒 private."""
    ctx = _RowCtx(repo)
    head = lambda conv: _title(ctx, conv, {}).plain.split(" ", 1)[0]
    named = Conversation(
        label="#general", is_channel=True, channel_id="c0", secret=derive_secret("#general")
    )
    public = Conversation(
        label="Public", is_channel=True, channel_id="c1", secret=DEFAULT_PUBLIC_SECRET
    )
    private = Conversation(
        label="Ops", is_channel=True, channel_id="c2", secret=bytes(range(16))
    )
    assert head(named) == "＃"
    assert head(public) == "🌐"
    assert head(private) == "🔒"


def test_title_contact_dot_reflects_conversation_history(repo: Repository) -> None:
    """A contact's dot is filled ● once we've talked, hollow ○ before — always in her hue."""
    from meshterm.ui.chat import _sender_hue

    ctx = _RowCtx(repo)
    contact = Conversation(
        label="Alice", is_channel=False, contact=Contact(name="Alice", public_key="d4" + "0" * 62)
    )
    head = lambda lasts: _title(ctx, contact, lasts).plain.split(" ", 1)[0]

    def dot_is_hued(row) -> bool:
        return any(
            span.style == _sender_hue("Alice") and span.start == 0 and span.end == 1
            for span in row.spans
        )

    # No history yet — a hollow ring, but still tinted in Alice's stable chat hue.
    fresh = _title(ctx, contact, {})
    assert fresh.plain.startswith("○") and dot_is_hued(fresh)

    # Once we've exchanged messages the same-hued dot fills in.
    last = ChatMessage(text="hi", peer=contact.peer)
    talked = _title(ctx, contact, {contact.key: last})
    assert talked.plain.startswith("●") and dot_is_hued(talked)


# -- chat service -------------------------------------------------------------


async def test_service_records_inbound_and_tracks_unread(repo: Repository) -> None:
    """Inbound messages are persisted and bump the conversation's unread count."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        ctx.events.publish(
            MeshEvent.message_event(Message(text="ping", sender="ffeeddcc", is_channel=False))
        )
        await chat._queue.join()  # let the inbound worker resolve and record the message
        assert chat.unread("dm:ffeeddcc") == 1
        assert chat.unread_total() >= 1
        stored = repo.recent_chat_messages(is_channel=False, peer="ffeeddcc")
        assert [m.text for m in stored] == ["ping"]
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_active_conversation_suppresses_unread(repo: Repository) -> None:
    """The open conversation clears and stops accruing unread while it stays active."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        chat.set_active("dm:ffeeddcc")
        ctx.events.publish(
            MeshEvent.message_event(Message(text="ping", sender="ffeeddcc", is_channel=False))
        )
        await chat._queue.join()  # let the inbound worker resolve and record the message
        assert chat.unread("dm:ffeeddcc") == 0  # active thread doesn't accrue unread
        assert repo.recent_chat_messages(is_channel=False, peer="ffeeddcc")  # still recorded
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_files_channel_message_by_current_slot_occupant(repo: Repository) -> None:
    """A channel message is filed under whatever channel is in its slot *now*.

    The wire reports only a slot index, which the firmware assigns from the channel's current
    position. Reordering the slots (here simulated out of band, without notifying the service)
    must not misroute later messages: resolution reads the slot fresh, so a message on slot 0
    lands in whatever channel occupies slot 0 at that moment — never a stale cached identity.
    """
    from meshterm.core.channels import channel_identity

    device = MockDevice()
    ctx = _StubContext(device, repo)
    await device.connect()
    secret_a, secret_b = bytes(range(16)), bytes(range(16, 32))
    await device.set_channel(0, "Alpha", secret_a)
    await device.set_channel(1, "Bravo", secret_b)
    id_a = channel_identity("Alpha", secret_a)
    id_b = channel_identity("Bravo", secret_b)

    chat = ChatService(ctx)
    await chat.start()  # primes slot 0 -> Alpha, slot 1 -> Bravo
    try:
        ctx.events.publish(
            MeshEvent.message_event(Message(text="from alpha", is_channel=True, channel=0))
        )
        await chat._queue.join()

        # Swap the occupants of slots 0 and 1 out of band — the service is never told.
        await device.set_channel(0, "Bravo", secret_b)
        await device.set_channel(1, "Alpha", secret_a)

        ctx.events.publish(
            MeshEvent.message_event(Message(text="from bravo", is_channel=True, channel=0))
        )
        await chat._queue.join()

        alpha = repo.recent_chat_messages(is_channel=True, channel_id=id_a)
        bravo = repo.recent_chat_messages(is_channel=True, channel_id=id_b)
        assert [m.text for m in alpha] == ["from alpha"]  # unaffected by the reorder
        assert [m.text for m in bravo] == ["from bravo"]  # not misfiled under Alpha's identity
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_records_channel_messages_in_arrival_order(repo: Repository) -> None:
    """A burst of channel messages is recorded in arrival order despite async resolution.

    Each channel message resolves its identity with a device read; the serial inbound worker
    guarantees they still land in the transcript (ordered by insertion) in the order received.
    """
    from meshterm.core.channels import channel_identity

    device = MockDevice()
    ctx = _StubContext(device, repo)
    await device.connect()
    secret = bytes(range(16))
    await device.set_channel(0, "Alpha", secret)
    channel_id = channel_identity("Alpha", secret)

    chat = ChatService(ctx)
    await chat.start()
    try:
        for i in range(5):
            ctx.events.publish(
                MeshEvent.message_event(Message(text=f"m{i}", is_channel=True, channel=0))
            )
        await chat._queue.join()
        stored = repo.recent_chat_messages(is_channel=True, channel_id=channel_id)
        assert [m.text for m in stored] == ["m0", "m1", "m2", "m3", "m4"]
    finally:
        await chat.stop()
        await device.disconnect()


# -- chat screen --------------------------------------------------------------


def _screen(session: _StubSession, send, *, messages=None) -> ChatScreen:
    """Build a ChatScreen for a direct conversation with a stub session and send hook."""
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    return ChatScreen(conv, messages or [], send=send, names={"d4e5f6a7": "Alice"}, session=session)


def test_chat_screen_renders_transcript_and_input() -> None:
    """The body shows each message and always ends with the input line."""
    session = _StubSession()
    messages = [ChatMessage(text="hi there", outbound=True, peer="d4e5f6a7", acked=True)]
    screen = _screen(session, send=None, messages=messages)

    lines = screen.render_body(60)
    joined = "\n".join(lines)
    assert "hi there" in joined
    assert "›" in joined  # the input editor's prompt marker


async def test_chat_screen_enter_sends_and_appends() -> None:
    """Pressing Enter sends the line and appends the returned message to the transcript."""
    session = _StubSession()
    sent: list[str] = []

    async def send(text: str) -> ChatMessage:
        sent.append(text)
        return ChatMessage(text=text, outbound=True, peer="d4e5f6a7", acked=True)

    screen = _screen(session, send=send)
    for ch in "hello":
        screen.handle("text", ch)
    screen.handle("enter")
    await asyncio.sleep(0)  # let the scheduled send task run

    assert sent == ["hello"]
    assert screen._messages[-1].text == "hello"
    assert session.invalidations > 0


async def test_direct_send_spins_until_the_ack_resolves(monkeypatch) -> None:
    """A pending direct message spins while the send is in flight, then settles on ✅."""
    from meshterm.ui.tui.spinner import Spinner

    monkeypatch.setattr("meshterm.ui.chat._SPINNER_INTERVAL", 0.005)
    session = _StubSession()
    release = asyncio.Event()

    async def send(text: str) -> ChatMessage:
        await release.wait()  # hold the send open so we can watch the glyph spin
        return ChatMessage(text=text, outbound=True, peer="d4e5f6a7", acked=True)

    screen = _screen(session, send=send)
    for ch in "hi":
        screen.handle("text", ch)
    screen.handle("enter")

    # While the send is held open the trailing glyph is a live spinner frame, and it advances.
    await asyncio.sleep(0.03)
    first = screen._spinner.frame
    joined = "\n".join(screen.render_body(60))
    assert first in Spinner.BRAILLE and first in joined and "⏳" not in joined
    await asyncio.sleep(0.03)
    assert screen._spinner.frame != first  # the animation is actually running

    # Once the ack lands, the spinner is gone and the message shows its delivered glyph.
    release.set()
    for _ in range(100):  # let _send_direct unwind (ticker cancel + swap) before asserting
        await asyncio.sleep(0.005)
        if screen._messages[-1].acked is not None:
            break
    assert screen._messages[-1].acked is True
    assert "✅" in "\n".join(screen.render_body(60))


def test_byte_counter_shows_used_over_limit_and_colors_only_used() -> None:
    """The compose bar shows ``used/limit`` with only the used count styled (the max is muted)."""
    from rich.text import Text

    screen = _screen(_StubSession(), send=None)
    for ch in "hello":
        screen.handle("text", ch)
    counter = screen._byte_counter(80, screen._used_bytes(), screen._byte_limit())
    assert isinstance(counter, Text)
    assert counter.plain.strip() == "5/150"  # 5 bytes used of the 150-byte direct-message cap
    # Only the "5" carries a color; the "/150" tail stays muted (it never changes).
    used_at = counter.plain.index("5")
    slash_at = counter.plain.index("/")
    used_spans = [s for s in counter.spans if s.start <= used_at < s.end]
    tail_spans = [s for s in counter.spans if s.start <= slash_at < s.end]
    assert used_spans and used_spans[0].style == "ok"  # green with room to spare
    assert tail_spans and tail_spans[0].style == "muted"


def test_byte_style_escalates_as_budget_runs_out() -> None:
    """The used-byte color steps green → yellow → orange → red as fewer bytes remain."""
    from meshterm.ui.chat import _BYTES_ORANGE, _BYTES_YELLOW

    style = ChatScreen._byte_style
    assert style(80) == "ok"  # plenty left → green
    assert style(20) == _BYTES_YELLOW  # within the tight band → yellow
    assert style(10) == _BYTES_ORANGE  # within the low band → orange
    assert style(0) == "err"  # limit reached → red
    assert style(-5) == "err"  # over the limit → still red


def test_channel_byte_limit_is_lower_than_direct() -> None:
    """A channel broadcast has a tighter byte budget than a direct message."""
    from meshterm.ui.chat import _CHANNEL_BYTE_LIMIT, _DM_BYTE_LIMIT

    direct = _screen(_StubSession(), send=None)
    channel = _channel_screen([])
    assert direct._byte_limit() == _DM_BYTE_LIMIT == 150
    assert channel._byte_limit() == _CHANNEL_BYTE_LIMIT == 130


def test_overflow_counts_multibyte_characters_by_byte() -> None:
    """An emoji (4 UTF-8 bytes) counts as 4 toward the budget, and overflow marks whole chars."""
    screen = _channel_screen([])  # 130-byte limit
    # 32 emojis = 128 bytes (under), a 33rd tips to 132 (over) at that whole character.
    for _ in range(33):
        screen.handle("text", "😀")
    assert screen._used_bytes() == 33 * 4
    assert screen._overflow_at(screen._byte_limit()) == 32  # the 33rd emoji is the first over


async def test_over_limit_message_is_not_sent_and_buffer_is_kept() -> None:
    """Enter on an over-budget line reports the overage and sends nothing, keeping the text."""
    session = _StubSession()
    sent: list[str] = []

    async def send(text: str) -> ChatMessage:
        sent.append(text)
        return ChatMessage(text=text, outbound=True, peer="d4e5f6a7", acked=True)

    screen = _screen(session, send=send)
    for ch in "x" * 151:  # one byte past the 150-byte direct cap
        screen.handle("text", ch)
    screen.handle("enter")
    await asyncio.sleep(0)  # nothing should have been scheduled, but let the loop turn

    assert sent == []  # the send was refused
    assert screen._editor.text == "x" * 151  # buffer kept intact so the user can trim it
    assert "Too long by 1 byte" in screen._status
    # Trimming back under the limit clears the notice.
    screen.handle("backspace")
    assert screen._status == ""


def test_chat_screen_shows_delivery_glyphs() -> None:
    """Outbound direct messages end with delivery glyphs: a spinner, then ✅ / ❌."""
    from meshterm.ui.tui.spinner import Spinner

    messages = [
        ChatMessage(text="delivered", outbound=True, peer="d4e5f6a7", acked=True),
        ChatMessage(text="dropped", outbound=True, peer="d4e5f6a7", acked=False),
        ChatMessage(text="inflight", outbound=True, peer="d4e5f6a7", acked=None),
    ]
    screen = _screen(_StubSession(), send=None, messages=messages)

    joined = "\n".join(screen.render_body(60))
    # A message still awaiting its ack spins (a Braille frame) rather than showing the old
    # static hourglass; resolved ones show ✅ / ❌.
    assert "✅" in joined and "❌" in joined
    assert Spinner.BRAILLE[0] in joined and "⏳" not in joined
    # A failed message advertises the retry shortcut in the footer hint.
    assert "Ctrl-R" in screen.footer_hint


def test_wrapped_body_hangs_under_the_first_line() -> None:
    """A body too long for the width wraps with a hanging indent under its own first line."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    # A run of short words guarantees several wrap points at a narrow width.
    text = " ".join(["word"] * 20)
    messages = [ChatMessage(text=text, peer="d4e5f6a7", created_at=base)]
    screen = _screen(_StubSession(), send=None, messages=messages)
    body_lines = [_strip_ansi(l) for l in screen._body_lines(text, messages[0], 30)]

    assert len(body_lines) > 1  # it actually wrapped
    stamp = base.astimezone().strftime("%H:%M")
    indent = body_lines[0].index(stamp) + len(stamp) + 2  # gutter: "  HH:MM  "
    first_word = body_lines[0].index("word")
    assert first_word == indent
    # Continuation lines start their text at the same column as the first line's body.
    for cont in body_lines[1:]:
        assert cont.startswith(" " * indent)
        assert cont[indent] != " "  # text resumes exactly under the body, not the gutter


def test_at_mention_renders_as_name_in_sender_hue() -> None:
    """An ``@[Name]`` token renders as a bare ``@Name`` colored in that sender's hue."""
    from datetime import datetime, timezone

    from meshterm.ui.chat import _SENDER_COLORS

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    message = ChatMessage(
        text="Bob: @[Alice] you around?", is_channel=True, channel_idx=0, created_at=base
    )
    screen = ChatScreen(conv, [message], send=None, names={}, session=_StubSession())
    _, body = screen._sender_and_body(message)
    text = screen._render_mentions(body, selected=False)

    assert "@Alice" in text.plain  # bracketed token collapsed to a bare mention
    assert "@[Alice]" not in text.plain and "[Alice]" not in text.plain
    # The "@Alice" run carries Alice's palette hue (the same the header would use).
    hue = screen._sender_style("Alice")
    assert hue in _SENDER_COLORS
    at = text.plain.index("@Alice")
    hue_spans = [
        s for s in text.spans if s.style == hue and s.start <= at and at + len("@Alice") <= s.end
    ]
    assert hue_spans


def test_direct_transcript_groups_under_sender_headers() -> None:
    """Direct chats use the same grouped layout as channels: one header per sender run."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    messages = [
        ChatMessage(text="hi", peer="d4e5f6a7", created_at=base),
        ChatMessage(text="you there?", peer="d4e5f6a7", created_at=base),
        ChatMessage(text="yes!", outbound=True, peer="d4e5f6a7", acked=True, created_at=base),
    ]
    screen = _screen(_StubSession(), send=None, messages=messages)
    rendered = _strip_ansi("\n".join(screen._render_grouped(80)))

    # Inbound sender resolves to the contact name (from the names map), not the raw key.
    assert rendered.count("Alice") == 1  # the two inbound messages share one header
    assert "d4e5f6a7" not in rendered  # the key is never shown when a name is known
    assert "you" in rendered  # our own reply gets its own header
    assert "hi" in rendered and "you there?" in rendered and "yes!" in rendered
    assert "✅" in rendered  # the outbound message keeps its delivery glyph


async def test_chat_screen_retry_resends_failed_message() -> None:
    """Ctrl-R re-attempts the latest unacknowledged message, flipping it in place."""
    session = _StubSession()
    failed = ChatMessage(text="oops", outbound=True, peer="d4e5f6a7", acked=False, row_id=7)

    async def resend(message: ChatMessage) -> ChatMessage:
        message.acked = True  # the retry gets through this time
        return message

    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    screen = ChatScreen(
        conv, [failed], send=None, names={}, session=session, resend=resend
    )

    screen.handle("retry")
    await asyncio.sleep(0)  # let the scheduled resend task run

    assert screen._messages[-1].acked is True  # same object, now acknowledged
    assert "✅" in "\n".join(screen.render_body(60))


def test_chat_screen_scroll_detaches_and_end_reattaches() -> None:
    """Scrolling up detaches from the live tail; End re-sticks to the bottom."""
    screen = _screen(_StubSession(), send=None)
    assert screen._stick is True
    screen.handle("up")
    assert screen._stick is False
    screen.handle("end")
    assert screen._stick is True


def test_split_channel_sender_extracts_name_prefix() -> None:
    """A ``Name: message`` channel line splits into sender and cleaned body."""
    from meshterm.ui.chat import _split_channel_sender

    assert _split_channel_sender("Alice: hey there") == ("Alice", "hey there")
    assert _split_channel_sender("Yagi Repeater: online") == ("Yagi Repeater", "online")
    # No plausible prefix: left untouched.
    assert _split_channel_sender("just a message") == (None, "just a message")
    assert _split_channel_sender("https://example.com") == (None, "https://example.com")
    assert _split_channel_sender("14:30 standup") == (None, "14:30 standup")


def test_channel_transcript_groups_by_sender() -> None:
    """Consecutive same-sender channel messages share one header; the body is cleaned."""
    from datetime import datetime, timezone

    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)

    def at(minutes: int) -> datetime:
        return base.replace(minute=24 + minutes)

    messages = [
        ChatMessage(text="Alice: hi", is_channel=True, channel_idx=0, created_at=at(0)),
        ChatMessage(text="Alice: again", is_channel=True, channel_idx=0, created_at=at(6)),
        ChatMessage(text="Bob: yo", is_channel=True, channel_idx=0, created_at=at(8)),
        ChatMessage(text="hello all", outbound=True, is_channel=True, channel_idx=0, created_at=at(9)),
    ]
    screen = ChatScreen(conv, messages, send=None, names={}, session=_StubSession())
    rendered = _strip_ansi("\n".join(screen._render_grouped(80)))

    assert rendered.count("Alice") == 1  # the two Alice messages share one header
    assert "Bob" in rendered and "you" in rendered
    assert "hi" in rendered and "again" in rendered  # bodies present, prefix stripped
    assert "Alice: hi" not in rendered  # the raw name prefix is lifted into the header
    # Each message keeps its own timestamp on its line, even when grouped under one sender.
    stamps = [at(m).astimezone().strftime("%H:%M") for m in (0, 6, 8, 9)]
    for stamp in stamps:
        assert stamp in rendered
    assert stamps[0] != stamps[1]  # grouped Alice messages show distinct times


def _two_day_messages():
    """Two days of direct messages, long enough to overflow a small viewport."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)
    day1 = [ChatMessage(text=f"day1-{i}", peer="d4e5f6a7", created_at=base + timedelta(minutes=i))
            for i in range(4)]
    day2 = [ChatMessage(text=f"day2-{i}", peer="d4e5f6a7",
                        created_at=base + timedelta(days=1, minutes=i)) for i in range(4)]
    return day1 + day2


def test_chat_sticky_header_pins_the_governing_day_divider() -> None:
    """The day divider above the top row pins there once it scrolls off (like the picker)."""
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    screen.render_body(60)
    (idx0, div0), (idx1, div1) = screen._sticky_headers
    assert screen.sticky_header(0) is None            # first divider is itself the top row
    assert screen.sticky_header(idx1 - 1) == div0     # still within day one — its divider pins
    assert screen.sticky_header(idx1) is None         # day two's divider is now the top row
    assert screen.sticky_header(idx1 + 1) == div1     # scrolled past it — day two's pins


def test_chat_frame_pins_a_day_divider_when_stuck_to_the_newest() -> None:
    """Rendered through the frame at the tail, a day divider occupies the pinned top row."""
    from meshterm.ui.tui import frame

    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    lines = screen.render_body(60)  # sticks to the newest message, scrolling early days off
    visible, above, _below = frame._visible_slice(screen, lines, 6)
    top = _strip_ansi(visible[0]).strip()
    assert top.startswith("──") and "Jul" in top  # a day divider is pinned to the top row
    assert above is True  # and the frame flags there's more above the pin


def test_chat_home_end_and_word_keys_move_the_compose_cursor() -> None:
    """In a chat, Home/End and Ctrl+←/→ act on the compose line, not the transcript scroll."""
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    for ch in "hello world":
        screen.handle("text", ch)
    assert screen._editor.cursor == 11
    screen.handle("home")
    assert screen._editor.cursor == 0  # line start, not scroll-to-top
    screen.handle("end")
    assert screen._editor.cursor == 11
    screen.handle("ctrl_left")
    assert screen._editor.cursor == 6  # start of "world"


def test_chat_scroll_keys_detach_and_reattach_to_the_tail() -> None:
    """PageUp/Ctrl+Home detach from the live tail; Ctrl+End snaps back to it."""
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    lines = screen.render_body(60)
    screen.note_metrics(total=len(lines), viewport=5)
    screen.handle("pageup")
    assert screen._stick is False  # a screenful up detaches from the tail
    screen.handle("ctrl_home")
    assert screen.scroll == 0 and screen._stick is False
    screen.handle("ctrl_end")
    assert screen._stick is True  # re-attached to the newest message


def test_chat_ctrl_page_scrolls_between_day_dividers() -> None:
    """Ctrl+PageDown/PageUp move the transcript scroll between day dividers."""
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    lines = screen.render_body(60)
    screen.note_metrics(total=len(lines), viewport=5)
    (idx0, _), (idx1, _) = screen._sticky_headers
    screen.scroll = 0
    screen.handle("ctrl_pagedown")
    assert screen.scroll == idx1 and screen._stick is False  # to the second day's divider
    screen.handle("ctrl_pageup")
    assert screen.scroll == idx0  # back to the first day's divider


def _two_day_channel_messages():
    """Channel messages spanning two local days (one sender), for section-jump tests."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)
    day1 = [ChatMessage(text=f"Alice: d1-{i}", is_channel=True, channel_idx=0,
                        created_at=base + timedelta(minutes=i)) for i in range(3)]
    day2 = [ChatMessage(text=f"Alice: d2-{i}", is_channel=True, channel_idx=0,
                        created_at=base + timedelta(days=1, minutes=i)) for i in range(3)]
    return day1 + day2


def test_chat_channel_ctrl_page_selects_across_days() -> None:
    """In a channel, Ctrl+PageDown/PageUp move the reply selection to day boundaries."""
    screen = _channel_screen(_two_day_channel_messages())
    starts = screen._day_start_indices()
    assert starts == [0, 3]
    screen.handle("ctrl_home")
    assert screen._selected == 0
    screen.handle("ctrl_pagedown")
    assert screen._selected == starts[1]  # first message of the second day
    screen.handle("ctrl_pageup")
    assert screen._selected == starts[0]  # back to the first day


def test_chat_channel_home_moves_the_compose_cursor() -> None:
    """A channel's Home key edits the compose line rather than jumping the selection."""
    screen = _channel_screen(_two_day_channel_messages())
    for ch in "reply":
        screen.handle("text", ch)
    assert screen._editor.cursor == 5
    screen.handle("home")
    assert screen._editor.cursor == 0
    assert screen._selected is None  # touching the compose line clears any reply selection


def test_channel_self_style_keyed_on_concept_not_label() -> None:
    """Our white 'self' style follows the message being outbound, not the 'you' label.

    A remote sender who happens to be named 'you' must still get a palette hue, never the
    white style reserved for us.
    """
    from meshterm.ui.chat import _SENDER_COLORS

    screen = _channel_screen([])
    assert screen._sender_style("you", is_self=True) == "you"  # us → white
    remote = screen._sender_style("you", is_self=False)
    assert remote != "you" and remote in _SENDER_COLORS  # remote 'you' → a normal hue


def test_channel_own_messages_do_not_merge_with_remote_namesake() -> None:
    """A remote sender literally named 'you' groups separately from our own messages."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    messages = [
        ChatMessage(text="you: impostor", is_channel=True, channel_idx=0, created_at=base),
        ChatMessage(text="mine", outbound=True, is_channel=True, channel_idx=0, created_at=base),
    ]
    screen = _channel_screen(messages)
    rendered = _strip_ansi("\n".join(screen._render_grouped(80)))
    assert rendered.count("you") == 2  # two separate headers, not one merged group


def _channel_screen(messages, session=None) -> ChatScreen:
    """Build a channel ChatScreen over ``messages`` for selection/reply tests."""
    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    return ChatScreen(
        conv, messages, send=None, names={}, session=session or _StubSession()
    )


def _channel_messages():
    """Three inbound channel messages (Alice, Alice, Bob) for reply-selection tests."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    return [
        ChatMessage(text="Alice: hi", is_channel=True, channel_idx=0, created_at=base),
        ChatMessage(text="Alice: still here", is_channel=True, channel_idx=0, created_at=base),
        ChatMessage(text="Bob: yo", is_channel=True, channel_idx=0, created_at=base),
    ]


def test_channel_up_enters_selection_from_newest() -> None:
    """No message is selected until ↑ picks the newest, then steps toward older ones."""
    screen = _channel_screen(_channel_messages())
    assert screen._selected is None  # compose focus: nothing selected initially

    screen.handle("up")
    assert screen._selected == 2  # newest message
    assert screen._stick is False
    screen.handle("up")
    assert screen._selected == 1  # steps to the previous message


def test_channel_down_past_newest_clears_selection() -> None:
    """Moving ↓ past the newest message returns focus to compose (nothing selected)."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")  # select newest (index 2)
    assert screen._selected == 2

    screen.handle("down")  # past the newest → deselect, re-stick to the tail
    assert screen._selected is None
    assert screen._stick is True


def test_channel_selected_line_tracked_for_scroll() -> None:
    """Rendering records the selected message's body line so the frame keeps it in view."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")  # select Bob (newest)
    screen.render_body(80)
    assert screen._selected_line is not None
    # No selection → no cursor line, so the transcript free-scrolls as before.
    screen.handle("end")
    screen.render_body(80)
    assert screen._selected_line is None


def test_channel_enter_on_selection_primes_at_mention() -> None:
    """Enter on a picked message seeds the compose line with the sender's @mention."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")
    screen.handle("up")  # select an Alice message (index 1)
    screen.handle("enter")

    assert screen._editor.text == "@[Alice] "
    assert screen._selected is None  # focus returns to compose after starting the reply


def test_channel_typing_clears_selection() -> None:
    """Editing the compose line drops any reply selection (compose has focus)."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")
    assert screen._selected is not None
    screen.handle("text", "x")
    assert screen._selected is None
    assert screen._editor.text == "x"


def test_chat_screen_escape_cancels() -> None:
    """Esc resolves the screen's future with CANCEL so the caller pops it."""
    screen = _screen(_StubSession(), send=None)
    loop = asyncio.new_event_loop()
    try:
        screen.future = loop.create_future()
        screen.handle("escape")
        assert screen.future.result() is CANCEL
    finally:
        loop.close()


async def test_service_send_records_outbound(repo: Repository) -> None:
    """Sending through the service records the outbound message with its ack state."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        contact = next(c for c in await device.get_contacts() if c.name == "Alice")
        sent = await chat.send_direct(contact, "hey")
        assert sent.outbound is True and sent.acked is True

        await chat.send_channel(0, "hello all", label="#public")
        channel_id = await chat.channel_id_for(0)
        stored = repo.recent_chat_messages(is_channel=True, channel_id=channel_id)
        assert [m.text for m in stored] == ["hello all"]
        assert stored[0].outbound is True
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_resend_updates_ack_in_place(repo: Repository) -> None:
    """Resending a failed message flips its stored ack rather than adding a duplicate row."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        contact = next(c for c in await device.get_contacts() if c.name == "Alice")
        peer = contact.key_prefix or contact.public_key[:12]
        failed = ChatMessage(
            text="retry me", outbound=True, peer=peer, peer_name=contact.name, acked=False
        )
        failed.row_id = repo.record_chat_message(failed)

        await chat.resend_direct(contact, failed)

        assert failed.acked is True  # the mock always acknowledges
        stored = repo.recent_chat_messages(is_channel=False, peer=peer)
        assert len(stored) == 1  # updated in place, not duplicated
        assert stored[0].acked is True
    finally:
        await chat.stop()
        await device.disconnect()


# -- end-to-end through the real session --------------------------------------


async def test_open_chat_sends_through_real_session(tmp_path: Path) -> None:
    """Driving open_chat with piped keys sends a message and records it, end to end."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.config import Settings
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.chat import open_chat
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "e2e.db")
    ctx = AppContext(
        console=Console(),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4e5f6a7" + "0" * 56, key_prefix="d4e5f6a7"),
    )
    try:
        with create_pipe_input() as inp:
            session = TuiSession(input=inp, output=DummyOutput())
            ctx.ui = TuiUi(session)

            async def main() -> None:
                inp.send_text("hi\r\x1b")  # type "hi", Enter (send), Esc (leave)
                await open_chat(ctx, conv)

            await asyncio.wait_for(session.run(main()), timeout=5)

        stored = ctx.repo.recent_chat_messages(is_channel=False, peer="d4e5f6a7")
        assert [m.text for m in stored] == ["hi"]
        assert stored[0].outbound is True
    finally:
        await ctx.aclose()
