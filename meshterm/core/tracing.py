"""The arithmetic a trace is sized and flagged by.

Two pure functions and the four constants behind them: how a per-hop path-hash width is
encoded into the trace ``flags`` byte, and how long to wait for a trace's reply given the
route it has to walk. Both are protocol arithmetic with no device, no persistence and no
rendering, and :class:`~meshterm.core.connection.MeshCoreDevice` needs them on the send path —
they live here so ``core`` never has to reach up into ``services`` for them. The trace
*runner* (:mod:`meshterm.services.trace_runner`) re-exports both under their own names, which
is where every other caller still reads them from.
"""

from __future__ import annotations


def path_hash_flags(width_bytes: int) -> int | None:
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


#: Fixed overhead in a trace's reply-wait budget, in seconds: our own transmit, the far
#: endpoint's turnaround, and our receive-side decode — the cost that doesn't grow with
#: the route.
TRACE_TIMEOUT_BASE_S = 4.0
#: Per-hop allowance added to the budget, in seconds. Each entry in the walked path is one
#: relay transmission, and this covers its packet airtime, the repeater's processing, and
#: the randomised transmit backoff MeshCore adds so relays don't collide.
TRACE_TIMEOUT_PER_HOP_S = 1.6
#: Upper bound on the reply-wait, in seconds, so a route that never comes home still
#: surrenders the trace (and the session) in bounded time rather than scaling without end.
TRACE_TIMEOUT_CEILING_S = 30.0
#: Fallback budget, in seconds, when the hop count can't be known ahead of the send — a
#: path-less flood to a contact we hold no route for. This is the historical flat value,
#: kept for exactly the case where we can't size the walk.
TRACE_TIMEOUT_FLOOD_S = 10.0


def trace_timeout(hop_count: int) -> float:
    """Return the reply-wait budget, in seconds, for a trace walking ``hop_count`` hops.

    A trace is a *single* packet that has to travel the whole path — out to the far hop
    and back over the mirrored return leg — before its reply reaches us, so the wait has
    to grow with the route. A flat budget sized for a neighbour cuts a long walk off
    mid-flight: the packet completes the circuit on the mesh, but our wait has already
    expired, so we log a phantom "no reply" for a route that actually worked. The budget
    is a fixed base (:data:`TRACE_TIMEOUT_BASE_S`) plus a per-hop allowance
    (:data:`TRACE_TIMEOUT_PER_HOP_S`) for each relay transmission, capped at
    :data:`TRACE_TIMEOUT_CEILING_S`.

    ``hop_count`` is the number of hops in the *transmitted* path, which already includes
    the mirrored return leg (see
    :meth:`~meshterm.core.connection.MeshCoreDevice._trace_path_to_contact`), so it maps
    one-to-one onto relay transmissions and must not be doubled here.

    Args:
        hop_count: Hops in the walked path (return leg included). ``0`` (or less) means the
            count is unknown — a path-less flood — and yields :data:`TRACE_TIMEOUT_FLOOD_S`.

    Returns:
        The number of seconds to wait for the trace reply.
    """
    if hop_count <= 0:
        return TRACE_TIMEOUT_FLOOD_S
    budget = TRACE_TIMEOUT_BASE_S + TRACE_TIMEOUT_PER_HOP_S * hop_count
    return min(budget, TRACE_TIMEOUT_CEILING_S)
