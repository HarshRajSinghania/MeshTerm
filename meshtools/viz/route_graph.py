"""pyvis renderer for the route-stability graph.

Draws the aggregated mesh routing as a self-contained, interactive HTML graph: nodes are
mesh devices (our companion highlighted), directed edges are links whose width grows with
how often the link was traversed and whose colour tracks median SNR. The title carries the
churn metrics so the picture and the numbers travel together.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..services.route_stability import RouteGraph

_SELF_COLOR = "#4ade80"
_NODE_COLOR = "#5eead4"
_BG = "#0f172a"
_FONT = "#e2e8f0"


def render_route_graph(graph: RouteGraph, output_dir: Path) -> Path:
    """Render a route graph to an interactive, self-contained HTML file.

    Args:
        graph: The aggregated route graph to draw.
        output_dir: Directory the HTML file is written to (created if needed).

    Returns:
        Path to the written HTML file.
    """
    from pyvis.network import Network

    # ``cdn_resources='in_line'`` inlines vis.js so the file works offline, matching the
    # self-contained Plotly charts elsewhere.
    net = Network(
        height="720px",
        width="100%",
        directed=True,
        bgcolor=_BG,
        font_color=_FONT,
        cdn_resources="in_line",
    )

    for node in graph.nodes:
        color = _SELF_COLOR if node.is_self else _NODE_COLOR
        net.add_node(
            node.label,
            label=node.label,
            color=color,
            shape="star" if node.is_self else "dot",
            size=14 + 3 * node.degree,
            title=f"{node.label} · {node.degree} link(s)",
        )

    max_count = max((link.count for link in graph.links), default=1)
    for link in graph.links:
        width = 1.0 + 6.0 * (link.count / max_count)
        net.add_edge(
            link.origin,
            link.destination,
            value=link.count,
            width=width,
            color=_snr_color(link.median_snr),
            title=f"{link.origin} → {link.destination}: "
            f"{link.count}× · median {link.median_snr:+.1f} dB",
        )

    net.set_options(_OPTIONS)

    scope = graph.target or "mesh"
    heading = (
        f"Route stability — {scope}  ·  {graph.total_routes} traces, "
        f"{graph.distinct_routes} route(s), {graph.stability:.0%} on the top route"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"route-map_{_slug(scope)}_{stamp}.html"
    html = net.generate_html(notebook=False)
    banner = f"<h2 style='color:{_FONT};font-family:sans-serif'>{heading}</h2>"
    html = html.replace("<body>", f"<body>{banner}")
    path.write_text(html, encoding="utf-8")
    return path


def _snr_color(snr: float) -> str:
    """Map a median SNR (dB) to an edge colour from red (weak) to green (strong).

    Args:
        snr: Median SNR in dB.

    Returns:
        A hex colour string.
    """
    if snr >= 5:
        return "#4ade80"  # strong
    if snr >= 0:
        return "#facc15"  # marginal
    if snr >= -8:
        return "#fb923c"  # weak
    return "#f87171"  # very weak


def _slug(value: str) -> str:
    """Make a filesystem-safe slug from a label.

    Args:
        value: Arbitrary node name or scope label.

    Returns:
        A lowercase slug containing only alphanumerics, dashes, and underscores.
    """
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in value).strip("-").lower()


#: vis.js physics/layout tuning for a readable, settled mesh graph.
_OPTIONS = """
{
  "physics": {
    "solver": "forceAtlas2Based",
    "forceAtlas2Based": {"gravitationalConstant": -60, "springLength": 120},
    "stabilization": {"iterations": 200}
  },
  "edges": {
    "arrows": {"to": {"enabled": true, "scaleFactor": 0.6}},
    "smooth": {"type": "curvedCW", "roundness": 0.15}
  },
  "nodes": {"font": {"color": "#e2e8f0", "size": 16}}
}
"""
