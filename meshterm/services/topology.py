"""The observed mesh topology: a link graph distilled from every path we ever received.

MeshCore never tells us the network's shape outright, but almost everything we *receive*
carries a fragment of it: a successful trace demonstrably crossed every link in its path
(out and back, with an SNR reading at each hop), the firmware's per-contact ``out_path``
is a route distilled from received floods, and — when the companion's packet logging is
on — every overheard frame reports the relay chain it rode in on. This module folds those
fragments into one undirected evidence graph and answers the questions the trace path
composer asks of it:

* *who neighbours whom* — :meth:`MeshTopology.next_hops`, the suggestion list while
  composing a path hop by hop, sorted by the strongest observed link first;
* *how might we reach a target* — :meth:`MeshTopology.scenarios`, ranked candidate
  outbound paths (the device's learned route, the direct shot, and the best alternatives
  the evidence supports) ready to be traced or probed.

Links are deliberately undirected: radio paths work both ways, and every reading — an SNR
measured at either end, a route learned from the opposite direction — is evidence for the
same physical link. Node identity is canonicalized against the contact list, because the
same node appears in different sources at different hash widths (a 1-byte trace hop, a
12-hex observation id, a full public key).

Everything here is pure data-in/data-out: the callers supply repository rows and contacts,
so the graph is unit-testable without a device or a database.
"""

from __future__ import annotations

import heapq
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..core.models import Contact, utcnow
from ..persistence.repository import PacketPath, TracedPath

#: Canonical id for our own device in the graph (its 12-hex key prefix when known).
#: Kept distinct from hop hashes by construction: it is derived from the full public key.

#: Half-life, in days, of link evidence: a reading a week old counts half as much as one
#: from just now. Old links stay suggestible but fresh observations dominate the ranking.
_EVIDENCE_HALF_LIFE_DAYS = 7.0

#: SNR range mapped onto the link-quality factor: -15 dB (barely readable) scores the
#: floor, +10 dB (excellent) the ceiling. Matches the trace screen's quality-bar span.
_SNR_FLOOR_DB = -15.0
_SNR_CEIL_DB = 10.0
_QUALITY_MIN = 0.15
_QUALITY_MAX = 1.5

#: Per-hop cost added when ranking candidate paths, so a marginally "stronger" long way
#: around doesn't beat a short path — every extra hop is real airtime and a real failure
#: point (and a trace crosses it twice: out and back).
_HOP_PENALTY = 0.35

#: Bounds for scenario generation: how many alternatives to offer and how deep a
#: suggested route may go (each hop is crossed twice by the trace's boomerang).
_MAX_SCENARIOS = 5
_MAX_SCENARIO_HOPS = 5

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_hex(value: str) -> bool:
    """Whether ``value`` is non-empty, even-length hex (a plausible path hash)."""
    return bool(value) and len(value) % 2 == 0 and all(c in _HEX_DIGITS for c in value)


