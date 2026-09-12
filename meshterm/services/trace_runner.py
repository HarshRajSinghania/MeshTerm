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
from collections.abc import Awaitable, Callable

from ..core.connection import Device
from ..core.models import Contact, NameKeyResolver, NodeResolver, TraceResult

# Trace arithmetic lives in ``core`` because the device layer sizes its own sends with it;
# re-exported here so the runner's long-standing public names keep working.
from ..core.tracing import (  # noqa: F401 - re-exported for this module's callers
    TRACE_TIMEOUT_BASE_S,
    TRACE_TIMEOUT_CEILING_S,
    TRACE_TIMEOUT_FLOOD_S,
    TRACE_TIMEOUT_PER_HOP_S,
    path_hash_flags,
    trace_timeout,
)

ProgressCallback = Callable[[int, int, TraceResult], None]

_HEX_DIGITS = frozenset("0123456789abcdef")


def make_node_resolver(
    contacts: list[Contact] | None,
    stored_names: dict[str, str] | None = None,
) -> NodeResolver:
    """Build a resolver that names a trace hop from its key-prefix hash.

    Trace replies identify each repeater only by a short hash — a leading slice of
    its public key. This maps that hash to the contact's friendly name when we know
    the node, so results read as names instead of opaque hex, and falls back to the
    raw hash for unknown nodes. ``stored_names`` widens the net beyond the device's
    contact list to names the recorder has *ever* overheard (the repository's
    latest advertised name per node), so a node the companion never befriended — or
    has since forgotten — still resolves; contacts are listed first, so they win.

    Args:
        contacts: Known contacts to resolve against.
        stored_names: Fallback names keyed by stored node id (a hex key prefix).

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
    for node, name in (stored_names or {}).items():
        ident = node.lower().removeprefix("0x")
        if name and ident:
            entries.append((name, "", ident))

    # Memoized per label: the render paths ask for the same few hop hashes on every
    # repaint (per hop, per row, per frame), and ``entries`` is immutable for this
    # closure's lifetime, so each distinct label is scanned exactly once.
    memo: dict[str, str | None] = {}

    def resolve(label: str | None) -> str | None:
        if not label:
            return label
        if label in memo:
            return memo[label]
        needle = label.lower().removeprefix("0x")
        result = label
        for name, pub, prefix in entries:
            # The hash is a prefix of the node's key; match either against the full
            # public key or the (possibly shorter) stored prefix, in either direction.
            if pub and pub.startswith(needle):
                result = name
                break
            if prefix and (prefix.startswith(needle) or needle.startswith(prefix)):
                result = name
                break
        memo[label] = result
        return result

    return resolve


def make_node_type_resolver(
    contacts: list[Contact] | None,
) -> Callable[[str | None], int | None]:
    """Build a resolver that names a hop's *node type* from its key-prefix hash.

    The type counterpart to :func:`make_node_resolver`: it maps a trace/relay hash to the
    contact's advertised node type (a ``NODE_TYPE_*`` constant), so a route graph can mark
    a repeater with its own glyph rather than a generic dot. Matches the hash against each
    contact's key the same prefix-either-way way the name resolver does; a hash we can't
    place, or a contact with no type, resolves to ``None``.

    Args:
        contacts: Known contacts to resolve against.

    Returns:
        A callable taking a hop hash and returning its node type, or ``None`` when unknown.
    """
    entries: list[tuple[str, int]] = []
    for c in contacts or []:
        ident = (c.public_key or c.key_prefix or "").lower().removeprefix("0x")
        if ident and c.node_type is not None:
            entries.append((ident, c.node_type))

    memo: dict[str, int | None] = {}  # per label, as in make_node_resolver

    def type_of(label: str | None) -> int | None:
        if not label:
            return None
        if label in memo:
            return memo[label]
        needle = label.lower().removeprefix("0x")
        result = None
        for ident, node_type in entries:
            if ident.startswith(needle) or needle.startswith(ident):
                result = node_type
                break
        memo[label] = result
        return result

    return type_of


def make_key_resolver(contacts: list[Contact] | None) -> NodeResolver:
    """Build a resolver that expands a node's stored key-prefix hash to its full public key.

    The recorder's observations identify a node only by a short key-prefix hash — the slice
    heard on the air — but a contact the device holds carries the node's whole public key.
    This maps that stored prefix back to the full key, so a display can show as much of the
    key as fits rather than stopping at the twelve stored hex digits. A prefix no contact's
    key begins with resolves to itself, leaving the stored hash to stand.

    Args:
        contacts: Known contacts to resolve against.

    Returns:
        A callable taking a stored key prefix and returning the full public key of the
        contact whose key begins with it, or the prefix unchanged when none matches.
    """
    keys = [pub for c in contacts or [] if (pub := (c.public_key or "").lower().removeprefix("0x"))]

    memo: dict[str, str] = {}  # per label, as in make_node_resolver

    def resolve(label: str | None) -> str | None:
        if not label:
            return label
        if label in memo:
            return memo[label]
        needle = label.lower().removeprefix("0x")
        result = next((pub for pub in keys if pub.startswith(needle)), label)
        memo[label] = result
        return result

    return resolve


def make_name_key_resolver(
    contacts: list[Contact] | None,
    stored_names: dict[str, str] | None = None,
) -> NameKeyResolver:
    """Build a resolver that finds the key behind a display name.

    The app-wide colour rule keys every name's hue on the node's key; some surfaces
    (a channel message's inline sender, an ``@mention``, a route graph's origin) hold
    only a *name*. This maps that name — casefolded — back to a key: the device's
    contacts win (their keys are canonical), then the recorder's stored names fill in
    nodes the companion never befriended, so a known stranger still lands on its own
    hue. A name nobody carries resolves to ``None`` — the caller renders it muted,
    because colour is reserved for keyed identities.

    Args:
        contacts: Known contacts to resolve against (name → public key/key prefix).
        stored_names: The recorder's latest advertised name per stored node id
            (:meth:`~meshterm.persistence.repository.Repository.node_names`), inverted
            here into a name → node-id fallback.

    Returns:
        A callable taking a display name and returning the best key hex we hold for
        it, or ``None`` when the name matches no known node.
    """
    keys: dict[str, str] = {}
    for node, name in (stored_names or {}).items():
        ident = node.lower().removeprefix("0x")
        if name and ident:
            keys.setdefault(name.casefold(), ident)
    for c in contacts or []:
        ident = (c.public_key or c.key_prefix or "").lower().removeprefix("0x")
        if c.name and ident:
            keys[c.name.casefold()] = ident  # contacts win over stored names

    def key_of(name: str) -> str | None:
        if not name:
            return None
        return keys.get(name.casefold())

    return key_of


def parse_trace_path(spec: str, contacts: list[Contact] | None = None) -> str:
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
            raise ValueError(f"{text!r} is not a known contact or a {width // 2}-byte hex prefix")
        hops.append(hop)
    return ",".join(hops)


async def run_traces(
    device: Device,
    target: str,
    *,
    samples: int = 5,
    path: str | None = None,
    cooldown_s: float = 1.0,
    timeout: float | None = None,
    on_result: ProgressCallback | None = None,
    persist: Callable[[TraceResult], Awaitable[None] | None] | None = None,
) -> list[TraceResult]:
    """Run ``samples`` traces to ``target`` with pacing between transmissions.

    Args:
        device: The connected device to trace through.
        target: Destination node name or key prefix.
        samples: Number of traces to run.
        path: Optional explicit path to force on every trace.
        cooldown_s: Delay between traces to respect radio duty cycle.
        timeout: Per-trace reply timeout in seconds. ``None`` (the default) lets each
            trace size its own wait to the route via :func:`trace_timeout`.
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
