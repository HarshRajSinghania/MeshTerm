"""The observed mesh topology: a link graph distilled from every path we ever received.

MeshCore never tells us the network's shape outright, but almost everything we *receive*
carries a fragment of it: a successful trace demonstrably crossed every link in its path
(out and back, with an SNR reading at each hop), the firmware's per-contact ``out_path``
is a route distilled from received floods, and — when the companion's packet logging is
on — every overheard frame reports the relay chain it rode in on. A fourth fragment can
be *asked for*: a repeater we hold admin rights on reports its own neighbour table (who
it hears directly, at what SNR) when queried over the mesh — a second vantage point that
reveals links, and whole nodes, our radio has never received anything from. This module
folds those fragments into one undirected evidence graph and answers the questions the
trace path composer asks of it:

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
from ..persistence.repository import NeighbourLink, PacketPath, TracedPath

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


def render_forced_spec(hops: tuple[str, ...], target_hash: str, width_bytes: int) -> str:
    """Render a symmetric forced-path spec: outbound hops, the target, the mirror back.

    The MeshCore trace protocol has no separate "return path" field — the whole
    boomerang (out to the target, back to us) is one path the repeaters walk in
    order, so the spec must spell out the return hops explicitly, not just the
    outbound ones. This renders the *symmetric* round trip — the return leg is the
    outbound hops in reverse, appended here rather than asked of the caller. A
    hand-composed route that comes home a different way is rendered verbatim by
    :func:`render_custom_spec` instead.

    Args:
        hops: Intermediate repeaters, in order from us outward, excluding both
            endpoints (empty = trace the target directly, no forced hops).
        target_hash: The target's hex hash (any width ≥ 1 byte).
        width_bytes: Preferred per-hop path-hash width in bytes (1, 2, 4, or 8);
            shrunk via :func:`collapse_width` when a hop's known hash is narrower.

    Returns:
        A comma-separated hex spec, e.g. ``"3d,f2,3d"`` for one forced hop.
    """
    width = collapse_width(*hops, target_hash, ceiling=width_bytes)
    outbound = [h[: width * 2] for h in (*hops, target_hash)]
    outbound.extend(h[: width * 2] for h in reversed(hops))
    return ",".join(outbound)


def render_custom_spec(hops: tuple[str, ...], width_bytes: int) -> str:
    """Render a hand-composed (asymmetric) path spec exactly as given.

    The counterpart of :func:`render_forced_spec` for a route the user spelled out in
    full — out to the target and back home by whatever way they chose. Nothing is
    appended or mirrored: the hops *are* the path, and the trace protocol happily
    walks any sequence that ends within earshot of us. Only the shared width rules
    apply.

    Args:
        hops: Every hop of the route in walk order, the target among them, excluding
            our own device at both ends (the reply lands on us off the final hop).
        width_bytes: Preferred per-hop path-hash width in bytes (1, 2, 4, or 8);
            shrunk via :func:`collapse_width` when a hop's known hash is narrower.

    Returns:
        A comma-separated hex spec, e.g. ``"3d,f2,27"`` — or ``""`` with no hops
        (there is no meaningful zero-hop custom route).
    """
    if not hops:
        return ""
    width = collapse_width(*hops, ceiling=width_bytes)
    return ",".join(h[: width * 2] for h in hops)


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
        sources: Which evidence classes saw it (``trace`` / ``route`` / ``packet`` /
            ``neighbour``).
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

    A scenario stores only the *outbound* leg — the return is always the symmetric
    mirror, so there's nothing to choose there — but :meth:`spec` renders the full
    boomerang, since the trace protocol has no separate return-path field: the whole
    out-and-back route is one spec the repeaters walk in order.

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
        """Render the scenario as a forced-path spec, return leg included.

        See :func:`render_forced_spec` for the width-collapsing and return-leg rules.

        Args:
            target_hash: The target's hex hash (any width ≥ 1 byte).
            width_bytes: Preferred per-hop path-hash width in bytes (1, 2, 4, or 8).

        Returns:
            A comma-separated hex spec, e.g. ``"3d,f2,3d"``.
        """
        return render_forced_spec(self.hops, target_hash, width_bytes)


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
            source: Evidence class tag (``trace`` / ``route`` / ``packet`` /
                ``neighbour``).
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

    def coalesce_prefixes(self) -> None:
        """Fold each under-specified node id into the longer id it can only be.

        The same physical node reaches the graph at different hash widths: a trace hop
        logged at 1 byte (``27``), a packet relay at 3 (``27d439``), a full 6-byte
        contact id (``27d4396a2967``). :meth:`canonical` widens a hop onto a contact id
        only when the prefix names *exactly one* contact — but the mesh holds two nodes
        whose keys both open ``27`` (``27d4…`` and ``27e4…``), so the bare ``27`` hop
        stays short and stands beside the wide ``27d4396a2967`` as a phantom second node.
        Left alone that doubles the node on every surface the graph feeds — the atlas
        draws it twice, the route graph fans two lanes to one repeater, and the scenario
        ranker offers a redundant weaker route through the stub.

        This closes the gap using the evidence the graph *actually holds* rather than the
        whole contact list: a short id is merged into a longer node id present in the graph
        when it is a strict prefix of **exactly one** of them (following a prefix chain —
        ``65`` → ``6532`` → ``6532eb`` — to its longest end). A short id that opens two
        distinct longer nodes (``c5`` → ``c5bc…`` and ``c5ba…``) is genuinely ambiguous and
        is left as its own node; a short id that opens none (a node we have only ever heard
        narrowly) keeps its width too — it is one node, merely under-named. Merging folds
        the short id's links into the wide id's, summing samples, pooling SNR readings and
        sources, and keeping the freshest sighting, so the wide node inherits every reading
        the stub had gathered. Idempotent: a second call finds nothing left to merge.

        Called once at the end of :func:`build_topology`, so every consumer sees each node
        once. Safe for our own node (its 12-hex id is never a short prefix candidate).
        """
        while True:
            merge = self._next_prefix_merge(self._node_ids())
            if merge is None:
                return
            self._merge_node(*merge)

    def _node_ids(self) -> set[str]:
        """Every node id that currently appears as a link endpoint."""
        ids: set[str] = set()
        for a, b in self._links:
            ids.add(a)
            ids.add(b)
        return ids

    def _next_prefix_merge(self, nodes: set[str]) -> Optional[tuple[str, str]]:
        """The next ``(short, long)`` pair to fold, or ``None`` when none remains.

        A short id (under a full 6-byte canonical width) folds when the graph's longer
        ids that extend it all lie on one prefix chain — i.e. the longest of them starts
        with every other — so the short can only mean that one node. Shortest ids are
        offered first, so a ``65`` → ``6532`` → ``6532eb`` chain collapses from the tail
        end inward over successive calls.
        """
        for short in sorted(nodes, key=len):
            if len(short) >= 12:  # a full canonical id is never under-specified
                continue
            exts = [
                other
                for other in nodes
                if other != short and len(other) > len(short) and other.startswith(short)
            ]
            if not exts:
                continue
            longest = max(exts, key=len)
            if all(longest.startswith(ext) for ext in exts):  # one node, not two
                return short, longest
        return None

    def _merge_node(self, src: str, dst: str) -> None:
        """Relabel every link touching ``src`` onto ``dst``, folding shared links together."""
        for key in list(self._links):
            if src not in key:
                continue
            link = self._links.pop(key)
            a, b = key
            na = dst if a == src else a
            nb = dst if b == src else b
            if na == nb:  # a src→dst link (src prefixes dst) collapses to a self-loop
                continue
            new_key = (na, nb) if na < nb else (nb, na)
            existing = self._links.get(new_key)
            if existing is None:
                self._links[new_key] = Link(
                    a=new_key[0], b=new_key[1],
                    samples=link.samples, snrs=list(link.snrs),
                    last_seen=link.last_seen, sources=set(link.sources),
                )
            else:
                existing.samples += link.samples
                existing.snrs.extend(link.snrs)
                existing.sources |= link.sources
                if link.last_seen is not None and (
                    existing.last_seen is None or link.last_seen > existing.last_seen
                ):
                    existing.last_seen = link.last_seen

    # --- queries -----------------------------------------------------------------

    @property
    def link_count(self) -> int:
        """How many distinct links the graph holds evidence for."""
        return len(self._links)

    def links(self) -> list[Link]:
        """Every link the graph holds evidence for (no particular order).

        The whole-graph view the Mesh Atlas draws; path queries should prefer
        :meth:`next_hops` / :meth:`scenarios`, which rank and filter.
        """
        return list(self._links.values())

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

        Every scenario stores an *outbound* leg only — the return is always those hops
        mirrored, so there's nothing to rank there — but :meth:`PathScenario.spec`
        renders the full boomerang; intermediate links are implicitly crossed twice.

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

    def suggested(self, target: str) -> Optional[PathScenario]:
        """The single best *evidence-backed* outbound route to ``target``, if one exists.

        The data-driven answer to "what's the best way to reach this node?": the highest-
        scoring **observed** scenario — a route through repeaters the recorder has actually
        watched carry traffic (traces, overheard relay chains, fetched neighbour tables),
        ranked weakest-link-first with a per-hop penalty and its links' sample counts and
        median SNR behind the score. It deliberately ignores the *device* and *direct*
        families :meth:`scenarios` also offers: the firmware's learned route is what an
        auto-routed trace already walks (the caller prefers it when it has one), and a
        direct shot is the trivial fallback — neither is a *suggestion* the observations
        made. A node the evidence can only reach directly (or not at all) yields ``None``,
        so the caller keeps whatever default it would have used.

        Args:
            target: The target's canonical id.

        Returns:
            The best observed :class:`PathScenario` (always with at least one intermediate
            hop and a positive, evidence-backed score), or ``None`` when the observations
            support nothing better than a direct route.
        """
        for scenario in self.scenarios(target):
            if scenario.source == "observed" and scenario.hops and scenario.score > 0:
                return scenario
        return None

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
        """Find up to ``k`` strong simple paths from us to ``target``, cheapest first.

        Yen's k-shortest-loopless-paths over the evidence graph. Each link costs
        ``1/strength + _HOP_PENALTY`` — a weak or roundabout way scores dearer — and the
        routes come back ranked by total cost, cheapest first (the candidate set
        :meth:`scenarios` then re-scores by weakest link). Simple (loopless) paths only,
        since revisiting a node is never useful, and only routes within
        :data:`_MAX_SCENARIO_HOPS` intermediate hops — a longer chain is more airtime and
        more failure points than a trace should carry (and it is crossed twice).

        Yen's builds on plain Dijkstra rather than enumerating simple paths with a
        best-first frontier, because that frontier is pathological on a real mesh graph.
        A well-heard core has cheap links everywhere, so a partial-path search *floods*
        it — every wandering prefix through the core costs less than the one weak link a
        distant target sits behind — exploring millions of dead-end prefixes before it
        reaches the target (tens of seconds for a busy node). Yen's instead finds the
        single shortest path, then derives each next-shortest by rerouting around one
        edge of the previous one, so the work stays polynomial in the graph size however
        dense the core: the same routes, in the same cost order (ties aside), in
        milliseconds.

        Args:
            target: The destination's canonical id.
            k: Maximum number of routes to return.

        Returns:
            The intermediate-hop tuples of the found routes (endpoints stripped),
            cheapest total-cost first.
        """
        neighbors: dict[str, list[tuple[str, float]]] = {}
        for (a, b), link in self._links.items():
            cost = 1.0 / max(link.strength(self._now), 1e-6) + _HOP_PENALTY
            neighbors.setdefault(a, []).append((b, cost))
            neighbors.setdefault(b, []).append((a, cost))

        def shortest(
            source: str, banned_nodes: set[str], banned_edges: set[tuple[str, str]]
        ) -> Optional[tuple[float, list[str]]]:
            """Dijkstra ``source``→``target`` avoiding the banned nodes/edges, or ``None``."""
            dist: dict[str, float] = {source: 0.0}
            prev: dict[str, str] = {}
            heap: list[tuple[float, str]] = [(0.0, source)]
            done: set[str] = set()
            while heap:
                d, u = heapq.heappop(heap)
                if u in done:
                    continue
                done.add(u)
                if u == target:
                    break
                for v, cost in neighbors.get(u, ()):
                    if v in banned_nodes or (u, v) in banned_edges:
                        continue
                    nd = d + cost
                    if nd < dist.get(v, math.inf):
                        dist[v] = nd
                        prev[v] = u
                        heapq.heappush(heap, (nd, v))
            if target not in dist:
                return None
            path = [target]
            while path[-1] != source:
                path.append(prev[path[-1]])
            path.reverse()
            return dist[target], path

        def path_cost(path: list[str]) -> float:
            total = 0.0
            for a, b in zip(path, path[1:]):
                total += min((c for v, c in neighbors.get(a, ()) if v == b), default=math.inf)
            return total

        first = shortest(self.self_id, set(), set())
        if first is None or len(first[1]) - 2 > _MAX_SCENARIO_HOPS:
            return []
        accepted: list[list[str]] = [first[1]]
        # Spur candidates, a min-heap by total cost (counter tiebreaker so equal-cost
        # candidates never fall through to comparing the path lists themselves).
        candidates: list[tuple[float, int, list[str]]] = []
        seen: set[tuple[str, ...]] = {tuple(first[1])}
        counter = 0
        while len(accepted) < k:
            prev_path = accepted[-1]
            for i in range(len(prev_path) - 1):
                spur = prev_path[i]
                root = prev_path[: i + 1]
                # Ban the next edge every accepted path with this same root took, so the
                # spur is forced onto a genuinely different route out of the spur node...
                banned_edges: set[tuple[str, str]] = set()
                for p in accepted:
                    if p[: i + 1] == root and len(p) > i + 1:
                        banned_edges.add((p[i], p[i + 1]))
                        banned_edges.add((p[i + 1], p[i]))
                # ...and ban the root's interior nodes so the detour stays loopless.
                banned_nodes = set(root[:-1])
                spur_result = shortest(spur, banned_nodes, banned_edges)
                if spur_result is None:
                    continue
                full = root[:-1] + spur_result[1]
                if len(full) - 2 > _MAX_SCENARIO_HOPS:
                    continue
                key = tuple(full)
                if key in seen:
                    continue
                seen.add(key)
                counter += 1
                heapq.heappush(candidates, (path_cost(full), counter, full))
            if not candidates:
                break
            _, _, best_path = heapq.heappop(candidates)
            accepted.append(best_path)
        return [tuple(path[1:-1]) for path in accepted]


def build_topology(
    *,
    self_id: str,
    contacts: list[Contact],
    trace_paths: list[TracedPath],
    packet_paths: list[PacketPath],
    neighbour_links: tuple[NeighbourLink, ...] | list[NeighbourLink] = (),
) -> MeshTopology:
    """Assemble the evidence graph from everything we have received — or asked for.

    Four sources fold in, each one walked through :meth:`MeshTopology.add_walk`:

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
    * **neighbour reports** — links a remote repeater asserted about itself when we
      fetched its neighbour table: one two-node walk per entry, SNR as measured at the
      repeater. The only evidence here not derived from our own reception — it can
      introduce nodes no other source has ever seen.

    Args:
        self_id: Our device's key/hash (any width; canonicalized to 12 hex).
        contacts: The device's known contacts (canonicalization + route evidence).
        trace_paths: Stored successful trace walks (see ``Repository.trace_paths``).
        packet_paths: Stored RX-logged packet paths (see ``Repository.packet_paths``).
        neighbour_links: Current repeater-reported links (see
            ``Repository.neighbour_links``); empty when none have been fetched.

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

    for reported in neighbour_links:
        topo.add_walk(
            [topo.canonical(reported.repeater), topo.canonical(reported.neighbour)],
            snrs=[reported.snr],
            when=reported.when,
            source="neighbour",
        )

    # Every source has folded in; collapse any node that reached us at two hash widths
    # (a bare trace hop beside its wide contact id) so each appears once everywhere.
    topo.coalesce_prefixes()
    return topo
