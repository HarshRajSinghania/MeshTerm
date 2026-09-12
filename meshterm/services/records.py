"""Walk records: how a completed trace is measured and scored for the trophy case.

A *walk* is one trace whose route starts and ends at our node — we transmit the first
hop and the final hop's transmission lands back on us. Every trace is one:

* **Trace path** composes the whole circuit by hand and only has to end within our
  earshot, so its route *is* the walk;
* **Trace target** walks the symmetric boomerang ``us → out… → target → out reversed…
  → us``, which starts and ends at us just the same.

So any successful trace can be scored, and the trophy case keeps the record-setters in
seven disciplines — the longest distance, the farthest node, the longest single link,
the most nodes (with and without revisits), the weakest surviving link, and the widest
enclosed loop. This module
is pure measurement: :func:`walk_from_trace` turns a reply into a spec, a canonical
route, and its :class:`WalkStats`; :func:`walk_scores` scores those stats against every
discipline at once (a walk found while tracing one thing still counts everywhere it
places). The UI owns the radio and the database; nothing here transmits or persists.

One eligibility rule gates every board: a record walk must be a *trail* — a walk that
never crosses the same link twice in the same direction (see
:func:`first_repeated_edge`). Repeating a link would let any scoring subpath be stitched
in again for free score, so :func:`walk_scores` disqualifies such a walk outright.
Recrossing a link the *other* way stays legal: a Trace target boomerang retraces every
link backwards by design, and radio links genuinely differ by direction.

Records live per ``(category, width_bytes)`` because the per-hop hash width bounds both a
walk's maximum length (the transmitted path field is :data:`MAX_PATH_BYTES`) and its
collision odds — a 1-byte board and a 4-byte board are different games.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from ..core.geo import haversine_km
from ..core.models import Hop, TraceResult

#: The transmitted path field is fixed at 64 bytes (see the contact-route parsing in
#: :mod:`~meshterm.core.connection`), so a walk may carry at most this many bytes of
#: hop hashes — the hard reason records are kept per hash width.
MAX_PATH_BYTES = 64


def max_hops(width_bytes: int) -> int:
    """The most hops a spec can carry at ``width_bytes`` per hop (64-byte field)."""
    return MAX_PATH_BYTES // max(1, width_bytes)


def first_repeated_edge(nodes: Sequence[str]) -> tuple[str, str] | None:
    """The first link a walk crosses twice in the same direction, or ``None``.

    The trophy case's no-cheat rule, shared by the arbiter and every screen that
    warns about it: a record walk must be a *trail* (graph theory's name for a walk
    with no repeated edge — here the directed edge ``a → b``, so ``a → b … b → a``
    stays a trail while ``a → b … a → b`` does not). The walk's implicit endpoints
    at our own node need not be passed in: the arcs touching us can't repeat unless
    we relay through ourselves mid-walk.

    Args:
        nodes: The walked hops in order — spec tokens or canonical ids, compared
            case-insensitively; blank entries are ignored.

    Returns:
        The ``(a, b)`` pair of the first link walked twice (as given, lowercased),
        or ``None`` when the walk is a trail.
    """
    walked = [n.strip().lower() for n in nodes if n and n.strip()]
    seen: set[tuple[str, str]] = set()
    for pair in pairwise(walked):
        if pair in seen:
            return pair
        seen.add(pair)
    return None


# --------------------------------------------------------------------------- scoring


@dataclass(slots=True)
class WalkStats:
    """Everything a finished walk is scored and displayed by.

    Attributes:
        node_ids: Canonical ids of the walked hops, in walk order (us excluded; the
            walk implicitly starts and ends at our node).
        hop_count: Hops in the transmitted spec (= relay transmissions out there).
        distinct_nodes: How many different nodes the walk visited.
        repeats: Whether any node was visited more than once.
        min_snr: The weakest per-hop reading of the walk (dB), when measured.
        rtt_ms: The walk's round trip, when measured.
        km_travelled: Great-circle km summed over the circuit us → hops… → us.
            Segments touching an unpositioned node add 0, making this a lower bound.
        km_complete: Whether every segment was positioned (``km_travelled`` exact).
        far_km: The furthest hop's distance from us (km), when both ends are
            positioned; ``None`` without our own or any hop's position.
        leg_km: The longest single link of the circuit (km) — one hop's transmission,
            end to end — when both of its ends are positioned; ``None`` when no
            segment had both. A different game from ``far_km``: a distant node reached
            over a chain of short hops scores far, not long.
        leg_link: The ids of that link's two ends, in walk order, with ``None`` for our
            own node (a link touching us is the first or last segment). ``None``
            alongside a ``None`` ``leg_km``.
        area_km2: The unsigned area enclosed by the circuit's positioned points in
            walk order (km², shoelace over a local plane). ``None`` below three
            positioned points; self-crossing walks score their net algebraic area.
    """

    node_ids: tuple[str, ...]
    hop_count: int
    distinct_nodes: int
    repeats: bool
    min_snr: float | None
    rtt_ms: float | None
    km_travelled: float
    km_complete: bool
    far_km: float | None
    area_km2: float | None
    leg_km: float | None = None
    leg_link: tuple[str | None, str | None] | None = None

    def as_dict(self) -> dict:
        """The stats as a JSON-serializable dict (the ``stats_json`` column)."""
        return {
            "hop_count": self.hop_count,
            "distinct_nodes": self.distinct_nodes,
            "repeats": self.repeats,
            "min_snr": self.min_snr,
            "rtt_ms": self.rtt_ms,
            "km_travelled": round(self.km_travelled, 3),
            "km_complete": self.km_complete,
            "far_km": round(self.far_km, 3) if self.far_km is not None else None,
            "area_km2": round(self.area_km2, 3) if self.area_km2 is not None else None,
            "leg_km": round(self.leg_km, 3) if self.leg_km is not None else None,
            # A list, not a tuple, because this round-trips through JSON; our own end
            # stays null so the reader draws it as the app-wide ★ rather than a name.
            "leg_link": list(self.leg_link) if self.leg_link is not None else None,
        }


@dataclass(frozen=True, slots=True)
class Category:
    """One trophy-case discipline: what makes a walk a record.

    Attributes:
        id: Stable identifier (the database key — never renamed, unlike the title).
        title: Display name, sentence case ("Most nodes").
        icon: Single glyph for menu rows.
        description: One-line description of the game being played, shown under the
            section heading on the records screen (word-wrapped when it must).
        unit: Short unit suffix for scores ("km", "nodes", "dB", "km²").
        ascending: ``True`` when *lower* scores win (Weakest link hunts the weakest
            link that still carried a walk home).
        needs_positions: Whether scoring requires node positions, so the UI can say
            why the category is starved rather than silently scoring nothing.
        score: Maps a walk's stats to its score — ``None`` when the walk simply
            can't compete in this category (no positions, a repeat in No revisits).
    """

    id: str
    title: str
    icon: str
    description: str
    unit: str
    score: Callable[[WalkStats], float | None]
    ascending: bool = False
    needs_positions: bool = False

    def format_score(self, value: float) -> str:
        """Render a score in the category's own unit (``12.4 km``, ``7 nodes``)."""
        if self.unit == "nodes":
            count = int(value)
            return f"{count} node{'s' if count != 1 else ''}"
        if self.unit == "dB":
            return f"{value:+.1f} dB"
        return f"{value:.1f} {self.unit}"


