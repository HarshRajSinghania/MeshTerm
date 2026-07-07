"""Pure channel logic: secret derivation and MeshCore share-URL round-tripping.

A MeshCore channel is a name plus a 16-byte shared secret. *Public* channels (whose name
starts with ``#``) derive their secret deterministically from the name, so anyone naming the
channel the same way lands on the same key; *private* channels carry a random secret shared
out of band. This module holds that logic with no device or I/O dependency, so the create /
join flows — and the ``meshcore://`` share links behind the QR codes — can be built and
validated (and unit-tested) without hardware.
"""

from __future__ import annotations

import secrets
from hashlib import sha256
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

#: A channel shared secret is exactly 16 bytes (128-bit), per the MeshCore protocol.
CHANNEL_SECRET_BYTES = 16

#: The channel-slot capacity assumed when a device can't be probed. Stock MeshCore companion
#: firmware is built with 8 slots; :meth:`meshtools.core.connection.Device.channel_capacity`
#: discovers the real number from the hardware at runtime, so this is only the fallback.
MAX_CHANNELS = 8

#: Ceiling for a slot probe. A channel slot is addressed on the wire by a single byte, so this
#: is generous headroom over any real firmware while still bounding a scan against a device that
#: never rejects an out-of-range index (so the probe can't loop forever).
CHANNEL_SLOT_PROBE_CAP = 64

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


def parse_share_url(url: str) -> Optional[tuple[str, bytes]]:
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
