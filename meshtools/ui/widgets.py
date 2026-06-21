"""Reusable Rich widgets: banner, status pill, trace tables, and progress bars."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from .. import __version__
from ..core.models import (
    LOCAL_DEVICE_LABEL,
    HopAggregate,
    HopEdge,
    TraceResult,
    TraceStats,
    TxOptResult,
)
from .theme import snr_style

if TYPE_CHECKING:
    from ..core.discovery import DiscoveredDevice

#: Maps a hop's raw key-prefix hash to a display label (a contact name when known).
NodeResolver = Callable[[Optional[str]], Optional[str]]

#: Non-breaking space, used in the route line to keep ``name (hash)`` and a node's
#: trailing arrow on the same line so wraps only ever land *after* an arrow.
_NBSP = " "


def _identity(label: Optional[str]) -> Optional[str]:
    """Default node resolver: leave labels untouched."""
    return label


def banner(
    profile: Optional[str], mock: bool, selected_device: Optional["DiscoveredDevice"] = None
) -> Panel:
    """Build the application header panel.

    Args:
        profile: Active device profile name, if any.
        mock: Whether the simulator is in use.
        selected_device: The discovered device chosen for this session, if any, shown
            when no named profile is in use.

    Returns:
        A Rich :class:`Panel` showing the app name, version, and connection target.
    """
    if mock:
        target = "[warn]simulator[/warn]"
    elif profile:
        target = profile
    elif selected_device is not None:
        target = selected_device.label
    else:
        target = "[muted]no device[/muted]"
    body = Text.from_markup(
        f"[brand]MeshTools[/brand] [muted]v{__version__}[/muted]\n"
        f"[muted]device:[/muted] {target}"
    )
    return Panel(body, border_style="accent", expand=False, title="[accent]mesh[/accent]")


def make_progress(console: Console) -> Progress:
    """Create a themed progress bar for trace sweeps.

    Args:
        console: The console to render into.

    Returns:
        A configured :class:`rich.progress.Progress` (use as a context manager).
    """
    return Progress(
        SpinnerColumn(style="accent"),
        TextColumn("[accent]{task.description}"),
        BarColumn(complete_style="brand", finished_style="ok"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def _node_text(
    label: Optional[str], device_label: str, resolve: NodeResolver = _identity
) -> Text:
    """Style a node label, making our own device stand out from repeaters.

    Args:
        label: The node identifier to render; ``None`` means our own device.
        device_label: The label used for our own device.
        resolve: Maps a raw hop hash to a friendly name when the node is known.

    Returns:
        A styled :class:`Text` for the node.
    """
    named = resolve(label) if label else label
    resolved = named if named else device_label
    style = "accent" if resolved == device_label else "brand"
    return Text(resolved, style=style)


def _link_text(
    origin: Optional[str],
    destination: Optional[str],
    device_label: str,
    resolve: NodeResolver = _identity,
) -> Text:
    """Render an ``origin -> destination`` link with a styled arrow.

    Args:
        origin: The transmitting node (``None`` = our device).
        destination: The receiving node (``None`` = our device).
        device_label: The label used for our own device.
        resolve: Maps a raw hop hash to a friendly name when the node is known.

    Returns:
        A :class:`Text` like ``us → Alice`` with the arrow muted.
    """
    text = _node_text(origin, device_label, resolve)
    text.append(" → ", style="muted")
    text.append_text(_node_text(destination, device_label, resolve))
    return text


def traces_table(
    traces: list[TraceResult],
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = _identity,
) -> Table:
    """Render every trace's per-hop SNR side by side, one column per trace.

    Hops are framed as ``origin -> destination`` links (the first originates at our
    device, the last returns to it) and aligned by position across traces, so each
    column is one trace's SNR readings down the shared path. A trailing ``min`` row
    shows each trace's bottleneck — the per-trace values the run's *median min SNR*
    summarizes. Traces that never replied appear as a ``✗`` column.

    Args:
        traces: The individual traces to display, in run order.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.

    Returns:
        A Rich :class:`Table` with a column per trace.
    """
    target = traces[0].target if traces else ""
    table = Table(title=f"Trace → {target}", border_style="muted", expand=False)
    table.add_column("Hop", justify="right", style="muted")
    table.add_column("From → To")
    for i in range(1, len(traces) + 1):
        table.add_column(f"#{i}", justify="right")

    # Index each trace's edges by hop position so columns line up even when traces
    # take different-length paths (a missing hop shows as a muted dash).
    edges_by_trace = [
        {e.index: e for e in t.edges(device_label)} if t.success else {} for t in traces
    ]
    indices = sorted({idx for edges in edges_by_trace for idx in edges})
    for idx in indices:
        link = next(
            (
                _link_text(edges[idx].origin, edges[idx].destination, device_label, resolve)
                for edges in edges_by_trace
                if idx in edges
            ),
            Text("—", style="muted"),
        )
        row: list[Text] = [Text(str(idx)), link]
        for edges in edges_by_trace:
            edge = edges.get(idx)
            if edge is None:
                row.append(Text("—", style="muted"))
            else:
                row.append(Text(f"{edge.snr:+.1f}", style=snr_style(edge.snr)))
        table.add_row(*row)

    # Bottleneck per trace: the weakest link the run's median min SNR is taken over.
    min_row: list[Text] = [Text("min", style="muted"), Text("bottleneck", style="muted")]
    for trace in traces:
        if not trace.success:
            min_row.append(Text("✗", style="err"))
        elif trace.min_snr is not None:
            min_row.append(Text(f"{trace.min_snr:+.1f}", style=snr_style(trace.min_snr)))
        else:
            min_row.append(Text("—", style="muted"))
    table.add_section()
    table.add_row(*min_row)
    return table


def _route_node_text(
    label: Optional[str],
    device_label: str,
    resolve: NodeResolver,
    hash_bytes: Optional[int],
) -> Text:
    """Render one route node as ``name (hash)``, the hash at the command's width.

    Our own device (``label`` is ``None`` or equal to ``device_label``) carries no
    hash. A known node shows its friendly name followed by its key-prefix hash; an
    unknown node shows just the hash. The hash is truncated to ``hash_bytes`` — the
    per-hop width the trace command used — so it matches how the node was addressed.

    Args:
        label: The node's raw hop hash, or ``None``/``device_label`` for our device.
        device_label: The label used for our own device.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        hash_bytes: Path-hash width (bytes) to truncate the shown hash to.

    Returns:
        A styled :class:`Text` for the node. A non-breaking space joins name and hash
        so they never split across a line wrap.
    """
    if not label or label == device_label:
        return Text(device_label, style="accent")
    raw = label.lower().removeprefix("0x")
    shown = raw[: hash_bytes * 2] if hash_bytes else raw
    named = resolve(label)
    if not named or named == label:
        return Text(shown, style="brand")  # unknown node: hash only
    text = Text(named, style="brand")
    text.append(f"{_NBSP}(", style="muted")  # nbsp keeps "name (hash)" together
    text.append(shown, style="muted")
    text.append(")", style="muted")
    return text


def _route_text(
    result: TraceResult,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = _identity,
) -> Text:
    """Render a trace's route as a sequence of ``name (hash)`` nodes.

    Shows the path the trace actually walked — the forced path, or the route the
    device resolved when auto-routing — with each node annotated by its hash at the
    command's path-hash width, joined by muted arrows, e.g.
    ``Me → Alice (3d63) → Bob (f2a1) → Me``. The line wraps after an arrow when it is
    too long for the panel, so each continuation line starts on a node.

    Args:
        result: The trace whose route to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.

    Returns:
        A :class:`Text` with the node sequence, or a muted note when no hops exist.
    """
    edges = result.edges(device_label)
    if not edges:
        return Text("no hops recorded", style="muted")
    hash_bytes = result.path_hash_bytes
    nodes = [edges[0].origin] + [edge.destination for edge in edges]
    text = Text()
    for i, node in enumerate(nodes):
        if i:
            # A non-breaking space glues the arrow to the preceding node; the trailing
            # regular space is the only wrap point, so wrapping lands *after* the arrow.
            text.append(f"{_NBSP}→ ", style="muted")
        text.append_text(_route_node_text(node, device_label, resolve, hash_bytes))
    return text


def _hop_medians_table(
    hop_snrs: list[HopAggregate], device_label: str, resolve: NodeResolver = _identity
) -> Table:
    """Render the per-hop median SNR aggregated across a run's traces.

    Args:
        hop_snrs: The per-hop aggregates to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.

    Returns:
        A compact Rich :class:`Table` of hop, link, and median SNR.
    """
    table = Table(box=None, padding=(0, 1, 0, 0), expand=False)
    table.add_column("hop", justify="right", style="muted")
    table.add_column("from → to")
    table.add_column("median SNR", justify="right")
    for agg in hop_snrs:
        table.add_row(
            str(agg.index),
            _link_text(agg.origin, agg.destination, device_label, resolve),
            Text(f"{agg.median_snr:+.1f} dB", style=snr_style(agg.median_snr)),
        )
    return table


def stats_panel(
    stats: TraceStats,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = _identity,
    route: Optional[TraceResult] = None,
) -> Panel:
    """Summarize aggregated trace statistics in a panel.

    Includes the median SNR for every hop along the path (not just the bottleneck), so
    a weak link anywhere in the route is visible. When a representative ``route`` trace
    is given, the path it actually walked is shown as a node sequence, e.g.
    ``Me → Alice → Bob → Me``.

    Args:
        stats: The aggregated statistics to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        route: A representative trace whose walked route to display, if any.

    Returns:
        A Rich :class:`Panel` with the route, success rate, robust SNR/RTT, and
        per-hop medians.
    """
    snr = stats.median_min_snr
    snr_text = Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("n/a")
    rtt = f"{stats.median_rtt_ms:.0f} ms" if stats.median_rtt_ms is not None else "n/a"
    summary = Text.assemble(
        ("target        ", "muted"), (f"{stats.target}\n", ""),
        ("success rate  ", "muted"), (f"{stats.success_rate:.0%} "
                                      f"({stats.successes}/{stats.samples})\n", ""),
        ("median min SNR ", "muted"), snr_text, ("\n", ""),
        ("median RTT    ", "muted"), (rtt, ""),
    )
    sections: list[Text | Table] = []
    if route is not None:
        sections.append(Text("route", style="muted"))
        sections.append(_route_text(route, device_label, resolve))
        sections.append(Text())  # blank line before the stats block
    sections.append(summary)
    if stats.hop_snrs:
        sections.append(Text("\nper-hop medians", style="muted"))
        sections.append(_hop_medians_table(stats.hop_snrs, device_label, resolve))
    body: Text | Group = sections[0] if len(sections) == 1 else Group(*sections)
    return Panel(body, title="[accent]trace summary[/accent]", border_style="accent", expand=False)


def tx_opt_table(result: TxOptResult) -> Table:
    """Render every measured TX level of an optimization sweep, best row highlighted.

    Args:
        result: The optimization result to display.

    Returns:
        A Rich :class:`Table` of TX level, SNR, success rate, and score.
    """
    table = Table(title=f"TX sweep -> {result.target}", border_style="muted", expand=False)
    table.add_column("TX", justify="right")
    table.add_column("median min SNR", justify="right")
    table.add_column("success", justify="right")
    table.add_column("score", justify="right")
    for lv in result.sorted_by_tx():
        is_best = lv.tx_power == result.best_tx
        marker = "[ok]★[/ok] " if is_best else "  "
        snr = lv.stats.median_min_snr
        snr_cell = Text(f"{snr:+.1f}", style=snr_style(snr)) if snr is not None else Text("—")
        score_txt = f"{lv.score:.2f}" if lv.score != float("-inf") else "—"
        tx_cell = f"{marker}{lv.tx_power}"
        row_style = "ok" if is_best else None
        table.add_row(
            tx_cell, snr_cell, f"{lv.stats.success_rate:.0%}", score_txt, style=row_style
        )
    return table


def tx_opt_summary(result: TxOptResult) -> Panel:
    """Summarize a TX optimization outcome.

    Args:
        result: The optimization result to summarize.

    Returns:
        A Rich :class:`Panel` stating the chosen optimum and whether it was applied.
    """
    best = result.best_level
    snr = best.stats.median_min_snr if best else None
    snr_text = Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("n/a")
    applied = "[ok]applied to device[/ok]" if result.applied else "[muted]not applied[/muted]"
    body = Text.assemble(
        ("optimal TX    ", "muted"), (f"{result.best_tx}", "brand"), ("\n", ""),
        ("at SNR        ", "muted"), snr_text, ("\n", ""),
        ("previous TX   ", "muted"),
        (f"{result.original_tx if result.original_tx is not None else '?'}\n", ""),
        ("status        ", "muted"), Text.from_markup(applied),
    )
    return Panel(body, title="[accent]tx optimization[/accent]", border_style="accent", expand=False)
