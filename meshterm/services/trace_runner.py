"""Repeated-trace execution with duty-cycle pacing, plus trace parsing helpers.

:func:`run_traces` is the measurement primitive the TX optimizer and the path probe
build on: run a trace N times with pacing, optionally reporting progress, persisting
every individual trace. The ``trace`` tool itself deliberately does *not* loop — it
transmits exactly one trace per invocation, because repeaters penalize (and can
blacklist) nodes that burst traffic — but it shares this module's path parsing and
node-name resolution.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from ..core.connection import Device
from ..core.models import Contact, TraceResult

ProgressCallback = Callable[[int, int, TraceResult], None]

#: Maps a trace hop's key-prefix hash to a display label (a contact name, or the
#: hash itself when unknown). ``None`` passes through (our own device).
NodeResolver = Callable[[Optional[str]], Optional[str]]


_HEX_DIGITS = frozenset("0123456789abcdef")


def make_node_resolver(contacts: Optional[list[Contact]]) -> NodeResolver:
    """Build a resolver that names a trace hop from its key-prefix hash.

    Trace replies identify each repeater only by a short hash — a leading slice of
    its public key. This maps that hash to the contact's friendly name when we know
    the node, so results read as names instead of opaque hex, and falls back to the
    raw hash for unknown nodes.

    Args:
        contacts: Known contacts to resolve against.

    Returns:
        A callable taking a hop label and returning a contact name when the hash
        matches a known contact, otherwise the label unchanged (``None`` = our
        device, passed through untouched).
    """
    # (name, public_key, key_prefix), all lowercased for case-insensitive prefixing.
    entries: list[tuple[str, str, str]] = []
    for c in contacts or []:
        pub = (c.public_key or "").lower().removeprefix("0x")
        prefix = (c.key_prefix or "").lower().removeprefix("0x")
        if c.name and (pub or prefix):
            entries.append((c.name, pub, prefix))

    def resolve(label: Optional[str]) -> Optional[str]:
        if not label:
            return label
        needle = label.lower().removeprefix("0x")
        for name, pub, prefix in entries:
            # The hash is a prefix of the node's key; match either against the full
            # public key or the (possibly shorter) stored prefix, in either direction.
            if pub and pub.startswith(needle):
                return name
            if prefix and (prefix.startswith(needle) or needle.startswith(prefix)):
                return name
        return label

    return resolve


def parse_trace_path(spec: str, contacts: Optional[list[Contact]] = None) -> str:
    """Parse a user path spec into the hex path string ``send_trace`` expects.

    The MeshCore trace protocol forces a route through a list of repeaters, each
    addressed by a leading slice of its public key (the "path hash"). This mirrors
    the comma-separated path field in the MeshCore mobile apps, e.g. ``"3d,f2,3d"``.

    Each comma-separated token may be either a contact name (resolved to its key
    prefix, case-insensitively) or a raw hex key prefix; the two may be mixed in a
    single spec. The path-hash *width* is taken from the hex tokens you type (every
    hop uses the same width); contact names are truncated to that width. With only
    contact names and no hex token, each name's full key prefix is used.

    Args:
        spec: Comma-separated path, e.g. ``"3d5f7a,Alice,f2a1b3"``.
        contacts: Known contacts used to resolve names to key prefixes.

    Returns:
        A comma-separated hex string of uniform-width prefixes, e.g. ``"3d5f7a,d4e5f6,f2a1b3"``.

    Raises:
        ValueError: If a token is neither a known contact nor valid hex, if the hex
            tokens disagree on width, or if the spec contains no usable hops.
    """
    by_name = {c.name.casefold(): c.key_prefix for c in (contacts or [])}
    # (hex_text, is_name) per hop; names keep their full prefix until width is known.
    tokens: list[tuple[str, bool]] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        key = token.casefold()
        if key in by_name:
            tokens.append((by_name[key].lower().removeprefix("0x"), True))
        else:
            tokens.append((token.lower().removeprefix("0x"), False))
    if not tokens:
        raise ValueError("path is empty")

    # The width is dictated by explicit hex tokens; names are truncated to match.
    # With names only, fall back to the (uniform) length of the resolved prefixes.
    hex_widths = {len(text) for text, is_name in tokens if not is_name}
    if len(hex_widths) > 1:
        raise ValueError("all hops must use the same number of hex digits")
    width = hex_widths.pop() if hex_widths else max(len(text) for text, _ in tokens)
    if width == 0 or width % 2:
        raise ValueError("hex key prefixes must have an even number of digits")

    hops: list[str] = []
    for text, is_name in tokens:
        hop = text[:width] if is_name else text
        if len(hop) != width or any(ch not in _HEX_DIGITS for ch in hop):
            raise ValueError(
                f"{text!r} is not a known contact or a {width // 2}-byte hex prefix"
            )
        hops.append(hop)
    return ",".join(hops)


def path_hash_flags(width_bytes: int) -> Optional[int]:
    """Return the trace ``flags`` value that encodes a per-hop path-hash width.

    The trace subsystem encodes the hash size as ``1 << (flags & 3)`` on both the
    send and receive sides (``send_trace`` and the ``TRACE_DATA`` reader), so only
    widths of 1, 2, 4, or 8 bytes are representable. This is independent of, and
    differs from, the ``mode = size - 1`` encoding used for *contact routing*
    (``out_path_hash_mode``).

    Args:
        width_bytes: Path-hash width in bytes.

    Returns:
        The flags value (the exponent ``s``), or ``None`` if the width is not a
        representable power of two.
    """
    for s in range(4):
        if (1 << s) == width_bytes:
            return s
    return None


async def run_traces(
    device: Device,
    target: str,
    *,
    samples: int = 5,
    path: Optional[str] = None,
    cooldown_s: float = 1.0,
    timeout: float = 10.0,
    on_result: Optional[ProgressCallback] = None,
    persist: Optional[Callable[[TraceResult], Awaitable[None] | None]] = None,
) -> list[TraceResult]:
    """Run ``samples`` traces to ``target`` with pacing between transmissions.

    Args:
        device: The connected device to trace through.
        target: Destination node name or key prefix.
        samples: Number of traces to run.
        path: Optional explicit path to force on every trace.
        cooldown_s: Delay between traces to respect radio duty cycle.
        timeout: Per-trace reply timeout in seconds.
        on_result: Optional callback invoked as ``(completed, total, result)`` after
            each trace, e.g. to advance a progress bar.
        persist: Optional callback to store each trace (sync or async).

    Returns:
        The list of individual :class:`TraceResult` objects, in order.
    """
    results: list[TraceResult] = []
    for i in range(samples):
        result = await device.run_trace(target, path=path, timeout=timeout)
        results.append(result)

        if persist is not None:
            maybe = persist(result)
            if asyncio.iscoroutine(maybe):
                await maybe
        if on_result is not None:
            on_result(i + 1, samples, result)

        if i < samples - 1 and cooldown_s > 0:
            await asyncio.sleep(cooldown_s)
    return results
