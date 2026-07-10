"""Reusable Rich widgets: banner, status pill, trace tables, and progress bars."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional

from rich import box
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
from ..core.channels import is_name_derived, is_public_channel, is_public_name
from ..core.models import (
    LOCAL_DEVICE_LABEL,
    NODE_TYPE_CHAT,
    NODE_TYPE_LABELS,
    NODE_TYPE_REPEATER,
    NODE_TYPE_ROOM,
    NODE_TYPE_SENSOR,
    Contact,
    HopAggregate,
    TraceResult,
    TraceStats,
    TxOptResult,
    utcnow,
)
from .map_render import _NODE, _REPEATER, _SELF
from .theme import snr_style

if TYPE_CHECKING:
    from ..core.discovery import DiscoveredDevice

#: Maps a hop's raw key-prefix hash to a display label (a contact name when known).
NodeResolver = Callable[[Optional[str]], Optional[str]]


def channel_glyph(name: str, secret: Optional[bytes]) -> str:
    """The one-character openness marker for a channel, shared across channel-facing screens.

    ``＃`` marks a name-derived (``#``-style) channel, ``🌐`` a fixed-key well-known public
    channel (e.g. the firmware default ``Public``), and ``🔒`` a private one. Every glyph is a
    single double-width cell, so callers can prefix rows with ``"{glyph} "`` without disturbing
    column alignment. When the secret is unknown, the name alone is used to guess.

    Args:
        name: The channel name.
        secret: The channel's 16-byte secret, or ``None`` when only the name is known.

    Returns:
        A single-character glyph.
    """
    if secret is None:
        return "＃" if is_public_name(name) else "🔒"
    if is_name_derived(name, secret):
        return "＃"
    if is_public_channel(name, secret):
        return "🌐"
    return "🔒"

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
        f"[brand]MeshTerm[/brand] [muted]v{__version__}[/muted]\n"
        f"[muted]device:[/muted] {target}"
    )
    return Panel(body, border_style="accent", expand=False, title="[accent]Mesh[/accent]")


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


def _link_text(
    origin: Optional[str],
    destination: Optional[str],
    device_label: str,
    resolve: NodeResolver = _identity,
    hash_bytes: Optional[int] = None,
    device_hash: Optional[str] = None,
) -> Text:
    """Render an ``origin -> destination`` link as ``name (hash)`` nodes, arrow styled.

    Each end is rendered like the route line: a known node as ``name (hash)``, an
    unknown one as its bare hash, and our own device as its label plus ``device_hash``
    when supplied. Hashes are truncated to ``hash_bytes`` so they match how the trace
    command addressed each node.

    Args:
        origin: The transmitting node (``None`` = our device).
        destination: The receiving node (``None`` = our device).
        device_label: The label used for our own device.
        resolve: Maps a raw hop hash to a friendly name when the node is known.
        hash_bytes: Path-hash width (bytes) to truncate shown hashes to.
        device_hash: Our own device's key/hash, shown alongside its label when known.

    Returns:
        A :class:`Text` like ``us (a1) → Alice (3d)`` with the arrow muted.
    """
    text = _route_node_text(origin, device_label, resolve, hash_bytes, device_hash)
    text.append(" → ", style="muted")
    text.append_text(
        _route_node_text(destination, device_label, resolve, hash_bytes, device_hash)
    )
    return text


def traces_table(
    traces: list[TraceResult],
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = _identity,
    device_hash: Optional[str] = None,
) -> Table:
    """Render every trace's per-hop SNR side by side, one column per trace.

    Hops are framed as ``origin -> destination`` links (the first originates at our
    device, the last returns to it) and aligned by position across traces, so each
    column is one trace's SNR readings down the shared path. Each node is annotated with
    its hash at the command's path-hash width. A trailing ``min`` row shows each trace's
    bottleneck — the per-trace values the run's *median min SNR* summarizes. Traces that
    never replied appear as a ``✗`` column.

    Args:
        traces: The individual traces to display, in run order.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        device_hash: Our own device's key/hash, annotated onto the path's endpoints.

    Returns:
        A Rich :class:`Table` with a column per trace.
    """
    target = traces[0].target if traces else ""
    # All traces in a run share the command's path-hash width; take it from the first
    # successful one so node hashes render at the width they were addressed.
    hash_bytes = next((t.path_hash_bytes for t in traces if t.success), None)
    table = Table(title=f"Trace → {target}", border_style="muted", expand=False)
    table.add_column("HOP", justify="right", style="muted")
    table.add_column("FROM → TO")
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
                _link_text(
                    edges[idx].origin, edges[idx].destination, device_label, resolve,
                    hash_bytes, device_hash,
                )
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


def highlighted_hash(value: str, prefix_bytes: int) -> Text:
    """Render a full hex key with its leading path-hash prefix highlighted.

    The first ``prefix_bytes`` bytes are the slice other nodes address in a forced trace
    path (the path-hash); they are shown in the brand colour and the remainder muted, so
    the addressable prefix stands out within the otherwise full key.

    Args:
        value: The full key as hex, optionally ``0x``-prefixed and mixed-case.
        prefix_bytes: Number of leading bytes the current path-hash mode addresses; ``0``
            (or negative) leaves the whole key un-highlighted.

    Returns:
        A styled :class:`Text` of the full key.
    """
    raw = value.lower().removeprefix("0x")
    split = max(0, prefix_bytes) * 2
    text = Text()
    if split:
        text.append(raw[:split], style="brand")
        text.append(raw[split:], style="muted")
    else:
        text.append(raw, style="muted")
    return text


def _shorten_hash(value: str, hash_bytes: Optional[int]) -> str:
    """Return ``value`` as bare hex truncated to ``hash_bytes`` bytes.

    Args:
        value: A hex hash, optionally ``0x``-prefixed and mixed-case.
        hash_bytes: Width in bytes to truncate to; the full value when falsy/unknown.

    Returns:
        Lowercase hex with no ``0x`` prefix, at most ``hash_bytes`` bytes wide.
    """
    raw = value.lower().removeprefix("0x")
    return raw[: hash_bytes * 2] if hash_bytes else raw


def _route_node_text(
    label: Optional[str],
    device_label: str,
    resolve: NodeResolver,
    hash_bytes: Optional[int],
    device_hash: Optional[str] = None,
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
        device_hash: Our own device's key/hash, shown alongside its label when known.

    Returns:
        A styled :class:`Text` for the node. A non-breaking space joins name and hash
        so they never split across a line wrap.
    """
    if not label or label == device_label:
        text = Text(device_label, style="accent")
        shown = _shorten_hash(device_hash, hash_bytes) if device_hash else ""
        if shown:
            text.append(f"{_NBSP}(", style="muted")  # nbsp keeps "name (hash)" together
            text.append(shown, style="muted")
            text.append(")", style="muted")
        return text
    shown = _shorten_hash(label, hash_bytes)
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
    device_hash: Optional[str] = None,
) -> Text:
    """Render a trace's route as a sequence of ``name (hash)`` nodes.

    Shows the path the trace actually walked — the forced path, or the route the
    device resolved when auto-routing — with each node annotated by its hash at the
    command's path-hash width, joined by muted arrows, e.g.
    ``Me (a1b2) → Alice (3d63) → Bob (f2a1) → Me (a1b2)``. The line wraps after an arrow
    when it is too long for the panel, so each continuation line starts on a node.

    Args:
        result: The trace whose route to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        device_hash: Our own device's key/hash, annotated onto its endpoints when known.

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
        text.append_text(
            _route_node_text(node, device_label, resolve, hash_bytes, device_hash)
        )
    return text


def _hop_medians_table(
    hop_snrs: list[HopAggregate],
    device_label: str,
    resolve: NodeResolver = _identity,
    hash_bytes: Optional[int] = None,
    device_hash: Optional[str] = None,
) -> Table:
    """Render the per-hop median SNR aggregated across a run's traces.

    Args:
        hop_snrs: The per-hop aggregates to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        hash_bytes: Path-hash width (bytes) to truncate shown node hashes to.
        device_hash: Our own device's key/hash, annotated onto the path's endpoints.

    Returns:
        A compact Rich :class:`Table` of hop, link, and median SNR.
    """
    table = Table(box=None, padding=(0, 1, 0, 0), expand=False)
    table.add_column("HOP", justify="right", style="muted")
    table.add_column("FROM → TO")
    table.add_column("MEDIAN SNR", justify="right")
    for agg in hop_snrs:
        table.add_row(
            str(agg.index),
            _link_text(
                agg.origin, agg.destination, device_label, resolve, hash_bytes, device_hash
            ),
            Text(f"{agg.median_snr:+.1f} dB", style=snr_style(agg.median_snr)),
        )
    return table


def stats_panel(
    stats: TraceStats,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = _identity,
    route: Optional[TraceResult] = None,
    device_hash: Optional[str] = None,
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
        device_hash: Our own device's key/hash, annotated onto the route endpoints.

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
        sections.append(Text("Route", style="accent"))
        sections.append(_route_text(route, device_label, resolve, device_hash))
        sections.append(Text())  # blank line before the stats block
    sections.append(summary)
    if stats.hop_snrs:
        sections.append(Text("\nPer-hop medians", style="accent"))
        hash_bytes = route.path_hash_bytes if route is not None else None
        sections.append(
            _hop_medians_table(stats.hop_snrs, device_label, resolve, hash_bytes, device_hash)
        )
    body: Text | Group = sections[0] if len(sections) == 1 else Group(*sections)
    return Panel(body, title="[accent]Trace summary[/accent]", border_style="accent", expand=False)


# Node-type glyphs and their colours, consistent with the map's marker palette across the
# whole app (see ui.map_render): our own node is the yellow ``★``, plain nodes the loud pink
# ``●`` and repeaters the calmer violet ``▲``. The remaining types take map-safe hues that
# stay distinct from those — a white square for rooms (the house glyph read poorly) and an
# orange ringed dot for sensors. All are single-width BMP glyphs so columns stay aligned.
_ROOM_COLOR = "#ffffff"
_SENSOR_COLOR = "#fb923c"
_NODE_GLYPHS: dict[int, tuple[str, str]] = {
    NODE_TYPE_REPEATER: (_REPEATER[0], _REPEATER[1]),
    NODE_TYPE_ROOM: ("■", _ROOM_COLOR),
    NODE_TYPE_SENSOR: ("◉", _SENSOR_COLOR),
    NODE_TYPE_CHAT: (_NODE[0], _NODE[1]),
}
_DEFAULT_GLYPH: tuple[str, str] = (_NODE[0], _NODE[1])

# A heat-map gradient for a contact's name, hottest (most recently heard) to coldest: white
# → yellow → orange → red → grey. Each stop pairs an age anchor (log10 of seconds since heard)
# with an RGB colour; :func:`_recency_style` interpolates continuously between them, so the
# colour glides with recency rather than snapping between a handful of discrete shades.
_HEAT_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (math.log10(300), (255, 255, 255)),           # ≤5m — white (fresh)
    (math.log10(3600), (250, 204, 21)),           # ~1h  — yellow
    (math.log10(21600), (251, 146, 60)),          # ~6h  — orange
    (math.log10(86400), (248, 113, 113)),         # ~1d  — red
    (math.log10(604800), (148, 163, 184)),        # ~1w  — grey
    (math.log10(2592000), (100, 116, 139)),       # ~30d+ — cold slate
)
_RECENCY_NEVER = "#64748b"       # never heard — the coldest slate


def _age_seconds(when: Optional[datetime]) -> Optional[float]:
    """Seconds since ``when`` (aware UTC), or ``None`` when unknown/naive."""
    if when is None or getattr(when, "tzinfo", None) is None:
        return None
    return max(0.0, (utcnow() - when).total_seconds())


def _format_age(secs: Optional[float]) -> str:
    """A compact relative age — ``now``, ``5m``, ``3h``, ``2d``, ``4w`` — or ``never``."""
    if secs is None:
        return "never"
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    if secs < 604800:
        return f"{int(secs // 86400)}d"
    return f"{int(secs // 604800)}w"


def _recency_style(secs: Optional[float]) -> str:
    """The heat-map name colour for a contact last heard ``secs`` ago (hotter = more recent).

    Interpolates the RGB channels between the two :data:`_HEAT_STOPS` bracketing ``secs`` (in
    log-age space), clamping to white below the first stop and cold slate above the last.
    """
    if secs is None:
        return _RECENCY_NEVER
    x = math.log10(max(secs, 0.0) + 1.0)
    if x <= _HEAT_STOPS[0][0]:
        r, g, b = _HEAT_STOPS[0][1]
    elif x >= _HEAT_STOPS[-1][0]:
        r, g, b = _HEAT_STOPS[-1][1]
    else:
        (x0, c0), (x1, c1) = next(
            (lo, hi) for lo, hi in zip(_HEAT_STOPS, _HEAT_STOPS[1:]) if lo[0] <= x <= hi[0]
        )
        f = (x - x0) / (x1 - x0)
        r, g, b = (round(a + (bb - a) * f) for a, bb in zip(c0, c1))
    return f"#{r:02x}{g:02x}{b:02x}"


def _key_id(value: str) -> str:
    """Normalise a key/prefix to the lowercased 12-hex id used to match heard nodes."""
    return value.lower().removeprefix("0x")[:12]


def _contact_pkts(contact: Contact, counts: dict[str, int]) -> Optional[int]:
    """The overheard-packet tally for ``contact``, or ``None`` if never overheard."""
    ident = contact.public_key or contact.key_prefix
    return counts.get(_key_id(ident)) if ident else None


# The columns the node list can be sorted by, left-to-right, and the direction each opens on
# — name A→Z, most-recently-heard first, most packets first — chosen so a fresh sort shows
# the "interesting" end at the top.
_SORT_COLUMNS: tuple[str, ...] = ("name", "heard", "packets")
_SORT_OPENS_ASCENDING: dict[str, bool] = {"name": True, "heard": True, "packets": False}


@dataclass
class NodesSort:
    """Which column the node list is sorted by, and in which direction.

    ``column`` is one of :data:`_SORT_COLUMNS`; ``ascending`` sorts the column's underlying
    metric low-to-high — name A→Z, *age* (so ascending = most recently heard first), packet
    count low-to-high. The interactive screen mutates this in place as the user presses the
    arrows.
    """

    column: str = "name"
    ascending: bool = True

    @classmethod
    def from_name(cls, name: str) -> "NodesSort":
        """Build a sort for ``name``, opening in that column's natural direction."""
        column = name if name in _SORT_COLUMNS else "name"
        return cls(column, _SORT_OPENS_ASCENDING[column])

    def move(self, delta: int) -> None:
        """Step the active column ``delta`` places (wrapping), adopting its natural direction."""
        index = (_SORT_COLUMNS.index(self.column) + delta) % len(_SORT_COLUMNS)
        self.column = _SORT_COLUMNS[index]
        self.ascending = _SORT_OPENS_ASCENDING[self.column]


