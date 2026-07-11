"""The Mesh Atlas's graph layout: hop-distance rings with force-relaxed angles.

The atlas answers the mesh's first spatial question — *how far out is everything?* —
so the layout is radial by construction: our own node pins the origin and every other
node sits at a radius fixed by its evidenced hop distance (BFS over the link graph).
That leaves exactly one degree of freedom per node, its **angle**, and that is what
the relaxation optimises: nodes are pulled toward the circular mean of their
neighbours' bearings (weighted by link strength), so chains stay radial, clusters
gather into wedges, and edges shorten — then each ring's members are pushed apart to
a minimum angular separation so markers and labels never pile up. One angular degree
of freedom keeps the optimisation deterministic and stable (no oscillating springs,
no random restarts): the same evidence lays out the same picture every time, and a
node keeps roughly the same bearing across rebuilds because its seed angle is a hash
of its id.

Evidence islands — link clusters known only through a repeater's neighbour table,
with no observed path back to us — take one shared ring outside everything reached;
their mutual edges pull them into a visible cluster there.

Positions come out in **unit space**: radius ``ring / max_ring`` (≤ 1.0), centre at
the origin. The screen owns projection — aspect, zoom, and pan — so the layout never
needs recomputing as the user moves the camera; only fresh evidence relays it.

Pure data-in/data-out (no UI, no storage), like every service.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass

_TAU = math.tau

#: Angular relaxation sweeps. Each sweep moves every node part-way toward its
#: neighbours' mean bearing then re-spaces each ring; a few dozen sweeps settle a
#: mesh-sized graph (tens of nodes) far below one dot of movement.
_RELAX_SWEEPS = 48

#: How far a node moves toward its neighbours' mean bearing per sweep (0..1).
_RELAX_ALPHA = 0.35

#: The widest minimum angular separation a ring enforces (radians, ≈20°). Crowded
#: rings fall back to their even-spacing angle (2π/n) — the most separation that
#: exists — so the floor only ever *relaxes*, never over-constrains.
_MIN_SEPARATION = 0.35

#: Separation passes per sweep: each pass walks the ring once, splitting deficits
#: between neighbouring pairs; a handful converges since rings hold few nodes.
_SEPARATION_PASSES = 4


@dataclass(slots=True)
class AtlasLayout:
    """One laid-out atlas graph, in unit space.

    Attributes:
        positions: Per-node ``(x, y)`` with the centre at the origin and every
            radius ≤ 1.0. The screen projects these through aspect/zoom/pan.
        rings: Per-node hop ring (0 = our own node; islands share the outermost).
        islands: Nodes with no observed path back to us.
        max_ring: The outermost ring index in use (0 for a lone node).
    """

    positions: dict[str, tuple[float, float]]
    rings: dict[str, int]
    islands: frozenset[str]
    max_ring: int


def _hash_angle(node: str) -> float:
    """A deterministic per-node seed bearing, stable across processes and rebuilds.

    A CRC rather than :func:`hash`, which Python salts per process — the whole point
    is that a node sits at the same clock position tomorrow as today.
    """
    return zlib.crc32(node.encode()) / 0xFFFFFFFF * _TAU


def _circular_mean(bearings: list[tuple[float, float]]) -> float | None:
    """The weighted circular mean of ``(angle, weight)`` bearings, or ``None`` if flat."""
    sin_sum = sum(w * math.sin(a) for a, w in bearings)
    cos_sum = sum(w * math.cos(a) for a, w in bearings)
    if abs(sin_sum) < 1e-12 and abs(cos_sum) < 1e-12:
        return None
    return math.atan2(sin_sum, cos_sum) % _TAU


def _toward(angle: float, target: float, alpha: float) -> float:
    """``angle`` moved ``alpha`` of the way toward ``target`` along the short arc."""
    delta = (target - angle + math.pi) % _TAU - math.pi
    return (angle + alpha * delta) % _TAU


def _space_ring(members: list[str], angles: dict[str, float]) -> None:
    """Push one ring's members apart to the separation floor, preserving their order.

    Members are walked in angular order and every adjacent pair (wrap included)
    closer than the floor is split apart symmetrically. Deficits propagate around
    the ring over :data:`_SEPARATION_PASSES` passes; a full ring (n·floor ≥ 2π)
    degrades gracefully toward even spacing rather than fighting itself, because
    the floor never exceeds ``2π / n``.
    """
    n = len(members)
    if n < 2:
        return
    floor = min(_MIN_SEPARATION, _TAU / n)
    for _ in range(_SEPARATION_PASSES):
        ordered = sorted(members, key=lambda m: angles[m])
        moved = False
        for i in range(n):
            a, b = ordered[i], ordered[(i + 1) % n]
            gap = (angles[b] - angles[a]) % _TAU
            if gap + 1e-12 < floor:
                push = (floor - gap) / 2
                angles[a] = (angles[a] - push) % _TAU
                angles[b] = (angles[b] + push) % _TAU
                moved = True
        if not moved:
            return


def compute_layout(self_id: str, links: list[tuple[str, str, float]]) -> AtlasLayout:
    """Lay the evidence graph out on hop rings with relaxed angular placement.

    Args:
        self_id: Canonical id of our own node (always placed, even with no links).
        links: The evidence graph as ``(a, b, strength)`` triples (strength > 0;
            parallel duplicates keep their strongest reading).

    Returns:
        The unit-space :class:`AtlasLayout`.
    """
    adjacency: dict[str, dict[str, float]] = {self_id: {}}
    for a, b, strength in links:
        adjacency.setdefault(a, {})
        adjacency.setdefault(b, {})
        adjacency[a][b] = max(strength, adjacency[a].get(b, 0.0))
        adjacency[b][a] = max(strength, adjacency[b].get(a, 0.0))

    # Hop rings: BFS from us; unreached nodes (islands) share one outer ring.
    rings: dict[str, int] = {self_id: 0}
    frontier = [self_id]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for other in sorted(adjacency.get(node, {})):
                if other not in rings:
                    rings[other] = rings[node] + 1
                    nxt.append(other)
        frontier = nxt
    islands = frozenset(n for n in adjacency if n not in rings)
    if islands:
        island_ring = max(rings.values(), default=0) + 1
        for node in islands:
            rings[node] = island_ring
    max_ring = max(rings.values(), default=0)

    # Seed every bearing from the node's id hash, then relax: each sweep pulls a
    # node toward the strength-weighted circular mean of its neighbours' bearings
    # (the centre node exerts no pull — it has no bearing) and re-spaces each ring.
    angles: dict[str, float] = {
        node: _hash_angle(node) for node in adjacency if node != self_id
    }
    by_ring: dict[int, list[str]] = {}
    for node, ring in rings.items():
        if ring:
            by_ring.setdefault(ring, []).append(node)
    order = sorted(angles)  # a stable sweep order keeps the result deterministic
    for _ in range(_RELAX_SWEEPS):
        for node in order:
            bearings = [
                (angles[other], strength)
                for other, strength in adjacency[node].items()
                if other != self_id
            ]
            target = _circular_mean(bearings)
            if target is not None:
                angles[node] = _toward(angles[node], target, _RELAX_ALPHA)
        for members in by_ring.values():
            _space_ring(members, angles)

    positions: dict[str, tuple[float, float]] = {self_id: (0.0, 0.0)}
    for node, angle in angles.items():
        radius = rings[node] / max_ring if max_ring else 1.0
        positions[node] = (radius * math.cos(angle), radius * math.sin(angle))
    return AtlasLayout(
        positions=positions, rings=rings, islands=islands, max_ring=max_ring
    )
