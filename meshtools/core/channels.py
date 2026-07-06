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

#: The default public channel every MeshCore device ships with on slot 0.
DEFAULT_PUBLIC_NAME = "public"


def is_public_name(name: str) -> bool:
    """Whether a channel name is a public one whose key derives from the name.

    Args:
        name: The channel name.

    Returns:
        ``True`` for a ``#``-prefixed name (the firmware's convention for a name-derived
        key), ``False`` otherwise.
    """
    return name.startswith("#")


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