def _ordered_contacts(
    contacts: list[Contact], counts: dict[str, int], sort: NodesSort
) -> list[Contact]:
    """Contacts sorted per ``sort`` (our own node is pinned separately, above these).

    The active column's metric drives the order (reversed for a descending sort); ties always
    break by case-folded name *ascending*, so two nodes sharing a metric (e.g. the same
    ``heard`` age) keep a stable A→Z order instead of flipping with the primary direction.
    Never-heard / never-overheard rows carry an extreme metric so they gather at the ascending
    end.
    """
    if sort.column == "heard":
        # One clock snapshot for the whole sort: per-row utcnow() calls would skew two
        # identical last_seen stamps apart by microseconds and defeat the name tie-break.
        now = utcnow()

        def metric(c: Contact) -> float:
            if c.last_seen is None or getattr(c.last_seen, "tzinfo", None) is None:
                return float("inf")
            return max(0.0, (now - c.last_seen).total_seconds())
    elif sort.column == "packets":
        def metric(c: Contact) -> float:
            return _contact_pkts(c, counts) or 0
    else:
        def metric(c: Contact) -> object:
            return c.name.casefold()

    # Name-ascending first, then a stable sort by the primary metric: equal-metric rows retain
    # their A→Z order under both directions (reversing the whole list would flip the tiebreak).
    ordered = sorted(contacts, key=lambda c: c.name.casefold())
    ordered.sort(key=metric, reverse=not sort.ascending)
    return ordered