def _score_long_haul(stats: WalkStats) -> float | None:
    return stats.km_travelled if stats.km_travelled > 0 else None


def _score_far_point(stats: WalkStats) -> float | None:
    return stats.far_km


def _score_long_leg(stats: WalkStats) -> float | None:
    return stats.leg_km if stats.leg_km else None


def _score_grand_tour(stats: WalkStats) -> float | None:
    return float(stats.distinct_nodes) if stats.distinct_nodes else None


def _score_clean_trail(stats: WalkStats) -> float | None:
    if stats.repeats or not stats.distinct_nodes:
        return None
    return float(stats.distinct_nodes)


def _score_thin_thread(stats: WalkStats) -> float | None:
    return stats.min_snr


def _score_big_loop(stats: WalkStats) -> float | None:
    if stats.area_km2 is None or stats.area_km2 <= 0:
        return None
    return stats.area_km2


#: The seven disciplines. Ids are stable database keys and must never change; titles and
#: descriptions are display-only and kept plain and descriptive.
CATEGORIES: tuple[Category, ...] = (
    Category(
        id="long_haul",
        title="Longest distance",
        icon="🛣",
        unit="km",
        description="travel the greatest distance and come home",
        score=_score_long_haul,
        needs_positions=True,
    ),
    Category(
        id="far_point",
        title="Farthest node",
        icon="🎯",
        unit="km",
        description="reach the node furthest from here",
        score=_score_far_point,
        needs_positions=True,
    ),
    Category(
        id="long_leg",
        title="Longest leg",
        icon="🏹",
        unit="km",
        description="cross the greatest distance in a single hop",
        score=_score_long_leg,
        needs_positions=True,
    ),
    Category(
        id="grand_tour",
        title="Most nodes",
        icon="🧳",
        unit="nodes",
        description="visit the most nodes, passing through some twice is fine",
        score=_score_grand_tour,
    ),
    Category(
        id="clean_trail",
        title="No revisits",
        icon="👣",
        unit="nodes",
        description="visit the most nodes without passing through any twice",
        score=_score_clean_trail,
    ),
    Category(
        id="thin_thread",
        title="Weakest link",
        icon="🕸",
        unit="dB",
        description="come home over the weakest link that still carries",
        score=_score_thin_thread,
        ascending=True,
    ),
    Category(
        id="big_loop",
        title="Biggest loop",
        icon="🔆",
        unit="km²",
        description="enclose the largest area inside the walk",
        score=_score_big_loop,
        needs_positions=True,
    ),
)

