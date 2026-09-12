"""Reading the device's channel slots back off the radio.

One configured channel slot (:class:`ChannelSlot`) and the two reads that discover them.
Every derived property on the slot delegates to :mod:`meshterm.core.channels`, which owns the
key/identity maths; this module is only the device-facing half — the scan, and what it means
when the scan stops.

It lives in :mod:`meshterm.core` rather than beside the channel manager screen because the
screen is not the only reader: the device-state session cache
(:class:`~meshterm.services.device_state.DeviceState`) warms the probe, the ``channels`` and
``chat`` tools read slots for their CLI faces, and a service must never have to reach up into
``ui/`` for a device read. It is a separate module from :mod:`meshterm.core.channels` because
it needs :class:`~meshterm.core.connection.Device`, and ``connection`` already imports
``channels`` — keeping the maths free of the transport is what keeps that direction one-way.
"""

from __future__ import annotations

from dataclasses import dataclass

from .channels import (
    CHANNEL_SLOT_EMPTY_RUN,
    CHANNEL_SLOT_PROBE_CAP,
    channel_hash,
    channel_identity,
    full_channel_hash,
    is_name_derived,
    is_public_channel,
)
from .connection import Device, DeviceCommandError, is_connection_lost
from .models import Conversation


@dataclass(slots=True)
class ChannelSlot:
    """One configured channel slot read back from the device.

    Attributes:
        idx: The 0-based slot index.
        name: The channel's name.
        secret: The channel's 16-byte shared secret.
    """

    idx: int
    name: str
    secret: bytes

    @property
    def is_name_derived(self) -> bool:
        """Whether this channel's key is reproducible from its name (so it needn't be stored)."""
        return is_name_derived(self.name, self.secret)

    @property
    def is_public(self) -> bool:
        """Whether this channel is public (shared meshwide) rather than a private one.

        Covers both name-derived channels and the firmware's fixed-key default ``Public``.
        """
        return is_public_channel(self.name, self.secret)

    @property
    def hash(self) -> str:
        """The channel's two-character hash fingerprint (the leading byte MeshCore shows)."""
        return channel_hash(self.secret)

    @property
    def full_hash(self) -> str:
        """The channel's complete ``sha256(secret)`` digest; its first byte is :attr:`hash`."""
        return full_channel_hash(self.secret)

    @property
    def identity(self) -> str:
        """The channel's slot-independent identity, used to key its chat history."""
        return channel_identity(self.name, self.secret)

    @property
    def conversation(self) -> Conversation:
        """A :class:`~meshterm.core.models.Conversation` for opening this channel in chat."""
        return Conversation(
            label=self.name,
            is_channel=True,
            channel_idx=self.idx,
            channel_id=self.identity,
            secret=self.secret,
        )


async def read_channel_slots(device: Device) -> list[ChannelSlot]:
    """Probe the channel slots and return the configured ones, in index order.

    The scan stops as soon as the firmware rejects a slot index, so it reads exactly the
    slots a well-behaved device has regardless of its capacity. Firmware that never rejects
    an out-of-range index (it answers every slot with an empty payload instead of raising)
    would otherwise walk all :data:`CHANNEL_SLOT_PROBE_CAP` slots on every read; a run of
    :data:`CHANNEL_SLOT_EMPTY_RUN` consecutive empty slots ends the scan on such firmware.
    That is safe because the manager packs channels from slot 0 up, so an unbroken empty run
    that long means every configured channel has already been seen (see the constant's note).

    Args:
        device: The connected device to query.

    Returns:
        One :class:`ChannelSlot` per configured slot (an empty slot is skipped).
    """
    slots, _complete = await probe_channel_slots(device)
    return slots


async def probe_channel_slots(device: Device) -> tuple[list[ChannelSlot], bool]:
    """The probe behind :func:`read_channel_slots`, reporting whether it *finished*.

    The scan stops for two very different reasons and the plain list cannot tell them
    apart. Off the end of the configured slots (a rejected index, or a long enough run of
    empty ones) the answer is complete and worth keeping. A read that simply *failed* —
    a timeout, a link hiccup, and then every subsequent read failing too because the
    firmware's reply no longer matches the slot asked for — ends the scan early with
    whatever it happened to have, which is not the device's layout and must not be cached
    or acted on as if it were: an empty one reads as "no channels configured", and a
    truncated one makes the next free slot look free when a channel is sitting in it.

    The two endings raise different things, and that is what separates them: the firmware
    *answering* "no such slot" surfaces as a plain rejection, while a link that stopped
    answering surfaces as a timeout, a :class:`~meshterm.core.connection.DeviceCommandError`
    (whose whole subject is a companion that did not reply in time), or one of the dropped-
    link signatures :func:`~meshterm.core.connection.is_connection_lost` knows. Only the
    first ending is a layout. A read that fails for some fourth reason is treated as a
    rejection, which is the safe way round: the list is used but the cache re-probes.

    Args:
        device: The connected device to query.

    Returns:
        The slots read, and whether the scan ran to a clean end.
    """
    slots: list[ChannelSlot] = []
    empty_run = 0
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            payload = await device.get_channel(idx)
        except Exception as exc:  # noqa: BLE001 - a rejected index, or a read that failed
            return slots, not _read_failed(exc)
        if payload and payload.get("channel_name"):
            empty_run = 0
            slots.append(
                ChannelSlot(
                    idx=idx,
                    name=str(payload["channel_name"]),
                    secret=bytes(payload.get("channel_secret") or b"\x00" * 16),
                )
            )
        else:
            empty_run += 1
            if empty_run >= CHANNEL_SLOT_EMPTY_RUN:
                break  # off the end of a never-rejecting firmware; nothing more to find
    return slots, True


def _read_failed(exc: BaseException) -> bool:
    """Whether ``exc`` means the *read* failed, rather than the firmware refusing a slot.

    A refusal is the probe's ordinary ending and leaves a complete list behind it. A
    failure leaves a short one that looks exactly the same — which is the whole reason
    this distinction has to be drawn somewhere (see :func:`probe_channel_slots`).
    """
    return isinstance(exc, (TimeoutError, DeviceCommandError)) or is_connection_lost(exc)
