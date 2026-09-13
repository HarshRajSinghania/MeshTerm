# SPDX-License-Identifier: Apache-2.0
"""Typed events carried by the always-on mesh event hub.

These are the domain-level events that flow from the connected companion device out to
any number of interested subscribers (see :class:`~meshterm.services.event_hub.EventHub`).
Keeping them here — plain dataclasses with no I/O dependencies, alongside the other
domain models — lets the hub, its subscribers, and the tests all speak the same language
without importing the device layer.

The hub carries the unsolicited inbound streams a client reacts to: overheard packets
(:class:`~meshterm.core.models.Observation`), inbound text messages
(:class:`~meshterm.core.models.Message`), and delivery acknowledgements
(:class:`~meshterm.core.models.Ack`). Further kinds (contact updates, path changes) slot
in by adding an :class:`EventKind` member and stamping the payload onto a
:class:`MeshEvent`; subscribers that don't ask for the new kind are unaffected.

Correlated request/response replies (a trace's ``TRACE_DATA``, a login's result) are not
carried here — those are awaited directly by the command that sent the request, so they
work whether or not the hub is running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .models import Ack, Message, Observation, utcnow


class EventKind(str, Enum):
    """The classes of event the hub can fan out.

    A ``str`` enum so values log and serialize legibly. New client features add their own
    members here.
    """

    OBSERVATION = "observation"
    MESSAGE = "message"
    ACK = "ack"


@dataclass(slots=True)
class MeshEvent:
    """One event delivered to hub subscribers.

    A thin envelope: :attr:`kind` selects who receives it and :attr:`payload` carries the
    kind-specific data (an :class:`~meshterm.core.models.Observation` for
    :attr:`EventKind.OBSERVATION`). Kind-specific accessors like :attr:`observation` give
    callers a typed handle without matching on ``kind`` themselves.

    Attributes:
        kind: Which class of event this is; determines the payload type and routing.
        payload: The kind-specific data object.
        received_at: When the hub emitted the event (UTC).
    """

    kind: EventKind
    payload: object = None
    received_at: datetime = field(default_factory=utcnow)

    @property
    def observation(self) -> Observation | None:
        """The carried :class:`Observation`, or ``None`` if this isn't an observation."""
        return self.payload if isinstance(self.payload, Observation) else None

    @property
    def message(self) -> Message | None:
        """The carried :class:`Message`, or ``None`` if this isn't a message."""
        return self.payload if isinstance(self.payload, Message) else None

    @property
    def ack(self) -> Ack | None:
        """The carried :class:`Ack`, or ``None`` if this isn't an acknowledgement."""
        return self.payload if isinstance(self.payload, Ack) else None

    @classmethod
    def observation_event(cls, obs: Observation) -> MeshEvent:
        """Wrap an :class:`Observation` as an :attr:`EventKind.OBSERVATION` event."""
        return cls(kind=EventKind.OBSERVATION, payload=obs)

    @classmethod
    def message_event(cls, message: Message) -> MeshEvent:
        """Wrap a :class:`Message` as an :attr:`EventKind.MESSAGE` event."""
        return cls(kind=EventKind.MESSAGE, payload=message)

    @classmethod
    def ack_event(cls, ack: Ack) -> MeshEvent:
        """Wrap an :class:`Ack` as an :attr:`EventKind.ACK` event."""
        return cls(kind=EventKind.ACK, payload=ack)
