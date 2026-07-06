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

from typing import TYPE_CHECKING, Optional

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import ChatMessage, Contact, Message, utcnow

if TYPE_CHECKING:
    from ..context import AppContext


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

        def on_event(event: MeshEvent) -> None:
            # Runs on the event loop as messages arrive; keep it cheap and defensive so a
            # single bad write can never take down the subscription.
            message = event.message
            if message is not None:
                self._record_inbound(run_id, message)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.MESSAGE)
        self._run_id = run_id
        self._ctx.log.info("chat recording (run %s)", run_id)

    async def stop(self) -> None:
        """Stop recording and close the run record. Idempotent.

        A no-op if not recording. The event hub keeps listening; only this service's
        recording subscription is removed.
        """
        if not self.active:
            return
        try:
            assert self._unsubscribe is not None
            self._unsubscribe()
        finally:
            self._unsubscribe = None
        if self._run_id is not None:
            self._ctx.repo.finish_run(
                self._run_id, "ok", {"messages": self._session_count}
            )
            self._run_id = None

    async def aclose(self) -> None:
        """Stop recording at session end."""
        await self.stop()

    def _record_inbound(self, run_id: int, message: Message) -> None:
        """Persist one inbound message and bump its conversation's unread count.

        Args:
            run_id: The owning background ``chat`` run.
            message: The received message to record.
        """
        chat = ChatMessage.from_message(message)
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
        self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        return chat

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
            channel_idx=index,
            peer_name=label,
            created_at=utcnow(),
        )
        self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        return chat
