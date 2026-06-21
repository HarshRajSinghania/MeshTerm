"""Tests for route-stability aggregation and the pyvis renderer."""

from __future__ import annotations

from pathlib import Path

from meshtools.core.models import Hop, TraceResult
from meshtools.services import route_stability


def _trace(*nodes: str) -> TraceResult:
    """Build a successful trace whose forward path visits ``nodes`` then returns to us.

    Each hop SNR is fixed; the final hash-less hop models the reply returning to us.
    """
    hops = [Hop(i, node, 5.0) for i, node in enumerate(nodes)]
    hops.append(Hop(len(nodes), None, 5.0))
    return TraceResult(target=nodes[-1] if nodes else "x", success=True, hops=hops)


def test_stable_route_scores_one() -> None:
    """A single route taken every time scores full stability and zero entropy."""
    traces = [_trace("3d", "f2") for _ in range(6)]
    graph = route_stability.build_route_graph(traces, target="f2")
    assert graph.total_routes == 6
    assert graph.distinct_routes == 1
    assert graph.stability == 1.0
    assert graph.churn_entropy == 0.0


def test_flapping_routes_lower_stability_and_raise_entropy() -> None:
    """An even split between two routes halves stability and gives 1 bit of entropy."""
    traces = [_trace("3d", "f2") for _ in range(3)] + [_trace("aa", "f2") for _ in range(3)]
    graph = route_stability.build_route_graph(traces, target="f2")
    assert graph.distinct_routes == 2
    assert graph.stability == 0.5
    assert graph.churn_entropy == 1.0  # two equally-likely routes


def test_links_aggregate_counts_and_self_node() -> None:
    """Links carry traversal counts and our own device is marked is_self."""
    traces = [_trace("3d", "f2") for _ in range(4)]
    graph = route_stability.build_route_graph(traces, target="f2", device_label="us")
    by_pair = {(link.origin, link.destination): link for link in graph.links}
    assert by_pair[("us", "3d")].count == 4  # every trace starts us → 3d
    assert by_pair[("f2", "us")].count == 4  # and returns f2 → us
    self_nodes = [n for n in graph.nodes if n.is_self]
    assert len(self_nodes) == 1 and self_nodes[0].label == "us"


def test_resolver_relabels_nodes() -> None:
    """A resolver renames hop hashes throughout the graph."""
    traces = [_trace("3d", "f2") for _ in range(3)]
    graph = route_stability.build_route_graph(
        traces, target="f2", resolve=lambda h: {"3d": "Repeater", "f2": "Alice"}.get(h, h)
    )
    labels = {n.label for n in graph.nodes}
    assert "Repeater" in labels
    assert "Alice" in labels


def test_empty_history_is_safe() -> None:
    """Failed/empty traces yield an empty graph with zeroed metrics, no errors."""
    graph = route_stability.build_route_graph(
        [TraceResult(target="x", success=False)], target="x"
    )
    assert graph.total_routes == 0
    assert graph.stability == 0.0
    assert graph.links == []


def test_render_route_graph_writes_html(tmp_path: Path) -> None:
    """The pyvis renderer produces a self-contained HTML file."""
    from meshtools.viz.route_graph import render_route_graph

    traces = [_trace("3d", "f2") for _ in range(3)] + [_trace("aa", "f2")]
    graph = route_stability.build_route_graph(traces, target="f2")
    out = render_route_graph(graph, tmp_path)
    assert out.exists()
    assert out.suffix == ".html"
    text = out.read_text(encoding="utf-8")
    assert "Route stability" in text  # heading injected
    assert out.stat().st_size > 1000  # vis.js inlined
