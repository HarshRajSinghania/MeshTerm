"""Reusable Rich widgets: banner, status pill, trace tables, and progress bars."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rich.console import Console
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
from ..core.models import TraceResult, TraceStats, TxOptResult
from .theme import snr_style

if TYPE_CHECKING:
    from ..core.discovery import DiscoveredDevice


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


def trace_table(result: TraceResult) -> Table:
    """Render a single trace's per-hop SNR as a table.

    Args:
        result: The trace to display.

    Returns:
        A Rich :class:`Table` of hops and SNR values.
    """
    table = Table(title=f"Trace -> {result.target}", border_style="muted", expand=False)
    table.add_column("Hop", justify="right", style="muted")
    table.add_column("Node")
    table.add_column("SNR (dB)", justify="right")
    if not result.success:
        table.add_row("-", "[err]no reply[/err]", "-")
        return table
    for hop in result.hops:
        table.add_row(
            str(hop.index),
            hop.node or "[muted]?[/muted]",
            Text(f"{hop.snr:+.1f}", style=snr_style(hop.snr)),
        )
    return table


def stats_panel(stats: TraceStats) -> Panel:
    """Summarize aggregated trace statistics in a panel.

    Args:
        stats: The aggregated statistics to display.

    Returns:
        A Rich :class:`Panel` with success rate and robust SNR/RTT.
    """
    snr = stats.median_min_snr
    snr_text = Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("n/a")
    rtt = f"{stats.median_rtt_ms:.0f} ms" if stats.median_rtt_ms is not None else "n/a"
    body = Text.assemble(
        ("target        ", "muted"), (f"{stats.target}\n", ""),
        ("success rate  ", "muted"), (f"{stats.success_rate:.0%} "
                                      f"({stats.successes}/{stats.samples})\n", ""),
        ("median min SNR ", "muted"), snr_text, ("\n", ""),
        ("median RTT    ", "muted"), (rtt, ""),
    )
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
