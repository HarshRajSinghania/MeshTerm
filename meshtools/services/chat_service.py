"""The chat service: message persistence, unread tracking, and the send path.

Like the passive monitor, this service is not a listener in its own right — the always-on
:class:`~meshtools.services.event_hub.EventHub` (``ctx.events``) does the listening. This
service is one of its subscribers: the one that writes every inbound
:class:`~meshtools.core.models.Message` to history as a
:class:`~meshtools.core.models.ChatMessage`, so a conversation transcript survives across
sessions. It also owns the per-conversation *unread* counters shown in the menu header, and
the outbound send path (so both the CLI and the live chat screen record what they send the
same way).

The service is session-scoped state on the :class:`~meshtools.context.AppContext`
(``ctx.chat``). Unlike the monitor it has no on/off preference: messages addressed to us are
communications, not overheard noise, so they are always recorded while a device is listening.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from ..core.channels import CHANNEL_SLOT_PROBE_CAP, channel_identity
from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import ChatMessage, Contact, Message, utcnow

if TYPE_CHECKING:
    from ..context import AppContext


def _fallback_channel_id(idx: Optional[int]) -> str:
    """Identity for a channel we can't read (an unconfigured/unknown slot).

    Only used when the device has no channel to identify at ``idx`` — a degenerate case a
    configured channel never hits. It is still slot-derived (there is nothing intrinsic to
    key on), so it is the one place the old slot coupling survives, and only for channels
    that have no real identity yet.
    """
    return f"slot:{idx}"


class ChatService:
    """Records inbound messages to history and tracks unread counts per conversation.

    Interact through the async lifecycle methods (:meth:`start`, :meth:`stop`,
    :meth:`aclose`), the send helpers (:meth:`send_direct`, :meth:`send_channel`), and the
    unread accessors. The currently-open conversation is registered via :meth:`set_active`
    so its inbound messages don't inflate the unread badge.
    """

    def __init__(self, ctx: "AppContext") -> None:
        """Initialize the service bound to an application context.

        Args:
            ctx: The shared application context (device, repository, event hub, logger).
        """
        self._ctx = ctx
        self._unsubscribe: Optional[Unsubscribe] = None
        self._run_id: Optional[int] = None
        self._unread: dict[str, int] = {}
        self._active: Optional[str] = None
        self._session_count = 0
        # Inbound messages are recorded off a single serialized worker (see
        # :meth:`_process_inbound`) so channel messages — whose identity is resolved with an
        # async device read — are still stored in the order they arrived. The queue is the
        # hand-off from the (synchronous) hub callback to that worker.
        self._queue: Optional["asyncio.Queue[Message]"] = None
        self._worker: Optional["asyncio.Task[None]"] = None
        # Last-known channel slot index -> intrinsic identity. Inbound resolution reads the
        # slot fresh every time (see :meth:`channel_id_for`), so this is not the source of
        # truth — only a warm map for priming/backfill and a fallback when a read fails.
        self._channel_ids: dict[int, str] = {}

    @property
    def active(self) -> bool:
        """Whether the service is subscribed to the hub and recording messages."""
        return self._unsubscribe is not None

    @property
    def session_count(self) -> int:
        """Number of inbound messages recorded since this process started."""
        return self._session_count

    def unread(self, key: str) -> int:
        """Return the unread count for one conversation key."""
        return self._unread.get(key, 0)

    def unread_total(self) -> int:
        """Return the total unread count across all conversations."""
        return sum(self._unread.values())

    def set_active(self, key: Optional[str]) -> None:
        """Mark ``key`` as the open conversation (or ``None`` when none is open).

        The open conversation is immediately cleared of unread and won't accrue more while
        it stays active, since the user is looking at it.

        Args:
            key: The conversation key now open, or ``None`` on close.
        """
        self._active = key
        if key is not None:
            self._unread.pop(key, None)

    async def refresh_channels(self) -> None:
        """Rebuild the slot-index -> channel-identity map from the device's channel table.

        The map is what lets an inbound message (which carries only a slot index) be recorded
        against its channel's intrinsic identity. It is rebuilt wholesale so a reordered,
        renamed, re-keyed, or cleared slot is reflected accurately.
        """
        device = await self._ctx.device()
        ids: dict[int, str] = {}
        for idx in range(CHANNEL_SLOT_PROBE_CAP):
            try:
                payload = await device.get_channel(idx)
            except Exception:  # noqa: BLE001 - firmware may not support channel reads
                break
            if not payload:
                continue  # an empty slot; keep scanning (slots can be non-contiguous)
            name = str(payload.get("channel_name") or "")
            secret = bytes(payload.get("channel_secret") or b"\x00" * 16)
            ids[idx] = channel_identity(name, secret)
        self._channel_ids = ids

    async def channel_id_for(self, idx: int) -> str:
        """Resolve a channel slot index to its intrinsic identity, reading the slot fresh.

        The slot's *current* occupant is read from the device every call, so the identity
        always reflects the channel that is actually in that slot right now — even if the
        slots were reordered or re-keyed (in this app, via the CLI/config tool, or on another
        client such as the phone app) since the cache was last built. Trusting a session-long
        slot→identity cache instead is exactly what let a reorder misfile a channel's
        messages. The resolved identity refreshes the cache; a transient read failure falls
        back to the last identity we saw for the slot (so a blip doesn't split a channel's
        history), and only a genuinely empty slot yields the slot-derived identity.

        Args:
            idx: The channel slot index the wire reported.

        Returns:
            The channel's identity, suitable for keying its history.
        """
        try:
            device = await self._ctx.device()
            payload = await device.get_channel(idx)
        except Exception as exc:  # noqa: BLE001 - fall back rather than misroute or drop
            self._ctx.log.debug("chat: channel read for slot %s failed: %s", idx, exc)
            return self._channel_ids.get(idx) or _fallback_channel_id(idx)
        if payload:
            name = str(payload.get("channel_name") or "")
            secret = bytes(payload.get("channel_secret") or b"\x00" * 16)
            cid = channel_identity(name, secret)
            self._channel_ids[idx] = cid
            return cid
        return _fallback_channel_id(idx)

    async def start(self) -> None:
        """Begin recording inbound messages to history. Idempotent.

        Ensures the always-on event hub is running (which opens the device connection and
        may raise if no device can be selected), then subscribes to its message stream and
        opens a ``chat`` run for the recorded messages to link to.

        Raises:
            Exception: Propagates any device/hub error; the caller can surface it and the
                service simply stays inactive.
        """
        if self.active:
            return
        await self._ctx.events.start()
        run_id = self._ctx.repo.start_run(
            "chat", {"mode": "background"}, self._ctx.profile_name
        )
        self._queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._process_inbound(run_id))

        def on_event(event: MeshEvent) -> None:
            # Runs on the event loop as messages arrive; keep it cheap and non-blocking. It
            # only hands the message to the worker queue — resolution and the database write
            # happen off the worker so a slow device read can never stall the event pump.
            message = event.message
            queue = self._queue
            if message is not None and queue is not None:
                queue.put_nowait(message)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.MESSAGE)
        self._run_id = run_id
        self._ctx.log.info("chat recording (run %s)", run_id)
        await self._prime_channels()

    async def _prime_channels(self) -> None:
        """Warm the fallback channel-identity map and backfill legacy (index-keyed) history.

        Reading the channels up front seeds the map that :meth:`channel_id_for` falls back to
        when a per-message read fails. It also backfills any pre-identity messages using the
        channels currently in each slot — best-effort, since the old slot-to-channel mapping
        wasn't recorded.
        """
        try:
            await self.refresh_channels()
            if self._channel_ids:
                self._ctx.repo.backfill_channel_ids(dict(self._channel_ids))
        except Exception as exc:  # noqa: BLE001 - priming is best-effort, never fatal
            self._ctx.log.debug("chat: channel priming failed: %s", exc)

    async def stop(self) -> None:
        """Stop recording and close the run record. Idempotent.

        A no-op if not recording. The event hub keeps listening; only this service's
        recording subscription and its inbound worker are removed.
        """
        if not self.active:
            return
        try:
            assert self._unsubscribe is not None
            self._unsubscribe()
        finally:
            self._unsubscribe = None
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        self._queue = None
        if self._run_id is not None:
            self._ctx.repo.finish_run(
                self._run_id, "ok", {"messages": self._session_count}
            )
            self._run_id = None

    async def aclose(self) -> None:
        """Stop recording at session end."""
        await self.stop()

    async def _process_inbound(self, run_id: int) -> None:
        """Serially resolve and record queued inbound messages, preserving arrival order.

        A single worker drains the queue so that channel messages — whose identity is
        resolved with an async device read (see :meth:`channel_id_for`) — are still stored in
        the order they arrived. History is ordered by insertion, so recording concurrently
        could interleave two messages by their differing read latencies; the serial worker
        makes that impossible while still resolving each against the live device.

        Args:
            run_id: The owning background ``chat`` run to link recorded messages to.
        """
        assert self._queue is not None
        while True:
            message = await self._queue.get()
            try:
                channel_id = (
                    await self.channel_id_for(message.channel)
                    if message.is_channel
                    else None
                )
                self._store_inbound(run_id, message, channel_id)
            except Exception as exc:  # noqa: BLE001 - one bad message must not kill the worker
                self._ctx.log.debug("chat: failed to record message: %s", exc)
            finally:
                self._queue.task_done()

    def _store_inbound(
        self, run_id: int, message: Message, channel_id: Optional[str]
    ) -> None:
        """Persist an inbound message under a resolved identity and bump its unread count."""
        chat = ChatMessage.from_message(message, channel_id=channel_id)
        self._session_count += 1
        if chat.key != self._active:
            self._unread[chat.key] = self._unread.get(chat.key, 0) + 1
        try:
            self._ctx.repo.record_chat_message(chat, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - never let logging break the subscription
            self._ctx.log.debug("chat: failed to record message: %s", exc)

    async def send_direct(self, contact: Contact, text: str) -> ChatMessage:
        """Send a direct message to a contact and record it in history.

        Args:
            contact: The recipient.
            text: The message body.

        Returns:
            The recorded outbound :class:`ChatMessage` (its ``acked`` reflects whether a
            delivery acknowledgement arrived).
        """
        device = await self._ctx.device()
        ack = await device.send_direct_message(contact, text)
        chat = ChatMessage(
            text=text,
            outbound=True,
            is_channel=False,
            peer=contact.key_prefix or contact.public_key[:12] or None,
            peer_name=contact.name,
            acked=ack is not None,
            created_at=utcnow(),
        )
        chat.row_id = self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        return chat

    async def resend_direct(self, contact: Contact, message: ChatMessage) -> ChatMessage:
        """Re-attempt delivery of an unacknowledged direct message, updating it in place.

        Used to retry a message that was transmitted but never acknowledged (its ``acked``
        is ``False``). The same stored row is reused — its delivery state is updated rather
        than a duplicate transcript entry created — so the message simply flips to delivered
        (or stays unacknowledged for another retry).

        Args:
            contact: The recipient.
            message: The previously-sent :class:`ChatMessage` to re-transmit; mutated in
                place with the new delivery state.

        Returns:
            The same ``message``, with :attr:`~ChatMessage.acked` refreshed.
        """
        device = await self._ctx.device()
        ack = await device.send_direct_message(contact, message.text)
        message.acked = ack is not None
        if message.row_id is not None:
            self._ctx.repo.update_chat_ack(message.row_id, message.acked)
        return message

    async def send_channel(
        self, index: int, text: str, *, label: Optional[str] = None
    ) -> ChatMessage:
        """Broadcast a message on a channel and record it in history.

        Args:
            index: The channel slot to transmit on.
            text: The message body.
            label: A display label for the channel (e.g. ``#general``), stored for the
                transcript.

        Returns:
            The recorded outbound :class:`ChatMessage`.
        """
        device = await self._ctx.device()
        await device.send_channel_message(index, text)
        chat = ChatMessage(
            text=text,
            outbound=True,
            is_channel=True,
            channel_id=await self.channel_id_for(index),
            channel_idx=index,
            peer_name=label,
            created_at=utcnow(),
        )
        self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        return chat