def collapse_width(*hashes: str, ceiling: int) -> int:
    """The widest representable per-hop hash width every given hash can honour.

    Trace paths transmit hops at a uniform width of 1, 2, 4, or 8 bytes. Canonical node
    ids are usually 6 bytes, but an unidentified hop may be as short as the width it was
    once traced at — the spec must shrink to what its narrowest hash can supply.

    Args:
        *hashes: The hex hashes destined for one spec.
        ceiling: The width the caller would prefer (bytes, itself 1/2/4/8).

    Returns:
        The largest of 1/2/4/8 that is ≤ ``ceiling`` and ≤ every hash's byte length.
    """
    limit = min([ceiling, *(len(h) // 2 for h in hashes if h)] or [ceiling])
    return max(s for s in (1, 2, 4, 8) if s <= max(limit, 1))


@dataclass(slots=True)
class Link:
    """The accumulated evidence for one undirected link between two nodes.

    Attributes:
        a: Canonical id of one endpoint (the lexically smaller, so the pair is a key).
        b: Canonical id of the other endpoint.
        samples: How many independent readings crossed this link, any source.
        snrs: The SNR readings (dB) attributed to the link, either direction.
        last_seen: When the link was most recently observed.
        sources: Which evidence classes saw it (``trace`` / ``route`` / ``packet``).
    """

    a: str
    b: str
    samples: int = 0
    snrs: list[float] = field(default_factory=list)
    last_seen: Optional[datetime] = None
    sources: set[str] = field(default_factory=set)

    @property
    def median_snr(self) -> Optional[float]:
        """Median SNR (dB) across the link's readings, or ``None`` without any."""
        return statistics.median(self.snrs) if self.snrs else None

    def strength(self, now: Optional[datetime] = None) -> float:
        """Score the link's observed reliability for ranking (higher = stronger).

        Three factors multiply: *evidence* (log-scaled sample count, so ten sightings
        beat one but a thousand don't drown everything), *recency* (exponential decay
        with a :data:`one-week half-life <_EVIDENCE_HALF_LIFE_DAYS>`), and *quality*
        (median SNR mapped linearly from -15 dB → :data:`_QUALITY_MIN` to +10 dB →
        beyond 1.0; links with no SNR reading — e.g. known only from a firmware route —
        score a neutral 1.0 rather than being punished for missing data).

        Args:
            now: The reference time for recency (defaults to the current time).

        Returns:
            The link's strength score, ``> 0``.
        """
        now = now or utcnow()
        evidence = 1.0 + math.log2(1 + self.samples)
        recency = 1.0
        if self.last_seen is not None and getattr(self.last_seen, "tzinfo", None):
            age_days = max(0.0, (now - self.last_seen).total_seconds() / 86400.0)
            recency = 0.5 ** (age_days / _EVIDENCE_HALF_LIFE_DAYS)
        snr = self.median_snr
        quality = 1.0
        if snr is not None:
            span = _SNR_CEIL_DB - _SNR_FLOOR_DB
            frac = (snr - _SNR_FLOOR_DB) / span
            quality = min(_QUALITY_MAX, max(_QUALITY_MIN, _QUALITY_MIN + frac * (_QUALITY_MAX - _QUALITY_MIN)))
        return evidence * recency * quality


@dataclass(slots=True)
class HopSuggestion:
    """One suggested next hop while composing a path.

    Attributes:
        node: Canonical id of the suggested node.
        link: The evidence for the link from the path's current tail to ``node``.
        strength: The link's precomputed strength (the sort key), so the UI can render
            a stable ranking without re-scoring per repaint.
    """

    node: str
    link: Link
    strength: float


@dataclass(slots=True)
class PathScenario:
    """One candidate outbound route to a target, ready to trace.

    A scenario describes only the *outbound* leg, ending at the target: the trace
    protocol replies along the reversed path automatically, so the return is always the
    symmetric mirror and is never part of the spec.

    Attributes:
        label: Short human description of where the route came from.
        hops: Canonical ids of the intermediate repeaters, in order from us outward —
            excluding both endpoints (empty = trace the target directly).
        source: The evidence class that proposed it (``device`` / ``direct`` /
            ``observed``).
        score: Ranking score (higher first): the path's weakest-link strength, hop count
            penalized. Direct/device scenarios with no observed evidence score 0.
        weakest_snr: The lowest per-link median SNR (dB) along the route, when known —
            the bottleneck the trace is expected to measure.
        samples: Total readings across the route's links (a rough confidence figure).
    """

    label: str
    hops: tuple[str, ...]
    source: str
    score: float
    weakest_snr: Optional[float] = None
    samples: int = 0

    def spec(self, target_hash: str, width_bytes: int) -> str:
        """Render the scenario as a forced-path spec ending at the target.

        Every hop (and the target's own hash, always the final hop so the destination
        recognizes the trace and replies) is truncated to one uniform per-hop width —
        ``width_bytes``, shrunk via :func:`collapse_width` when a hop's known hash is
        narrower, since a trace transmits every hop at the same width (see
        :func:`~meshterm.services.trace_runner.parse_trace_path`).

        Args:
            target_hash: The target's hex hash (any width ≥ 1 byte).
            width_bytes: Preferred per-hop path-hash width in bytes (1, 2, 4, or 8).

        Returns:
            A comma-separated hex spec, e.g. ``"3d,f2"``.
        """
        width = collapse_width(*self.hops, target_hash, ceiling=width_bytes)
        return ",".join(h[: width * 2] for h in (*self.hops, target_hash))


class MeshTopology:
    """The evidence graph over observed mesh links, queryable for routing suggestions.

    Build one with :func:`build_topology`; instances are cheap, immutable-in-spirit
    snapshots — rebuild rather than mutate when fresh evidence lands.
    """

    def __init__(self, self_id: str, contacts: list[Contact]) -> None:
        """Create an empty graph rooted at our own node.

        Args:
            self_id: Canonical id of our own device (12-hex key prefix, lowercased).
            contacts: Known contacts, used to canonicalize hop hashes of any width to
                stable node ids and to name nodes for display.
        """
        self.self_id = self_id.lower().removeprefix("0x")[:12]
        self._links: dict[tuple[str, str], Link] = {}
        self._now = utcnow()
        # Contact index for canonicalization: full lowercased public keys (falling back
        # to the stored prefix) mapped to the 12-hex canonical id and display name.
        self._known: list[tuple[str, str, str]] = []  # (full_key, canonical, name)
        for c in contacts:
            key = (c.public_key or c.key_prefix or "").lower().removeprefix("0x")
            if key:
                self._known.append((key, key[:12], c.name))

    # --- identity ----------------------------------------------------------------

    def canonical(self, hop: Optional[str]) -> Optional[str]:
        """Collapse a hop hash of any width onto a stable node id.

        Trace hops arrive at the command's path-hash width (1-8 bytes), observation ids
        at 12 hex, routes at the region's width — all naming the same nodes. A hash that
        prefix-matches exactly one known contact adopts that contact's 12-hex id; an
        ambiguous or unknown hash stays as itself, so evidence about nodes we can't
        identify is kept (under the short id) rather than guessed onto the wrong node.

        Args:
            hop: The raw hex hash (any case, optional ``0x``), or ``None``/empty.

        Returns:
            The canonical id, or ``None`` for a missing/non-hex hop (non-hex covers
            legacy simulator artefacts like ``hop0``).
        """
        if not hop:
            return None
        needle = hop.lower().removeprefix("0x")
        if not _is_hex(needle):
            return None
        matches = {
            canonical
            for key, canonical, _name in self._known
            if key.startswith(needle) or needle.startswith(key[:12])
        }
        if len(matches) == 1:
            return next(iter(matches))
        return needle[:12]

    def display_name(self, node: str) -> Optional[str]:
        """The contact name for a canonical id, or ``None`` when unknown."""
        for key, canonical, name in self._known:
            if canonical == node:
                return name
        return None

    # --- construction ------------------------------------------------------------

    def add_walk(
        self,
        nodes: list[Optional[str]],
        *,
        snrs: Optional[list[Optional[float]]] = None,
        when: Optional[datetime] = None,
        source: str,
    ) -> None:
        """Record one walked node sequence as a set of link readings.

        Consecutive nodes become one reading each on their undirected link. ``nodes``
        holds canonical ids (``None`` entries — unresolvable hops — break the chain, so
        no false adjacency is invented across them). Self-loops (a hash repeated
        back-to-back, e.g. a manually forced there-and-back) are skipped.

        Args:
            nodes: The walked sequence, endpoints included, in order.
            snrs: Optional per-link SNR readings aligned with the links (``snrs[i]``
                belongs to the ``nodes[i] → nodes[i+1]`` link, measured at arrival).
            when: When the walk was observed (stamps every touched link's recency).
            source: Evidence class tag (``trace`` / ``route`` / ``packet``).
        """
        for i in range(len(nodes) - 1):
            a, b = nodes[i], nodes[i + 1]
            if not a or not b or a == b:
                continue
            key = (a, b) if a < b else (b, a)
            link = self._links.get(key)
            if link is None:
                link = self._links[key] = Link(a=key[0], b=key[1])
            link.samples += 1
            link.sources.add(source)
            snr = snrs[i] if snrs and i < len(snrs) else None
            if snr is not None:
                link.snrs.append(snr)
            if when is not None and (
                link.last_seen is None or when > link.last_seen
            ):
                link.last_seen = when

    # --- queries -----------------------------------------------------------------

    @property
    def link_count(self) -> int:
        """How many distinct links the graph holds evidence for."""
        return len(self._links)

    def link(self, a: str, b: str) -> Optional[Link]:
        """The evidence for the undirected link between ``a`` and ``b``, if any."""
        return self._links.get((a, b) if a < b else (b, a))

    def next_hops(self, tail: str, *, exclude: frozenset[str] = frozenset()) -> list[HopSuggestion]:
        """Suggest nodes reachable from ``tail``, strongest observed link first.

        This is the composer's step-by-step feed: standing at the path's current tail,
        every node the evidence says ``tail`` can hear (links are bidirectional, so a
        path observed in either direction qualifies) — minus the nodes already used.

        Args:
            tail: Canonical id the path currently ends at (our own id to start).
            exclude: Canonical ids to omit (nodes already in the path; revisiting one
                is never useful and duplicated hops are dropped by repeaters anyway).

        Returns:
            Suggestions sorted by descending link strength.
        """
        out: list[HopSuggestion] = []
        for (a, b), link in self._links.items():
            other = b if a == tail else a if b == tail else None
            if other is None or other in exclude:
                continue
            out.append(HopSuggestion(node=other, link=link, strength=link.strength(self._now)))
        out.sort(key=lambda s: (-s.strength, s.node))
        return out

    def scenarios(self, target: str, *, device_route: Optional[tuple[str, ...]] = None) -> list[PathScenario]:
        """Rank the candidate outbound routes for reaching ``target``.

        Three families, deduplicated in this priority order:

        1. **device** — the route the firmware itself learned from received floods (what
           an auto-routed trace would walk), when the caller supplies one;
        2. **direct** — no repeaters at all, always offered as the baseline;
        3. **observed** — the strongest simple paths through the evidence graph
           (weakest-link scoring with a per-hop penalty), which is where a path the
           device never learned — but the data supports — comes from.

        Every scenario is an *outbound* leg only: the trace reply retraces it in
        reverse automatically, so intermediate links are implicitly crossed twice.

        Args:
            target: The target's canonical id.
            device_route: The firmware's learned route (canonical intermediate hops),
                if the contact has one.

        Returns:
            Scenarios ranked best-evidence first (the device route, when present, is
            always listed first regardless of score — it is what "auto" would do).
        """
        seen: set[tuple[str, ...]] = set()
        out: list[PathScenario] = []

        def add(label: str, hops: tuple[str, ...], source: str) -> None:
            if hops in seen or len(hops) > _MAX_SCENARIO_HOPS or target in hops:
                return
            seen.add(hops)
            score, weakest, samples = self._score_route(hops, target)
            out.append(
                PathScenario(
                    label=label, hops=hops, source=source, score=score,
                    weakest_snr=weakest, samples=samples,
                )
            )

        if device_route is not None:
            add("device route", device_route, "device")
        add("direct", (), "direct")
        for hops in self._best_routes(target, k=_MAX_SCENARIOS):
            add("observed path", hops, "observed")

        # Rank observed evidence, but keep the device route pinned on top: it is the
        # reference point every alternative is compared against.
        head = [s for s in out if s.source == "device"]
        rest = sorted(
            (s for s in out if s.source != "device"),
            key=lambda s: (-s.score, len(s.hops)),
        )
        return (head + rest)[:_MAX_SCENARIOS]

    # --- internals -----------------------------------------------------------------

    def _score_route(
        self, hops: tuple[str, ...], target: str
    ) -> tuple[float, Optional[float], int]:
        """Score one outbound route by its weakest observed link.

        A chain is only as reliable as its weakest link, so the route's score is the
        minimum link strength along ``us → hops… → target``, discounted per hop (see
        :data:`_HOP_PENALTY`). A route containing a link we have *no* evidence for
        scores 0 — offerable, but ranked below anything actually observed.

        Args:
            hops: Intermediate canonical ids (may be empty for a direct route).
            target: The destination's canonical id.

        Returns:
            ``(score, weakest_median_snr, total_samples)``.
        """
        chain = [self.self_id, *hops, target]
        weakest_strength: Optional[float] = None
        weakest_snr: Optional[float] = None
        samples = 0
        for a, b in zip(chain, chain[1:]):
            link = self.link(a, b)
            if link is None:
                return 0.0, None, samples
            strength = link.strength(self._now)
            samples += link.samples
            if weakest_strength is None or strength < weakest_strength:
                weakest_strength = strength
            snr = link.median_snr
            if snr is not None and (weakest_snr is None or snr < weakest_snr):
                weakest_snr = snr
        score = (weakest_strength or 0.0) / (1.0 + _HOP_PENALTY * len(hops))
        return score, weakest_snr, samples

    def _best_routes(self, target: str, *, k: int) -> list[tuple[str, ...]]:
        """Find up to ``k`` strong simple paths from us to ``target`` in the graph.

        A best-first search over path cost (each link contributes ``1/strength``, plus
        the per-hop penalty), expanding the cheapest partial path until ``k`` complete
        routes emerge. The graph is small (tens of nodes), so exhaustive-ish search is
        fine; simple paths only, since revisiting a node is never useful.

        Args:
            target: The destination's canonical id.
            k: Maximum number of routes to return.

        Returns:
            The intermediate-hop tuples of the found routes, cheapest first.
        """
        neighbors: dict[str, list[tuple[str, float]]] = {}
        for (a, b), link in self._links.items():
            cost = 1.0 / max(link.strength(self._now), 1e-6) + _HOP_PENALTY
            neighbors.setdefault(a, []).append((b, cost))
            neighbors.setdefault(b, []).append((a, cost))

        found: list[tuple[str, ...]] = []
        counter = 0  # heap tiebreaker so equal-cost paths never compare tuples of str
        heap: list[tuple[float, int, tuple[str, ...]]] = [(0.0, counter, (self.self_id,))]
        while heap and len(found) < k:
            cost, _tie, path = heapq.heappop(heap)
            tail = path[-1]
            if tail == target:
                found.append(path[1:-1])  # strip both endpoints: hops only
                continue
            if len(path) - 1 > _MAX_SCENARIO_HOPS:
                continue
            for node, edge_cost in neighbors.get(tail, ()):
                if node in path:
                    continue
                counter += 1
                heapq.heappush(heap, (cost + edge_cost, counter, (*path, node)))
        return found


def build_topology(
    *,
    self_id: str,
    contacts: list[Contact],
    trace_paths: list[TracedPath],
    packet_paths: list[PacketPath],
) -> MeshTopology:
    """Assemble the evidence graph from everything we have received.

    Three sources fold in, each one walked through :meth:`MeshTopology.add_walk`:

    * **traces** — each successful trace's hop sequence, bracketed by us at both ends
      (the boomerang leaves us and returns to us; the final hash-less hop *is* us).
      Every hop's SNR is a reading on the link it arrived over.
    * **contact routes** — the firmware's learned ``out_path`` per contact: a chain from
      us through its repeaters to the contact. No SNR, but it is the distillation of
      real received floods, so it counts as one reading per link (stamped with the
      contact's last-heard time). A learned *direct* route is a direct link to us.
    * **packets** — RX-logged frames: the relay chain, prefixed by the originator when
      known and always ending at us (we heard the last relay). The measured SNR belongs
      to that final link only.

    Args:
        self_id: Our device's key/hash (any width; canonicalized to 12 hex).
        contacts: The device's known contacts (canonicalization + route evidence).
        trace_paths: Stored successful trace walks (see ``Repository.trace_paths``).
        packet_paths: Stored RX-logged packet paths (see ``Repository.packet_paths``).

    Returns:
        The populated :class:`MeshTopology`.
    """
    topo = MeshTopology(self_id, contacts)
    us = topo.self_id

    for traced in trace_paths:
        nodes: list[Optional[str]] = [us]
        snrs: list[Optional[float]] = []
        for hop, snr in traced.hops:
            # The final hash-less hop is the reply landing back at us.
            nodes.append(topo.canonical(hop) if hop is not None else us)
            snrs.append(snr)
        topo.add_walk(nodes, snrs=snrs, when=traced.when, source="trace")

    for contact in contacts:
        if contact.route_hops is None:
            continue
        contact_id = topo.canonical(contact.public_key or contact.key_prefix)
        route = [topo.canonical(h) for h in contact.route_hops]
        topo.add_walk(
            [us, *route, contact_id], when=contact.last_seen, source="route"
        )

    for packet in packet_paths:
        nodes = []
        if packet.origin:
            nodes.append(topo.canonical(packet.origin))
        nodes.extend(topo.canonical(h) for h in packet.hops)
        nodes.append(us)
        if len(nodes) < 2:
            continue
        # Only the last link (last relay → us) carries the measured reception SNR.
        snrs = [None] * (len(nodes) - 2) + [packet.snr]
        topo.add_walk(nodes, snrs=snrs, when=packet.when, source="packet")

    return topo
