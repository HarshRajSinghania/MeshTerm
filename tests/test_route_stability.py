"""Tests for route-stability aggregation and the pyvis renderer."""

from __future__ import annotations

import math
from pathlib import Path

from meshtools.core.models import Hop, TraceResult
from meshtools.services import route_stability
from meshtools.services.route_stability import RouteGraph


def _trace(*nodes: str, snr: float = 5.0) -> TraceResult:
    """Build a successful trace whose forward path visits ``nodes`` then returns to us.

    Each hop SNR is fixed; the final hash-less hop models the reply returning to us.
    """
    hops = [Hop(i, node, snr) for i, node in enumerate(nodes)]
    hops.append(Hop(len(nodes), None, snr))
    return TraceResult(target=nodes[-1] if nodes else "x", success=True, hops=hops)


# -- churn metrics ----------------------------------------------------------


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


def test_three_way_split_entropy() -> None:
    """Three equally-likely routes give log2(3) ≈ 1.585 bits of entropy."""
    traces = (
        [_trace("a", "x") for _ in range(4)]
        + [_trace("b", "x") for _ in range(4)]
        + [_trace("c", "x") for _ in range(4)]
    )
    graph = route_stability.build_route_graph(traces, target="x")
    assert graph.distinct_routes == 3
    assert math.isclose(graph.churn_entropy, math.log2(3), rel_tol=1e-9)


def test_stability_property_equals_dominant_share() -> None:
    """The stability property is an alias for dominant_share."""
    graph = RouteGraph(target="x", dominant_share=0.75)
    assert graph.stability == 0.75


# -- graph topology ---------------------------------------------------------


def test_links_aggregate_counts_and_self_node() -> None:
    """Links carry traversal counts and our own device is marked is_self."""
    traces = [_trace("3d", "f2") for _ in range(4)]
    graph = route_stability.build_route_graph(traces, target="f2", device_label="us")
    by_pair = {(link.origin, link.destination): link for link in graph.links}
    assert by_pair[("us", "3d")].count == 4  # every trace starts us → 3d
    assert by_pair[("f2", "us")].count == 4  # and returns f2 → us
    self_nodes = [n for n in graph.nodes if n.is_self]
    assert len(self_nodes) == 1 and self_nodes[0].label == "us"


def test_single_hop_direct_link() -> None:
    """A one-hop trace (direct link, no repeaters) produces a two-edge diamond."""
    traces = [_trace("Alice") for _ in range(5)]
    graph = route_stability.build_route_graph(traces, target="Alice")
    assert graph.total_routes == 5
    assert graph.distinct_routes == 1
    by_pair = {(l.origin, l.destination): l for l in graph.links}
    assert ("us", "Alice") in by_pair
    assert ("Alice", "us") in by_pair


def test_median_snr_on_links() -> None:
    """Links carry the median SNR across all traversals."""
    traces = [
        _trace("relay", "dest", snr=v)
        for v in [2.0, 4.0, 6.0, 8.0, 10.0]
    ]
    graph = route_stability.build_route_graph(traces, target="dest")
    by_pair = {(l.origin, l.destination): l for l in graph.links}
    assert by_pair[("us", "relay")].median_snr == 6.0  # median of 2,4,6,8,10


def test_links_sorted_by_count_descending() -> None:
    """Links come back sorted by traversal count, busiest first."""
    traces = [_trace("a", "x")] + [_trace("b", "x") for _ in range(5)]
    graph = route_stability.build_route_graph(traces, target="x")
    counts = [link.count for link in graph.links]
    assert counts == sorted(counts, reverse=True)


def test_node_degree_reflects_link_count() -> None:
    """Each node's degree equals the number of distinct links touching it."""
    # Two different routes: us→relay→dest→us and us→alt→dest→us
    # "relay" touches (us,relay) and (relay,dest) → degree 2
    # "dest" touches (relay,dest), (alt,dest), (dest,us) → degree 3
    traces = [_trace("relay", "dest") for _ in range(3)] + [_trace("alt", "dest")]
    graph = route_stability.build_route_graph(traces, target="dest")
    by_label = {n.label: n for n in graph.nodes}
    assert by_label["dest"].degree == 3
    assert by_label["relay"].degree == 2


# -- resolver / labelling ---------------------------------------------------


def test_resolver_relabels_nodes() -> None:
    """A resolver renames hop hashes throughout the graph."""
    traces = [_trace("3d", "f2") for _ in range(3)]
    graph = route_stability.build_route_graph(
        traces, target="f2", resolve=lambda h: {"3d": "Repeater", "f2": "Alice"}.get(h, h)
    )
    labels = {n.label for n in graph.nodes}
    assert "Repeater" in labels
    assert "Alice" in labels


def test_resolver_returning_none_keeps_original_label() -> None:
    """When the resolver returns None for a hash, the original label is kept."""
    traces = [_trace("3d", "f2") for _ in range(2)]
    graph = route_stability.build_route_graph(
        traces, target="f2", resolve=lambda h: None
    )
    labels = {n.label for n in graph.nodes}
    assert "3d" in labels
    assert "f2" in labels


