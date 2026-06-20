"""Plotly renderer for TX-power optimization sweeps.

Produces a self-contained, interactive HTML chart: reliability-weighted score and median
bottleneck SNR versus TX power, with the chosen optimum marked.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import plotly.graph_objects as go

from ..core.models import TxOptResult

_TEMPLATE = "plotly_dark"
_BRAND = "#5eead4"
_ACCENT = "#818cf8"
_MUTED = "#94a3b8"


def render_tx_optimization(result: TxOptResult, output_dir: Path) -> Path:
    """Render a TX-power optimization sweep to an interactive HTML file.

    Args:
        result: The optimization result to visualize.
        output_dir: Directory the HTML file is written to (created if needed).

    Returns:
        Path to the written HTML file.
    """
    levels = result.sorted_by_tx()
    tx = [lv.tx_power for lv in levels]
    snr = [lv.stats.median_min_snr for lv in levels]
    score = [lv.score if math.isfinite(lv.score) else None for lv in levels]
    success = [lv.stats.success_rate * 100 for lv in levels]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=tx, y=snr, name="median min SNR (dB)", mode="lines+markers",
            line=dict(color=_BRAND, width=3), marker=dict(size=8),
            hovertemplate="TX %{x}<br>SNR %{y:+.1f} dB<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=tx, y=score, name="score", mode="lines+markers",
            line=dict(color=_ACCENT, width=2, dash="dot"), marker=dict(size=6),
            hovertemplate="TX %{x}<br>score %{y:.2f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            x=tx, y=success, name="success rate (%)", yaxis="y2",
            marker=dict(color=_MUTED), opacity=0.25,
            hovertemplate="TX %{x}<br>%{y:.0f}% success<extra></extra>",
        )
    )

    best = result.best_level
    if best is not None and best.stats.median_min_snr is not None:
        fig.add_vline(x=result.best_tx, line=dict(color="#4ade80", width=2, dash="dash"))
        fig.add_trace(
            go.Scatter(
                x=[result.best_tx], y=[best.stats.median_min_snr],
                name=f"optimum (TX {result.best_tx})", mode="markers",
                marker=dict(color="#4ade80", size=16, symbol="star"),
                hovertemplate=f"optimum<br>TX {result.best_tx}"
                "<br>%{y:+.1f} dB<extra></extra>",
            )
        )

    fig.update_layout(
        template=_TEMPLATE,
        title=dict(
            text=f"TX-power optimization — {result.target}"
            f"  ·  optimum TX {result.best_tx}",
            font=dict(color=_BRAND),
        ),
        xaxis=dict(title="TX power", dtick=1),
        yaxis=dict(title="SNR (dB) / score"),
        yaxis2=dict(
            title="success rate (%)", overlaying="y", side="right",
            range=[0, 105], showgrid=False,
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
        bargap=0.4,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_dir / f"tx-optimize_{_slug(result.target)}_{stamp}.html"
    fig.write_html(path, include_plotlyjs=True)  # fully self-contained / offline
    return path


def _slug(value: str) -> str:
    """Make a filesystem-safe slug from a node name.

    Args:
        value: Arbitrary node name or key prefix.

    Returns:
        A lowercase slug containing only alphanumerics, dashes, and underscores.
    """
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in value).strip("-").lower()