def _sort_header(label: str, column: str, sort: NodesSort) -> str:
    """A column header: plain-muted, or cyan with a direction triangle when it's the sort key.

    The active column's name and its triangle are lit cyan together (so the interactive
    left/right selection is obvious) — ``▲`` for ascending, ``▼`` for descending. The column
    reserves its width (see :func:`nodes_table`) so toggling the sort doesn't shift the row.
    """
    if column != sort.column:
        return label
    triangle = "▲" if sort.ascending else "▼"
    return f"[bold #22d3ee]{label} {triangle}[/]"


def _nodes_legend() -> Text:
    """A one-line glyph legend covering every node type (plus us), whatever's listed."""
    legend = Text("  ")  # a small indent to sit under the table body
    legend.append(_SELF[0], style=_SELF[1])
    legend.append(" you", style="muted")
    for node_type in (NODE_TYPE_REPEATER, NODE_TYPE_CHAT, NODE_TYPE_ROOM, NODE_TYPE_SENSOR):
        glyph, color = _NODE_GLYPHS[node_type]
        legend.append("   ")
        legend.append(glyph, style=color)
        legend.append(f" {NODE_TYPE_LABELS[node_type]}", style="muted")
    return legend


def nodes_table(
    self_name: str,
    self_key: str,
    contacts: list[Contact],
    prefix_bytes: int,
    counts: dict[str, int],
    sort: Optional[NodesSort] = None,
) -> Group:
    """List this node and its known contacts with recency, packets, type, key, and legend.

    Our own node is the first row (``★``, name in accent); contacts follow in ``sort`` order.
    A per-type glyph marks each node in the app's shared colours, the name is coloured by how
    recently it was last heard (brighter = fresher), and the full key is shown with its
    path-hash prefix lit — chopped with an ellipsis only when the terminal is too narrow.

    Args:
        self_name: This node's advertised name.
        self_key: This node's full public key (hex); blank renders as ``?``.
        contacts: Known contacts, listed after our own node.
        prefix_bytes: Path-hash width in bytes to highlight in every key.
        counts: Overheard-packet counts keyed by lowercased 12-hex node id (from
            monitoring); a contact with no entry shows ``—``.
        sort: The active sort (column + direction); defaults to name-ascending. The sorted
            column's header is lit cyan with an up/down direction triangle.

    Returns:
        A Rich :class:`Group` of the frameless table and its glyph legend.
    """
    sort = sort if sort is not None else NodesSort()

    # expand=True lets the key column (the only flexible one) soak up all spare width and be
    # the sole column Rich squeezes when narrow — the fixed columns keep their natural size.
    table = Table(
        title=f"[accent]Nodes[/accent]  [muted]· {len(contacts)} known[/muted]",
        title_justify="left",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="muted",
        expand=True,
        padding=(0, 2, 0, 0),
    )
    # Each sortable header reserves two extra columns for its " ▲" direction marker (via
    # min_width = label + 2) so the same width holds whether or not it's the active sort —
    # switching the sort never widens a column and shifts the rest of the row.
    table.add_column("", no_wrap=True)  # node-type glyph
    table.add_column(_sort_header("NAME", "name", sort), no_wrap=True, min_width=6)
    table.add_column(
        _sort_header("HEARD", "heard", sort), justify="right", no_wrap=True, min_width=7
    )
    table.add_column(
        _sort_header("PKTS", "packets", sort), justify="right", no_wrap=True, min_width=6
    )
    # The full key, chopped to an ellipsis by Rich only when the row won't otherwise fit.
    table.add_column("KEY", no_wrap=True, overflow="ellipsis", ratio=1, min_width=10)

    unknown = Text("?", style="muted")
    table.add_row(
        Text(_SELF[0], style=_SELF[1]),
        Text.assemble((self_name, "accent"), ("  (you)", "muted")),
        Text("—", style="faint"),
        Text("—", style="faint"),
        highlighted_hash(self_key, prefix_bytes) if self_key else unknown,
    )
    table.add_section()
    for c in _ordered_contacts(contacts, counts, sort):
        secs = _age_seconds(c.last_seen)
        glyph, glyph_style = _NODE_GLYPHS.get(c.node_type, _DEFAULT_GLYPH)
        pkts = _contact_pkts(c, counts)
        table.add_row(
            Text(glyph, style=glyph_style),
            Text(c.name, style=_recency_style(secs)),
            Text(_format_age(secs), style="muted"),
            Text(str(pkts), style="muted") if pkts else Text("—", style="faint"),
            highlighted_hash(c.public_key, prefix_bytes) if c.public_key else unknown,
        )
    return Group(table, Text(""), _nodes_legend())