def test_resolver_renames_self_device() -> None:
    """When the resolver renames the device_label, the self node follows."""
    traces = [_trace("relay", "dest") for _ in range(2)]
    graph = route_stability.build_route_graph(
        traces, target="dest",
        resolve=lambda h: "MyDevice" if h == "us" else h,
    )
    self_nodes = [n for n in graph.nodes if n.is_self]
    assert len(self_nodes) == 1
    assert self_nodes[0].label == "MyDevice"


# -- edge cases -------------------------------------------------------------


def test_empty_history_is_safe() -> None:
    """Failed/empty traces yield an empty graph with zeroed metrics, no errors."""
    graph = route_stability.build_route_graph(
        [TraceResult(target="x", success=False)], target="x"
    )
    assert graph.total_routes == 0
    assert graph.stability == 0.0
    assert graph.links == []
    assert graph.nodes == []


def test_completely_empty_trace_list() -> None:
    """An empty list produces a valid zeroed graph."""
    graph = route_stability.build_route_graph([], target="x")
    assert graph.total_routes == 0
    assert graph.distinct_routes == 0
    assert graph.churn_entropy == 0.0


def test_mixed_success_and_failure_traces() -> None:
    """Failed traces are excluded from the graph; only successes contribute."""
    good = [_trace("relay", "dest") for _ in range(3)]
    bad = [TraceResult(target="dest", success=False) for _ in range(5)]
    graph = route_stability.build_route_graph(good + bad, target="dest")
    assert graph.total_routes == 3  # only the successful ones


def test_multi_target_graph_without_target() -> None:
    """A whole-mesh graph (target=None) merges traces across targets."""
    t1 = [_trace("relay", "Alice") for _ in range(3)]
    t2 = [_trace("relay", "Bob") for _ in range(2)]
    graph = route_stability.build_route_graph(t1 + t2, target=None)
    assert graph.target is None
    assert graph.total_routes == 5
    labels = {n.label for n in graph.nodes}
    assert "Alice" in labels
    assert "Bob" in labels


# -- pyvis renderer ---------------------------------------------------------


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


def test_render_empty_graph_writes_valid_html(tmp_path: Path) -> None:
    """An empty graph still produces a valid HTML file."""
    from meshtools.viz.route_graph import render_route_graph

    graph = RouteGraph(target="nobody")
    out = render_route_graph(graph, tmp_path)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "<body>" in text


def test_render_creates_output_dir(tmp_path: Path) -> None:
    """The renderer creates the output directory if it doesn't exist."""
    from meshtools.viz.route_graph import render_route_graph

    nested = tmp_path / "a" / "b"
    graph = route_stability.build_route_graph([_trace("x")], target="x")
    out = render_route_graph(graph, nested)
    assert out.exists()


def test_render_whole_mesh_filename(tmp_path: Path) -> None:
    """Whole-mesh graphs use 'mesh' in the filename, not an empty slug."""
    from meshtools.viz.route_graph import render_route_graph

    graph = route_stability.build_route_graph([_trace("x")], target=None)
    out = render_route_graph(graph, tmp_path)
    assert "mesh" in out.name


# -- viz helpers -------------------------------------------------------------


def test_snr_color_thresholds() -> None:
    """SNR-to-colour mapping covers all four bands."""
    from meshtools.viz.route_graph import _snr_color

    assert _snr_color(10.0) == "#4ade80"   # strong
    assert _snr_color(5.0) == "#4ade80"    # boundary: strong
    assert _snr_color(2.0) == "#facc15"    # marginal
    assert _snr_color(0.0) == "#facc15"    # boundary: marginal
    assert _snr_color(-4.0) == "#fb923c"   # weak
    assert _snr_color(-8.0) == "#fb923c"   # boundary: weak
    assert _snr_color(-15.0) == "#f87171"  # very weak


def test_slug_edge_cases() -> None:
    """Slug handles special characters and pathological inputs."""
    from meshtools.viz.route_graph import _slug

    assert _slug("Alice") == "alice"
    assert _slug("my node") == "my-node"
    assert _slug("a/b\\c") == "a-b-c"
    assert _slug("") == "graph"             # empty → fallback
    assert _slug("!!!") == "graph"          # all-special → fallback


# -- tool panel / validator --------------------------------------------------


def test_graph_panel_with_links() -> None:
    """The Rich panel renders without error when there are links."""
    from meshtools.tools.route_map import _graph_panel

    traces = [_trace("relay", "dest") for _ in range(3)]
    graph = route_stability.build_route_graph(traces, target="dest")
    panel = _graph_panel(graph)
    assert panel.title is not None


def test_graph_panel_empty() -> None:
    """The Rich panel renders without error on an empty graph."""
    from meshtools.tools.route_map import _graph_panel

    graph = RouteGraph(target="nobody")
    panel = _graph_panel(graph)
    assert panel.title is not None


def test_is_nonneg_int_validator() -> None:
    """The questionary validator accepts zero/positive and rejects negative/non-int."""
    from meshtools.tools.route_map import _is_nonneg_int

    assert _is_nonneg_int("0") is True
    assert _is_nonneg_int("5") is True
    assert isinstance(_is_nonneg_int("-1"), str)
    assert isinstance(_is_nonneg_int("abc"), str)
