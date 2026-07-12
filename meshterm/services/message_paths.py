"""Recover the paths a chat message rode in on, from the stored packet log.

The radio's RX log records every frame it decodes — including the relay path each one
traversed — and the recorder has been persisting those as ``packet`` observations all
along. A message heard more than once (a channel broadcast rebroadcast by several
repeaters, a flood that reached us two ways) therefore left one logged frame *per
arrival*, each with its own path and SNR. This module matches those frames back to a
chat message so the chat screen can show, on demand, every way the message reached us:

* **Channel messages** match by content: a ``GRP_TXT`` frame names its channel only by
  a one-byte hash, but we hold the channel's key, so
  :func:`~meshterm.core.channels.decrypt_channel_text` can confirm the channel by MAC
  and recover the plaintext. A frame whose text equals the message's (allowing for the
  ``Name: `` sender prefix convention on either side) is one arrival of that message —
  our own broadcasts included, since a repeater's rebroadcast of us is overheard and
  logged like anything else.
* **Direct messages** ride ECDH-encrypted ``TXT_MSG`` frames that only the companion
  can decrypt, so no content match is possible from the log. Frames of that class
  logged within a tight window around the message are offered instead, clearly billed
  as matched by time — honest evidence, not a claim.

Nothing here transmits; it is a read-model over the repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from ..core.channels import decrypt_channel_text, split_channel_sender
from ..core.models import ChatMessage, Observation

if TYPE_CHECKING:
    from ..persistence.repository import Repository

#: How far around a channel message the log is searched. Wide, because an inbound
#: message is stamped with the *sender's* clock, which may drift from ours by minutes.
_CHANNEL_WINDOW = timedelta(minutes=15)

#: How far around a direct message the log is searched. Tight, because time proximity
#: is the only evidence tying an encrypted frame to the message.
_DIRECT_WINDOW = timedelta(seconds=90)

#: Frame classes that carry a direct (addressed) text message.
_DIRECT_TYPENAMES = frozenset({"TXT_MSG"})


@dataclass(slots=True)
class Arrival:
    """One logged copy of a message reaching this radio.

    Attributes:
        when: When the frame was heard.
        hops: The relay path it rode, in propagation order (empty = arrived direct).
        snr: Reception SNR in dB — of the *last relay*, as with every packet row.
        resend: The sender's resend counter for this copy (0 = the original send),
            recovered from a decrypted channel frame; always 0 for direct frames.
    """

    when: datetime
    hops: tuple[str, ...]
    snr: Optional[float]
    resend: int = 0


def _frame_hops(observation: Observation) -> tuple[str, ...]:
    """An observation's relay path as hop hashes (empty = direct)."""
    return tuple(h for h in (observation.path or "").split(",") if h)


def _texts_match(wire: str, stored: str) -> bool:
    """Whether an on-air text and a stored chat text are the same message body.

    An inbound message is stored verbatim off the wire (sender prefix included), so an
    exact match covers it. Our own outbound messages are stored as typed while the
    radio prepends the ``Name: `` sender convention — so the wire text also matches
    when its prefix-stripped body equals the stored text. Comparison is
    whitespace-trimmed but otherwise exact: paths are evidence, and a fuzzy match
    would fabricate some.
    """
    wire, stored = wire.strip(), stored.strip()
    if wire == stored:
        return True
    _sender, body = split_channel_sender(wire)
    body = body.strip()
    return bool(body) and body == stored


def channel_arrivals(
    repo: "Repository",
    message: ChatMessage,
    *,
    channel_name: str,
    secret: bytes,
) -> list[Arrival]:
    """Every logged arrival of a channel message, matched by decrypted content.

    Args:
        repo: The repository holding the packet log.
        message: The chat message whose arrivals to find (in- or outbound).
        channel_name: The conversation's channel name (for the decrypt attempt).
        secret: The channel's 16-byte key.

    Returns:
        The matching arrivals, oldest first (resends of the same message included,
        each carrying its ``resend`` counter).
    """
    frames = repo.packet_frames_between(
        message.created_at - _CHANNEL_WINDOW, message.created_at + _CHANNEL_WINDOW
    )
    arrivals: list[Arrival] = []
    for frame in frames:
        raw = frame.raw if isinstance(frame.raw, dict) else {}
        if raw.get("payload_typename") != "GRP_TXT":
            continue
        chan_hash = raw.get("chan_hash")
        cipher_mac = raw.get("cipher_mac")
        crypted = raw.get("crypted")
        if not (chan_hash and cipher_mac and crypted):
            continue
        decrypted = decrypt_channel_text(
            chan_hash, cipher_mac, crypted, [(channel_name, secret)]
        )
        if decrypted is None or not _texts_match(decrypted.text, message.text):
            continue
        arrivals.append(
            Arrival(
                when=frame.observed_at,
                hops=_frame_hops(frame),
                snr=frame.snr,
                resend=decrypted.attempt,
            )
        )
    return arrivals


def direct_frames_near(repo: "Repository", message: ChatMessage) -> list[Arrival]:
    """Direct-message frames logged around ``message``, matched by time alone.

    Direct frames are encrypted to their recipient, so the log can't confirm which
    message a frame carried — the caller must present these as time-correlated
    evidence, not a claim (see the module docstring).

    Args:
        repo: The repository holding the packet log.
        message: The chat message to search around.

    Returns:
        The window's direct-class frames, oldest first.
    """
    frames = repo.packet_frames_between(
        message.created_at - _DIRECT_WINDOW, message.created_at + _DIRECT_WINDOW
    )
    return [
        Arrival(when=f.observed_at, hops=_frame_hops(f), snr=f.snr)
        for f in frames
        if isinstance(f.raw, dict) and f.raw.get("payload_typename") in _DIRECT_TYPENAMES
    ]


def distinct_paths(arrivals: list[Arrival]) -> int:
    """How many distinct relay paths a set of arrivals covers."""
    return len({a.hops for a in arrivals})