def tx_opt_table(result: TxOptResult) -> Table:
    """Render every measured TX level of an optimization sweep, best row highlighted.

    Args:
        result: The optimization result to display.

    Returns:
        A Rich :class:`Table` of TX level, target SNR, success rate, and sample count.
    """
    table = Table(
        title=f"TX sweep · {result.admin_node} -> {result.target}",
        border_style="muted",
        expand=False,
    )
    table.add_column("TX", justify="right")
    table.add_column("TARGET SNR", justify="right")
    table.add_column("SUCCESS", justify="right")
    table.add_column("TRACES", justify="right")
    for lv in result.sorted_by_tx():
        is_best = lv.tx_power == result.best_tx
        marker = "[ok]★[/ok] " if is_best else "  "
        snr = lv.target_snr
        snr_cell = Text(f"{snr:+.1f}", style=snr_style(snr)) if snr is not None else Text("—")
        # Highlight anything short of a perfect success rate — reliability comes first.
        rate = lv.success_rate
        rate_style = "ok" if rate >= 1.0 else ("warn" if rate > 0 else "err")
        rate_cell = Text(f"{rate:.0%}", style=rate_style)
        tx_cell = f"{marker}{lv.tx_power}"
        row_style = "ok" if is_best else None
        table.add_row(
            tx_cell, snr_cell, rate_cell, f"{lv.successes}/{lv.samples}", style=row_style
        )
    return table


def tx_opt_summary(result: TxOptResult) -> Panel:
    """Summarize a TX optimization outcome.

    Args:
        result: The optimization result to summarize.

    Returns:
        A Rich :class:`Panel` stating the chosen optimum and whether it was applied.
    """
    snr = result.best_snr
    snr_text = Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("n/a")
    applied = (
        f"[ok]applied to {result.admin_node}[/ok]"
        if result.applied
        else "[muted]not applied[/muted]"
    )
    body = Text.assemble(
        ("tuning node   ", "muted"), (f"{result.admin_node}\n", "brand"),
        ("target        ", "muted"), (f"{result.target}\n", "brand"),
        ("optimal TX    ", "muted"), (f"{result.best_tx}", "brand"), ("\n", ""),
        ("target SNR    ", "muted"), snr_text, ("\n", ""),
        ("reliability   ", "muted"), (f"{result.best_success_rate:.0%}\n", ""),
        ("previous TX   ", "muted"),
        (f"{result.original_tx if result.original_tx is not None else '?'}\n", ""),
        ("status        ", "muted"), Text.from_markup(applied),
    )
    return Panel(body, title="[accent]TX optimization[/accent]", border_style="accent", expand=False)
