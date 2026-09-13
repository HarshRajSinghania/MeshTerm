# SPDX-License-Identifier: Apache-2.0
"""Pure channel logic: secret derivation, MeshCore share-URL round-tripping, and decrypt.

A MeshCore channel is a name plus a 16-byte shared secret. *Public* channels (whose name
starts with ``#``) derive their secret deterministically from the name, so anyone naming the
channel the same way lands on the same key; *private* channels carry a random secret shared
out of band. This module holds that logic with no device or I/O dependency, so the create /
join flows — and the ``meshcore://`` share links behind the QR codes — can be built and
validated (and unit-tested) without hardware. :func:`decrypt_channel_text` runs the same AES
key against an overheard, still-encrypted channel-text frame (the packet viewer's use of it),
so the app never needs the radio's own decode to read a channel it holds the key for.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import parse_qs, quote, urlsplit

# PyCryptodome is imported inside the two functions that need it, never here. Importing
# it probes the host CPU's crypto feature set (``Crypto.Util._cpu_features``), which costs
# a fifth of a second on the PicoCalc's Cortex-A7 — paid by *every* startup, because this
# module reaches the boot path through a single constant: core.connection imports
# CHANNEL_SLOT_PROBE_CAP, and core.channel_store imports the derivation helpers. Neither
# decrypts anything at import. Deferring the import moves that cost onto the first
# overheard channel frame, where the work is actually wanted; every later call is a
# sys.modules dict hit, and both functions bind the names once per call rather than per
# candidate channel, so the reception path pays effectively nothing.

#: Matches the ``Name: message`` convention channel senders use to identify themselves
#: (the protocol carries no sender field). The name is 1–20 non-colon characters and must
#: be followed by ``": "`` — conservative enough to leave ``http://…`` and ``note:x`` alone.
SENDER_PREFIX = re.compile(r"^([^\s:][^:]{0,19}):[ \t]+(.*)$", re.DOTALL)

#: Matches an ``@[Name]`` mention token, as the reply flow primes into the compose line. A
#: transcript renders each as a bare ``@Name`` coloured in that sender's hue instead of
#: showing the literal brackets, and the CLI's history face lifts the same names out — so the
#: token lives here with the rest of the message-body parsing rather than in either renderer.
#: Name is 1–20 non-``]`` characters.
MENTION = re.compile(r"@\[([^\]]{1,20})\]")


def split_channel_sender(text: str) -> tuple[str | None, str]:
    """Split a channel message into ``(sender_name, body)`` when it carries a name prefix.

    Channel messages have no sender field on the wire, so senders identify themselves by
    prefixing the text with ``Name: ``. Lifting that name out lets a transcript show it
    as a coloured header, a feed name *who* spoke, and the message-paths matcher compare
    an on-air body against a stored one.

    Args:
        text: The raw channel message text.

    Returns:
        ``(name, body)`` when a plausible ``Name: `` prefix is present, else
        ``(None, text)``.
    """
    match = SENDER_PREFIX.match(text)
    if match is None:
        return None, text
    name, body = match.group(1).strip(), match.group(2)
    if not name or name.isdigit() or body.startswith("//"):  # reject URLs / timestamps
        return None, text
    return name, body


#: A channel shared secret is exactly 16 bytes (128-bit), per the MeshCore protocol.
CHANNEL_SECRET_BYTES = 16

#: The channel-slot capacity assumed when a device can't be probed. Stock MeshCore companion
#: firmware is built with 8 slots; :meth:`meshterm.core.connection.Device.channel_capacity`
#: discovers the real number from the hardware at runtime, so this is only the fallback.
MAX_CHANNELS = 8

#: Ceiling for a slot probe. A channel slot is addressed on the wire by a single byte, so this
#: is generous headroom over any real firmware while still bounding a scan against a device that
#: never rejects an out-of-range index (so the probe can't loop forever).
CHANNEL_SLOT_PROBE_CAP = 64

#: A run of this many consecutive *empty* slots ends a *configured-channel* scan (see
#: :func:`meshterm.core.channel_probe.read_channel_slots`) on firmware that never rejects an
#: out-of-range index — some builds answer every index with an empty payload instead of
#: raising, so the scan would otherwise walk all :data:`CHANNEL_SLOT_PROBE_CAP` slots on
#: every read. The channel manager packs channels from slot 0 up within the stock
#: :data:`MAX_CHANNELS` slots, so no real layout leaves this many empty slots *before* its
#: last channel — an unbroken run this long means the scan has passed the configured
#: channels. Firmware that *does* reject an out-of-range index still ends the scan at its
#: true ceiling first. (Deliberately not applied to
#: :meth:`meshterm.core.connection.Device.channel_capacity`, which must report a larger
#: rejecting firmware's real slot count and so has to reach the rejection, not guess from a
#: run of empties; its cost is bounded by caching it for the session instead.)
CHANNEL_SLOT_EMPTY_RUN = MAX_CHANNELS

#: The default public channel every MeshCore device ships with on slot 0.
DEFAULT_PUBLIC_NAME = "public"

#: MeshCore's built-in public channel ships on slot 0 as ``Public`` with this fixed 16-byte
#: key — it is *not* derived from the name, so it can't be recognized by the ``#`` / name-key
#: heuristics and must be matched on its well-known secret instead.
DEFAULT_PUBLIC_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")

#: Secrets of well-known public channels — public (shared meshwide) regardless of their name
#: or whether their key is name-derived. Today just the firmware default above; a frozenset so
#: further authoritative public channels can be added over time.
KNOWN_PUBLIC_SECRETS = frozenset({DEFAULT_PUBLIC_SECRET})


def is_public_name(name: str) -> bool:
    """Whether a channel name is a public one whose key derives from the name.

    Args:
        name: The channel name.

    Returns:
        ``True`` for a ``#``-prefixed name (the firmware's convention for a name-derived
        key), ``False`` otherwise.
    """
    return name.startswith("#")


def is_name_derived(name: str, secret: bytes) -> bool:
    """Whether a channel's key is reproducible from its name.

    ``True`` for a ``#``-prefixed name and for any channel whose stored secret already equals
    ``derive_secret(name)``. Such a channel need not store its key — the firmware recomputes it
    from the name — so re-keying or reordering it can pass no secret at all.

    Args:
        name: The channel name.
        secret: The 16-byte secret stored in the slot.

    Returns:
        ``True`` if the name alone reproduces the key.
    """
    return is_public_name(name) or bytes(secret) == derive_secret(name)


def is_public_channel(name: str, secret: bytes) -> bool:
    """Whether a channel is public — shared across the mesh rather than a private, off-mesh one.

    A channel counts as public when its key is name-derived (see :func:`is_name_derived`) *or*
    it is a :data:`KNOWN_PUBLIC_SECRETS` channel — notably the firmware's default ``Public``,
    whose key is a fixed well-known value rather than one derived from its name. The latter is
    why a plain name/``#`` check alone would mislabel it as private.

    Args:
        name: The channel name.
        secret: The 16-byte secret stored in the slot.

    Returns:
        ``True`` if the channel is public.
    """
    return is_name_derived(name, secret) or bytes(secret) in KNOWN_PUBLIC_SECRETS


def derive_secret(name: str) -> bytes:
    """Derive a channel's 16-byte secret from its name, exactly as the firmware does.

    A ``#``-prefixed (public) channel — or any channel created without an explicit key —
    uses ``sha256(name)[:16]``, so everyone who types the same name shares the same key.

    Args:
        name: The channel name (including any leading ``#``).

    Returns:
        The derived 16-byte secret.
    """
    return sha256(name.encode("utf-8")).digest()[:CHANNEL_SECRET_BYTES]


def random_secret() -> bytes:
    """Generate a fresh cryptographically-random 16-byte secret for a private channel.

    Returns:
        16 random bytes.
    """
    return secrets.token_bytes(CHANNEL_SECRET_BYTES)


def normalize_secret(text: str) -> bytes:
    """Parse a user-supplied secret (32 hex chars / 16 bytes).

    Tolerates surrounding whitespace, an ``0x`` prefix, and internal spaces so a key
    pasted in any common shape is accepted.

    Args:
        text: The secret as a hex string.

    Returns:
        The 16-byte secret.

    Raises:
        ValueError: If ``text`` is not exactly 16 bytes of hexadecimal.
    """
    cleaned = text.strip().removeprefix("0x").replace(" ", "").replace(":", "")
    data = bytes.fromhex(cleaned)  # raises ValueError on odd length / non-hex
    if len(data) != CHANNEL_SECRET_BYTES:
        raise ValueError("Channel secret must be exactly 16 bytes (32 hex characters).")
    return data


def full_channel_hash(secret: bytes) -> str:
    """Return the full ``sha256(secret)`` hex digest (64 chars) for a channel.

    Its leading byte is the short :func:`channel_hash` MeshCore reports; the rest is shown
    only for a complete fingerprint.

    Args:
        secret: The channel's 16-byte secret.

    Returns:
        A 64-character lowercase hex string.
    """
    return sha256(bytes(secret)).hexdigest()


def effective_secret(name: str, secret: bytes) -> bytes:
    """Return the key a channel actually encrypts with, resolving public channels by name.

    A public channel's key is *derived from its name*, so the key material a device happens
    to have stored in the slot (some firmware/simulators keep zeros) is not its true secret.
    Private channels use their stored secret as-is.

    Args:
        name: The channel name (a leading ``#`` marks it public).
        secret: The 16-byte secret stored in the slot.

    Returns:
        The channel's effective 16-byte secret.
    """
    if is_name_derived(name, secret):
        return derive_secret(name)
    return bytes(secret)


def channel_identity(name: str, secret: bytes) -> str:
    """Return a channel's stable identity — independent of which slot it occupies.

    The identity is ``sha256`` of the channel's :func:`effective_secret`, so it is intrinsic
    to the channel itself (its key material) rather than to its UX position. Reordering
    channel slots therefore never changes a channel's identity, and every device that shares
    the channel — a public channel by name, a private one by its exchanged key — computes the
    same value. Hashing keeps the raw secret out of anything the identity is stored in.

    Args:
        name: The channel name.
        secret: The 16-byte secret stored in the slot.

    Returns:
        A 64-character lowercase hex identity.
    """
    return full_channel_hash(effective_secret(name, secret))


def channel_hash(secret: bytes) -> str:
    """Return the 2-hex-character channel hash MeshCore shows for a secret.

    This is the leading byte of ``sha256(secret)``, the same short fingerprint the
    companion reports for a channel — handy for telling two channels apart at a glance.

    Args:
        secret: The channel's 16-byte secret.

    Returns:
        A two-character lowercase hex string.
    """
    return full_channel_hash(secret)[:2]


def share_url(name: str, secret: bytes) -> str:
    """Build the MeshCore ``meshcore://channel/add`` share URL for a channel.

    This is the exact scheme the MeshCore mobile apps scan, so a QR code rendered from the
    returned string adds the channel directly on another device.

    Args:
        name: The channel name.
        secret: The channel's 16-byte secret.

    Returns:
        A ``meshcore://channel/add?name=…&secret=…`` URL.
    """
    return f"meshcore://channel/add?name={quote(name, safe='')}&secret={bytes(secret).hex()}"


def parse_share_url(url: str) -> tuple[str, bytes] | None:
    """Parse a ``meshcore://channel/add`` link into ``(name, secret)``.

    Args:
        url: A channel-add share URL (as scanned from a QR code or pasted in).

    Returns:
        The channel name and its 16-byte secret, or ``None`` if the string is not a valid
        MeshCore channel-add link.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme != "meshcore":
        return None
    # The path splits as netloc="channel", path="/add"; recombine and compare on the route.
    if f"{parts.netloc}{parts.path}".strip("/") != "channel/add":
        return None
    query = parse_qs(parts.query)
    names = query.get("name")
    keys = query.get("secret")
    if not names or not keys:
        return None
    try:
        return names[0], normalize_secret(keys[0])
    except ValueError:
        return None


@dataclass(slots=True)
class DecryptedText:
    """A channel-text frame recovered from an overheard, encrypted packet.

    Attributes:
        channel_name: The name of the channel whose key unlocked the frame.
        text: The decrypted message body.
        sent_at: The sender's own clock at the moment it composed the message, when
            the embedded timestamp parses to a sane value.
        attempt: The sender's resend counter for this message (0 = first try).
    """

    channel_name: str
    text: str
    sent_at: datetime | None
    attempt: int


def identify_channel(
    chan_hash: str,
    cipher_mac: str,
    crypted: str,
    channels: Iterable[tuple[str, bytes]],
) -> tuple[str, bytes] | None:
    """Name the channel an overheard frame belongs to, confirming the key by its MAC.

    The frame names its channel only by :func:`channel_hash`'s one-byte fingerprint, and
    a fingerprint is not proof: 256 buckets hold every channel on the air, so several
    can — and on a busy mesh do — collide on one. The MAC is the proof. Every candidate
    with a matching fingerprint is HMAC-checked against the ciphertext, exactly as the
    firmware does before decoding anything, and only a channel whose key reproduces the
    frame's own MAC is named.

    Naming is all this does, which is what makes it usable on a frame we have no business
    decrypting: a channel *datagram* carries the same envelope as a channel text but a
    body that is not text at all, and a feed lane only ever wanted the channel's name.
    :func:`decrypt_channel_text` is this same confirmation followed by the decode.

    Args:
        chan_hash: The frame's channel-hash fingerprint (2 hex chars).
        cipher_mac: The frame's MAC, hex-encoded (2 bytes).
        crypted: The frame's ciphertext, hex-encoded.
        channels: Candidate channels to try, as ``(name, secret)`` pairs.

    Returns:
        The channel's ``(name, effective key)``, or ``None`` when no known channel's MAC
        matches — a channel we don't hold the key for, or a bare fingerprint collision.
    """
    from Crypto.Hash import HMAC  # deferred: see the module header
    from Crypto.Hash import SHA256 as _SHA256

    try:
        mac = bytes.fromhex(cipher_mac)
        msg = bytes.fromhex(crypted)
    except ValueError:
        return None
    if not msg:
        return None
    for name, secret in channels:
        key = effective_secret(name, secret)
        if channel_hash(key) != chan_hash:
            continue
        mac_check = HMAC.new(key, digestmod=_SHA256)
        mac_check.update(msg)
        if mac_check.digest()[:2] == mac:
            return name, key
    return None  # unknown channel, or the fingerprint collided and the key doesn't match


def decrypt_channel_text(
    chan_hash: str,
    cipher_mac: str,
    crypted: str,
    channels: Iterable[tuple[str, bytes]],
) -> DecryptedText | None:
    """Recover a GRP_TXT frame's plaintext against a set of known channels.

    Mirrors the firmware's own decode of an overheard channel-text packet: the channel is
    first *confirmed* by MAC (:func:`identify_channel` — a fingerprint alone can collide),
    and only the key that proved itself is trusted to decrypt anything. A frame from a
    channel not in ``channels`` (or whose fingerprint matches but MAC doesn't — a
    genuine collision) simply yields ``None``, same as firmware that doesn't know the
    channel either.

    Args:
        chan_hash: The frame's channel-hash fingerprint (2 hex chars).
        cipher_mac: The frame's MAC, hex-encoded (2 bytes).
        crypted: The frame's ciphertext, hex-encoded (a whole number of AES blocks).
        channels: Candidate channels to try, as ``(name, secret)`` pairs.

    Returns:
        The decrypted text and its channel, or ``None`` if no known channel's MAC
        matches or the ciphertext is malformed.
    """
    from Crypto.Cipher import AES  # deferred: see the module header

    try:
        msg = bytes.fromhex(crypted)
    except ValueError:
        return None
    if not msg or len(msg) % AES.block_size:
        return None  # not a whole number of blocks: not a decryptable GRP_TXT body
    identified = identify_channel(chan_hash, cipher_mac, crypted, channels)
    if identified is None:
        return None
    name, key = identified
    plain = AES.new(key, AES.MODE_ECB).decrypt(msg)
    timestamp = int.from_bytes(plain[0:4], "little")
    attempt = plain[4] & 0x03
    text = plain[5:].strip(b"\x00").decode("utf-8", "ignore")
    sent_at = None
    if timestamp:
        try:
            sent_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            sent_at = None
    return DecryptedText(channel_name=name, text=text, sent_at=sent_at, attempt=attempt)