CATEGORY_BY_ID: dict[str, Category] = {c.id: c for c in CATEGORIES}


def local_xy(origin: tuple[float, float], point: tuple[float, float]) -> tuple[float, float]:
    """Project a lat/lon onto a local plane around ``origin``, in km.

    An equirectangular approximation — exact enough for mesh-sized areas (a few tens
    of km) and keeps the shoelace area in honest km².
    """
    lat0, lon0 = origin
    lat, lon = point
    x = (lon - lon0) * 111.320 * math.cos(math.radians(lat0))
    y = (lat - lat0) * 110.574
    return x, y


def compute_walk_stats(
    node_ids: Sequence[str],
    hops: Sequence[Hop],
    *,
    rtt_ms: float | None,
    positions: dict[str, tuple[float, float]],
    self_pos: tuple[float, float] | None,
) -> WalkStats:
    """Measure one successful walk for every category at once.

    Args:
        node_ids: Canonical ids of the walked hops, in walk order (us excluded).
        hops: The trace reply's per-hop readings (the final hash-less hop, our own
            device, contributes its SNR like any other — it is the reading on the
            homecoming link).
        rtt_ms: The walk's measured round trip.
        positions: Known node positions keyed by canonical id.
        self_pos: Our own node's position, or ``None`` when the device shares none.

    Returns:
        The walk's :class:`WalkStats`.
    """
    ids = tuple(node_ids)
    snrs = [h.snr for h in hops if h.snr is not None]
    # The walked circuit for geometry: us, every hop in order, us again.
    points: list[tuple[float, float] | None] = [self_pos]
    points.extend(positions.get(node) for node in ids)
    points.append(self_pos)

    # The circuit's segments carry their endpoints alongside their positions, so the
    # longest one can name the link it was: ``None`` at either end is our own node,
    # which the walk leaves from and comes home to.
    ends: list[str | None] = [None, *ids, None]

    km = 0.0
    complete = True
    leg_km: float | None = None
    leg_link: tuple[str | None, str | None] | None = None
    for i, (a, b) in enumerate(pairwise(points)):
        if a is None or b is None:
            complete = False
            continue
        span = haversine_km(a[0], a[1], b[0], b[1])
        km += span
        if leg_km is None or span > leg_km:
            leg_km, leg_link = span, (ends[i], ends[i + 1])

    far: float | None = None
    if self_pos is not None:
        dists = [
            haversine_km(self_pos[0], self_pos[1], p[0], p[1])
            for p in points[1:-1]
            if p is not None
        ]
        far = max(dists) if dists else None

    area: float | None = None
    if self_pos is not None:
        placed = [p for p in points[:-1] if p is not None]  # circuit closes itself
        if len(placed) >= 3:
            xy = [local_xy(self_pos, p) for p in placed]
            twice = sum(
                xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1]
                for i in range(len(xy))
            )
            area = abs(twice) / 2.0

    return WalkStats(
        node_ids=ids,
        hop_count=len(ids),
        distinct_nodes=len(set(ids)),
        repeats=len(set(ids)) < len(ids),
        min_snr=min(snrs) if snrs else None,
        rtt_ms=rtt_ms,
        km_travelled=km,
        km_complete=complete,
        far_km=far,
        area_km2=area,
        leg_km=leg_km,
        leg_link=leg_link,
    )


