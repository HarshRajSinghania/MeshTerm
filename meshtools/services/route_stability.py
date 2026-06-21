"""Route-stability aggregation: turn trace history into a mesh graph with churn metrics.

The MeshCore app traces a route once. Because MeshTools persists every trace, it can
instead show how *stable* routing is over time — which hops are always used and which the
mesh flaps between. This service aggregates a set of traces into:

* a **graph** of nodes and directed links, each link weighted by how often it was
  traversed and its median SNR; and
* per-target **churn** metrics: how many distinct routes were seen and what share the
  single most-common route held (the higher that share, the steadier the routing).

It is pure logic over :class:`~meshtools.core.models.TraceResult` lists; the pyvis
renderer in :mod:`meshtools.viz.route_graph` draws the result.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..core.models import LOCAL_DEVICE_LABEL, TraceResult

NodeResolver = Callable[[Optional[str]], Optional[str]]


@dataclass(slots=True)
class RouteLink:
    """A directed link between two nodes, aggregated across traces.

    Attributes:
        origin: Transmitting node label.
        destination: Receiving node label.
        count: Number of trace hops that traversed this link.
        median_snr: Median SNR (dB) measured on it.
    """

    origin: str
    destination: str
    count: int
    median_snr: float


@dataclass(slots=True)
class RouteNode:
    """A node in the route graph.

    Attributes:
        label: Display label for the node.
        is_self: Whether this is our own companion device.
        degree: Number of distinct links (in or out) touching the node.
    """

    label: str
    is_self: bool
    degree: int


@dataclass(slots=True)
class RouteGraph:
    """An aggregated route graph plus churn metrics.

    Attributes:
        target: The single target analyzed, or ``None`` for a whole-mesh graph.
        nodes: The graph's nodes.
        links: The graph's directed links.
        total_routes: Number of successful traces aggregated.
        distinct_routes: Number of unique full-path signatures seen.
        dominant_share: Fraction of traces that took the single most-common route.
        churn_entropy: Shannon entropy (bits) of the route-choice distribution — ``0``
            when one route is always chosen, growing as traffic spreads across more
            equally-likely routes.
    """

    target: Optional[str]
    nodes: list[RouteNode] = field(default_factory=list)
    links: list[RouteLink] = field(default_factory=list)
    total_routes: int = 0
    distinct_routes: int = 0
    dominant_share: float = 0.0
    churn_entropy: float = 0.0

    @property
    def stability(self) -> float:
        """A 0-1 stability score: ``1`` when one route always wins, lower as it flaps.

        Defined as the dominant route's share, so a single stable path scores ``1.0`` and
        an even split across many routes trends toward ``0``.
        """
        return self.dominant_share


def build_route_graph(
    traces: list[TraceResult],
    *,
    target: Optional[str] = None,
    resolve: Optional[NodeResolver] = None,
    device_label: str = LOCAL_DEVICE_LABEL,
) -> RouteGraph:
    """Aggregate traces into a route graph with churn metrics.

    Args:
        traces: The traces to aggregate (only successful ones contribute edges/routes).
        target: The target these traces share, or ``None`` for a multi-target graph.
        resolve: Optional resolver mapping a hop hash to a friendly node name.
        device_label: Label for our own device at the path endpoints.

    Returns:
        A populated :class:`RouteGraph`. With no successful traces the graph is empty and
        its metrics are zero.
    """
    successes = [t for t in traces if t.success and t.hops]
    link_snrs: dict[tuple[str, str], list[float]] = {}
    route_counts: dict[tuple[str, ...], int] = {}
    node_links: dict[str, set[tuple[str, str]]] = {}

    for trace in successes:
        edges = trace.edges(device_label)
        signature: list[str] = []
        for edge in edges:
            origin = _name(edge.origin, resolve)
            destination = _name(edge.destination, resolve)
            key = (origin, destination)
            link_snrs.setdefault(key, []).append(edge.snr)
            node_links.setdefault(origin, set()).add(key)
            node_links.setdefault(destination, set()).add(key)
            if not signature:
                signature.append(origin)
            signature.append(destination)
        route_counts[tuple(signature)] = route_counts.get(tuple(signature), 0) + 1

    links = [
        RouteLink(
            origin=origin,
            destination=destination,
            count=len(snrs),
            median_snr=statistics.median(snrs),
        )
        for (origin, destination), snrs in link_snrs.items()
    ]
    links.sort(key=lambda link_item: link_item.count, reverse=True)

    self_label = _name(None, resolve) or device_label
    nodes = [
        RouteNode(label=label, is_self=(label == self_label), degree=len(keys))
        for label, keys in sorted(node_links.items(), key=lambda kv: len(kv[1]), reverse=True)
    ]

    total = len(successes)
    dominant_share = (max(route_counts.values()) / total) if total else 0.0
    return RouteGraph(
        target=target,
        nodes=nodes,
        links=links,
        total_routes=total,
        distinct_routes=len(route_counts),
        dominant_share=dominant_share,
        churn_entropy=_route_entropy(route_counts, total),
    )


def _route_entropy(route_counts: dict[tuple[str, ...], int], total: int) -> float:
    """Return the Shannon entropy (bits) of a route-choice distribution.

    Args:
        route_counts: Map of route signature to how often it occurred.
        total: Total number of routes counted.

    Returns:
        The entropy in bits (``0`` for a single route).
    """
    if total <= 0:
        return 0.0
    entropy = 0.0
    for count in route_counts.values():
        p = count / total
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def _name(label: Optional[str], resolve: Optional[NodeResolver]) -> str:
    """Resolve a hop label to a friendly name, leaving our own device's label intact."""
    if label is None:
        resolved = resolve(None) if resolve else None
        return resolved or LOCAL_DEVICE_LABEL
    if resolve is None:
        return label
    return resolve(label) or label