def walk_scores(stats: WalkStats) -> dict[str, float]:
    """Score a walk against every category (the every-board-at-once rule).

    A walk that repeats a directed link isn't a trail (see
    :func:`first_repeated_edge`) and is disqualified from every board at once — the
    arbiter's enforcement of the no-cheat rule, so no caller has to remember it.

    Returns:
        ``category id → score`` for each category the walk can compete in; empty
        for a disqualified walk.
    """
    if first_repeated_edge(stats.node_ids) is not None:
        return {}
    out: dict[str, float] = {}
    for category in CATEGORIES:
        value = category.score(stats)
        if value is not None:
            out[category.id] = value
    return out


def walk_from_trace(
    result: TraceResult,
    *,
    canonical: Callable[[str], str | None],
    positions: dict[str, tuple[float, float]],
    self_pos: tuple[float, float] | None,
) -> tuple[str, tuple[str, ...], WalkStats] | None:
    """Derive the walk a successful trace just made: its spec, route, and stats.

    Every trace is a walk (it starts and ends at us — see the module docstring), so a
    successful reply is scoreable straight from its hops. The reply's addressed hops are
    the relays it rode, in order, with a final hash-less hop that is our own device
    coming home; the relays become both the re-walkable spec (their hashes, joined —
    exactly what *Trace this path* forces to walk it again) and, canonicalized, the
    route the geometry is measured over.

    Args:
        result: The trace reply to measure (must have succeeded).
        canonical: Folds a hop hash to its canonical node id (the topology's resolver),
            so positions line up and re-walks resolve; unknown hops keep their hash.
        positions: Known node positions keyed by canonical id.
        self_pos: Our own node's position, or ``None`` when the device shares none.

    Returns:
        ``(spec, route, stats)`` — the comma-separated hex spec, the canonical route
        aligned with it, and the walk's :class:`WalkStats` — or ``None`` when the trace
        failed or carried no addressable relay to score.
    """
    if not result.success:
        return None
    hashes = tuple(h.node.lower() for h in result.hops if h.node)
    if not hashes:
        return None
    route = tuple(canonical(h) or h for h in hashes)
    stats = compute_walk_stats(
        route,
        result.hops,
        rtt_ms=result.round_trip_ms,
        positions=positions,
        self_pos=self_pos,
    )
    return ",".join(hashes), route, stats
