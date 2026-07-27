"""Reusable Rich widgets: banner, status pill, trace tables, and progress bars."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Optional, Sequence

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
from .map_render import _NODE, _REPEATER, _SELF, _UNKNOWN
from .mapcanvas import RGB, parse_hex
from .pathgraph import DST_NODE, GlyphOf, LabelOf, LabelRgbOf, SRC_NODE
from .pathline import PathLine, path_line
from .theme import name_style, node_style, snr_style

if TYPE_CHECKING:
    from ..core.discovery import DiscoveredDevice

#: Maps a hop's raw key-prefix hash to a display label (a contact name when known).
NodeResolver = Callable[[Optional[str]], Optional[str]]

#: Maps a display name back to its node's key hex, or ``None`` for a name no known
#: node carries (built by :func:`~meshterm.services.trace_runner.make_name_key_resolver`).
NameKeyResolver = Callable[[str], Optional[str]]


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
    """Render an ``origin -> destination`` link through THE path widget.

    A two-node :func:`path_text` in the trace presentation: a known node as
    ``name (hash)``, an unknown one as its bare hash, our own device as its label
    (plus ``device_hash`` when supplied). Hashes are truncated to ``hash_bytes`` so
    they match how the trace command addressed each node.

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
    ends = [
        None if not node or node == device_label else node
        for node in (origin, destination)
    ]
    return path_text(
        ends, resolve, prefix_bytes=hash_bytes or 8, self_name=device_label,
        show_hash=True, hash_bytes=hash_bytes, device_hash=device_hash,
    )


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


def path_text(
    hops: Sequence[Optional[str]],
    resolve: NodeResolver = _identity,
    *,
    prefix_bytes: int = 0,
    self_name: Optional[str] = None,
    empty: str = "direct",
    show_hash: bool = False,
    hash_bytes: Optional[int] = None,
    device_hash: Optional[str] = None,
    dim_from: Optional[int] = None,
    hash_as_name: bool = False,
    cursor_arrow: Optional[int] = None,
) -> Text:
    """Render a hop sequence compactly on one line — THE path widget.

    The one way MeshTerm shows a walked, relayed, or planned hop sequence, wherever one
    appears: a packet's ``via`` row, a message's delivery paths, a feed note, a trace's
    walked route or planned spec. Each hop renders as its resolved name — coloured in
    the app-wide per-name hue, our own node pure white — or, unnamed, as its hash
    through :func:`highlighted_hash` (the addressed prefix lit, the rest muted; colour
    stays the "this is a name" signal). Hops are joined by muted ``→`` arrows and, by
    default, no hash is repeated after a name, so the compact form survives a 72-column
    row. The trace-flavoured options: ``show_hash`` annotates each named hop with the
    hash it is addressed by (``Alice (3d63)``), a ``None`` hop is our own device at a
    route's endpoints, and ``dim_from`` fades the tail a caller wants read as automatic
    (a boomerang's mirrored return leg). ``hash_as_name`` reframes an *unnamed* hop as
    its own identity — the hash at the path-hash-mode (``prefix_bytes``) width, muted
    grey with no prefix lit (colour is the "this is a name" signal, and there is no
    name), annotated with its addressed byte like a named hop (``e839f2 (e8)``) — where
    the default instead lights the compact addressed hash. An empty path reads as
    ``empty``.

    Args:
        hops: The hops in propagation order — hex hashes, with ``None`` marking our
            own device (empty strings are skipped; ``dim_from`` counts rendered hops).
        resolve: Maps a hop hash to a friendly name when known.
        prefix_bytes: Path-hash width to light in unnamed hops' hashes (0 = none).
        self_name: Our own node's name — drawn in the white ``you`` style when a
            resolved name matches it, and naming any ``None`` device hop.
        empty: The muted text shown when there are no hops (e.g. ``"direct"``).
        show_hash: Annotate named hops (and, with ``device_hash``, our device) with
            their hash in parentheses — the trace presentation.
        hash_bytes: Truncate shown/annotated hashes to this byte width (the width the
            hops were addressed at); ``None`` shows them whole.
        device_hash: Our own device's key, annotated onto ``None`` hops when
            ``show_hash`` is on.
        dim_from: Render hops at/after this index — and the arrows into them — faint
            (a planned route's return leg); ``None`` dims nothing.
        hash_as_name: Present each unnamed hop as its identity hash — muted grey at the
            ``prefix_bytes`` width, annotated with its addressed byte (``hash_bytes``)
            like a named hop — rather than the compact prefix-lit addressed hash.
        cursor_arrow: Draw this joining arrow (0-based: arrow *j* joins rendered hops
            *j* and *j+1*) as an insertion cursor — the ``→`` glyph in the
            reverse-video ``selected`` block, winning over ``dim_from`` — where the
            path composer edits its route. ``None`` (everywhere else) draws every
            arrow plain.

    Returns:
        A one-line :class:`Text`. Space tighter than the path is the caller's call,
        per surface: ellipsize (``no_wrap``), wrap under a hanging indent, or crop
        with horizontal scrolling.
    """
    shown = [h for h in hops if h is None or h]
    if not shown:
        return Text(empty, style="muted")
    text = Text()
    for i, hop in enumerate(shown):
        dim = dim_from is not None and i >= dim_from
        if i:
            if cursor_arrow is not None and i - 1 == cursor_arrow:
                text.append(" ")
                text.append("→", style="selected")
                text.append(" ")
            else:
                text.append(" → ", style="faint" if dim else "muted")
        text.append_text(
            _path_node(
                hop, resolve, prefix_bytes=prefix_bytes, self_name=self_name,
                show_hash=show_hash, hash_bytes=hash_bytes, device_hash=device_hash,
                dim=dim, hash_as_name=hash_as_name,
            )
        )
    return text


def _path_node(
    hop: Optional[str],
    resolve: NodeResolver,
    *,
    prefix_bytes: int,
    self_name: Optional[str],
    show_hash: bool,
    hash_bytes: Optional[int],
    device_hash: Optional[str],
    dim: bool,
    hash_as_name: bool = False,
) -> Text:
    """One node of :func:`path_text` (see there for the rendering rules)."""
    note_style = "faint" if dim else "muted"
    if hop is None:
        text = Text(self_name or LOCAL_DEVICE_LABEL, style="faint" if dim else "you")
        annotated = _shorten_hash(device_hash, hash_bytes) if device_hash else ""
        if show_hash and annotated:
            text.append(f" ({annotated})", style=note_style)
        return text
    named = resolve(hop)
    if named and named != hop:
        if dim:
            style = "faint"
        elif self_name and named == self_name:
            style = "you"
        else:
            style = name_style(named, hop)
        text = Text(named, style=style)
        if show_hash:
            text.append(f" ({_shorten_hash(hop, hash_bytes)})", style=note_style)
        return text
    shown = _shorten_hash(hop, hash_bytes)
    if dim:
        return Text(shown, style="faint")
    if hash_as_name:
        # No name: the hash is the node's identity. Show it at the path-hash-mode
        # width, fully muted (prefix_bytes=0 lights nothing — colour is the "this is
        # a name" signal, and there is no name), then annotate it with the byte it
        # was addressed by, exactly as a named hop is — unless that byte already *is*
        # the whole shown hash. So a 3-byte mode reads ``e839f2 (e8)`` and still
        # cross-references a byte-labelled route graph.
        identity = _shorten_hash(hop, prefix_bytes or None)
        text = highlighted_hash(identity, 0)
        if show_hash and shown and shown != identity:
            text.append(f" ({shown})", style=note_style)
        return text
    return highlighted_hash(shown, prefix_bytes)


def highlighted_hash(value: str, prefix_bytes: int, width: Optional[int] = None) -> Text:
    """Render a hex key with its leading path-hash prefix highlighted — THE hash widget.

    The one way MeshTerm displays a hash, wherever one appears: the first
    ``prefix_bytes`` bytes are the slice other nodes address in a forced trace path
    (the path-hash), lit in the node's hash-derived palette hue — the same hue its
    name wears (see :func:`~meshterm.ui.theme.node_style`) — with the remainder muted,
    so the addressable prefix stands out within the otherwise full key and carries the
    node's identity colour even where no name is known. A ``width`` budget
    shorter than the key ellipsizes it (the ``…`` takes the colour of the digit it
    replaces, so a highlight wider than the budget still reads as one).

    Args:
        value: The key as hex, optionally ``0x``-prefixed and mixed-case.
        prefix_bytes: Number of leading bytes the current path-hash mode addresses; ``0``
            (or negative) leaves the whole key un-highlighted.
        width: Display budget in cells; a longer key is truncated to the whole leading
            bytes that fit in ``width - 1`` cells (an even digit count — a hash reads in
            bytes, two hex digits each) plus an ellipsis, a shorter one is right-padded to
            the budget so lanes stay aligned. ``None`` shows the key whole, unpadded.

    Returns:
        A styled :class:`Text` of the key (exactly ``width`` cells when given).
    """
    raw = value.lower().removeprefix("0x")
    split = max(0, prefix_bytes) * 2
    pad = 0
    ellipsis = False
    if width is not None and len(raw) > width:
        # Truncate on a byte boundary: keep an even number of hex digits, the ellipsis
        # taking the next cell and any odd cell left over padding out, so a mid-byte digit
        # never shows and the lane still spans exactly ``width``.
        kept = max(0, width - 1)
        kept -= kept % 2
        raw, ellipsis = raw[:kept], True
        pad = width - kept - 1
    elif width is not None:
        pad = width - len(raw)
    hue = node_style(value)  # from the untruncated key, though any prefix agrees
    text = Text()
    text.append(raw[:split], style=hue)
    text.append(raw[split:], style="muted")
    if ellipsis:
        text.append("…", style=hue if len(raw) < split else "muted")
    text.append(" " * pad)
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


#: The pure-white our-own-node hue, matching the ``you`` style — an endpoint that is us
#: (and any relay resolving to our own name) takes it over its palette colour.
_SELF_RGB: RGB = (255, 255, 255)


#: The truecolour of the ``muted`` grey — what a keyless name's hue collapses to on a
#: raster (mirrors ``theme``'s muted ``#94a3b8``).
_MUTED_RGB: RGB = (148, 163, 184)


def _name_rgb(name: str, key: Optional[str] = None) -> RGB:
    """The RGB of a node's stable palette hue (``theme.name_style`` minus its bold).

    A name with no resolvable key styles ``muted`` (no hex to parse), so it lands on
    the muted grey here — the raster twin of the app-wide keyless-stays-muted rule.
    """
    hexpart = name_style(name, key).split()[-1]
    return parse_hex(hexpart) if hexpart.startswith("#") else _MUTED_RGB


def name_rgb(name: str, key: Optional[str] = None) -> RGB:
    """The truecolour of a node's stable palette hue, for a braille canvas.

    The public face of :func:`_name_rgb` — the same hue :func:`~meshterm.ui.theme.name_style`
    paints a name in (hash-derived when ``key`` is given), as an ``(r, g, b)`` tuple a
    raster can plot. A caller colouring node markers by their mesh identity (the trophy
    case's area drawing) mints them through here.
    """
    return _name_rgb(name, key)


#: Maps a relay hash to its node type (see the ``NODE_TYPE_*`` constants), or ``None`` when
#: the type is unknown — the hook that lets the route graph mark a repeater ``▲`` and not
#: just a generic ``●``. Endpoint sentinels are never passed to it.
TypeOf = Callable[[str], Optional[int]]


def route_graph_style(
    *,
    resolve: NodeResolver,
    self_name: Optional[str],
    source: Optional[str],
    type_of: Optional[TypeOf] = None,
    key_of: Optional[NameKeyResolver] = None,
) -> tuple[GlyphOf, LabelOf, LabelRgbOf]:
    """Build the per-node callbacks that draw a route on THE route graph (``pathgraph``).

    The shared presentation the Message paths dialog and the Trophy case both draw their
    graphs with: the two endpoints carry node names, every relay in between its marker
    plus the first byte of its hash, and each label takes its node's own name hue (us the
    pure-white ``you``) so a byte reads as the mesh name it stands for. It maps the graph's
    two endpoint sentinels — :data:`~meshterm.ui.pathgraph.SRC_NODE` on the left,
    :data:`~meshterm.ui.pathgraph.DST_NODE` (always us) on the right — plus every relay
    hash, to a glyph, a label, and a label colour.

    Args:
        resolve: Maps a relay's hash to a friendly name when one is known.
        self_name: Our own node's name — the right endpoint's label, drawn white.
        source: The left endpoint's display name (a message's origin, or us for a walk
            that starts at home — pass ``self_name`` to draw both ends as us). ``None``
            reads as an unknown ``?`` origin.
        type_of: Maps a relay's hash to its node type, so a relay draws its own map marker
            (``▲`` repeater, ``■`` room, ``◉`` sensor) in the shared palette instead of a
            generic dot. ``None``, or a hash whose type it can't resolve, keeps the old
            named-dot / unknown-ring fallback.
        key_of: Maps the ``source`` display name back to its node's key (see
            :func:`~meshterm.services.trace_runner.make_name_key_resolver`), so the left
            endpoint's label takes its key-derived hue; ``None``, or a name it can't
            place, leaves the origin muted — colour is reserved for keyed identities.

    Returns:
        The ``(glyph_of, label_of, label_rgb_of)`` triple to hand to
        :func:`~meshterm.ui.pathgraph.render_path_graph`.
    """
    src_is_self = bool(source) and source == self_name

    def glyph_of(node: str) -> tuple[str, str]:
        """Us a star, a typed relay its map marker, a named node a dot, else a ring."""
        if node == DST_NODE:
            return _SELF
        if node == SRC_NODE:
            return _SELF if src_is_self else (_NODE if source else _UNKNOWN)
        if type_of is not None:
            node_type = type_of(node)
            if node_type is not None:
                return _NODE_GLYPHS.get(node_type, _DEFAULT_GLYPH)
        named = resolve(node)
        return _NODE if named and named != node else _UNKNOWN

    def label_of(node: str) -> Optional[str]:
        """Endpoints by name, relays by their first hash byte."""
        if node == DST_NODE:
            return self_name or "you"
        if node == SRC_NODE:
            return source or "?"
        return node[:2]

    def label_rgb_of(node: str) -> RGB:
        """A label's colour: its node's name hue, us pure white, an unknown its ring."""
        if node == DST_NODE:
            return _SELF_RGB
        if node == SRC_NODE:
            if src_is_self:
                return _SELF_RGB
            if not source:
                return parse_hex(_UNKNOWN[1])
            return _name_rgb(source, key_of(source) if key_of else None)
        named = resolve(node)
        if named and named != node:
            return _name_rgb(named, node)
        return parse_hex(glyph_of(node)[1])

    return glyph_of, label_of, label_rgb_of


def _route_path(
    result: TraceResult,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = _identity,
    device_hash: Optional[str] = None,
    *,
    bare_self: bool = False,
    show_hash: bool = True,
) -> PathLine:
    """A trace's walked route as THE path widget's line object.

    The same route :func:`_route_text` renders — this hands back the
    :class:`~meshterm.ui.pathline.PathLine` itself, so a surface with room to spare
    can wrap it at hop boundaries (:meth:`~meshterm.ui.pathline.PathLine.wrapped`)
    rather than taking the one-liner and folding it mid-name.

    Args:
        result: The trace whose route to build.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        device_hash: Our own device's key/hash, annotated onto its endpoints when known.
        bare_self: Draw both ``us`` endpoints as their bare arrow — for a surface (the
            trace screens' route lane) where a walk starting and ending on us is the
            premise, not news.
        show_hash: Annotate each *named* hop with the hash it was addressed by. On for
            the scripted table output, where the hash is half the answer; off for the
            live screens, whose route lane reads as the sequence of nodes and leaves
            the hex to the wire-spec lane below it. Unnamed hops always show their
            hash — it is the only identity they have.

    Returns:
        The route's :class:`~meshterm.ui.pathline.PathLine` (hopless — reading
        ``no hops recorded`` — when the trace recorded none).
    """
    edges = result.edges(device_label)
    if not edges:
        return PathLine([], empty="no hops recorded")
    hash_bytes = result.path_hash_bytes
    nodes = [edges[0].origin] + [edge.destination for edge in edges]
    hops = [None if not node or node == device_label else node for node in nodes]
    return path_line(
        hops, resolve, prefix_bytes=hash_bytes or 8, self_name=device_label,
        show_hash=show_hash, hash_bytes=hash_bytes, device_hash=device_hash,
        bare_self=bare_self,
    )


def _route_text(
    result: TraceResult,
    device_label: str = LOCAL_DEVICE_LABEL,
    resolve: NodeResolver = _identity,
    device_hash: Optional[str] = None,
) -> Text:
    """Render a trace's walked route through THE path widget, on one line.

    Shows the path the trace actually walked — the forced path, or the route the
    device resolved when auto-routing — in the trace presentation: each node
    annotated by its hash at the command's path-hash width, our own device
    bracketing both ends, e.g.
    ``Me (a1b2) → Alice (3d63) → Bob (f2a1) → Me (a1b2)``.

    Args:
        result: The trace whose route to display.
        device_label: Name to show for our own device at the path's endpoints.
        resolve: Maps a raw hop hash to a friendly contact name when known.
        device_hash: Our own device's key/hash, annotated onto its endpoints when known.

    Returns:
        A :class:`Text` with the node sequence, or a muted note when no hops exist.
    """
    return _route_path(result, device_label, resolve, device_hash).text()


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


def node_marker(node_type: Optional[int]) -> tuple[str, RGB]:
    """The map-palette glyph and truecolour for a node type, as a ``(glyph, rgb)`` pair.

    The shared node-type marks (``▲`` repeater, ``■`` room, ``◉`` sensor, ``●`` plain
    node) in the map's own colours, minted here for a braille raster the way
    :data:`_NODE_GLYPHS` mints them for a Rich row — so a spatial drawing pins its nodes
    in the exact glyphs and hues the map and the nodes list use. An unknown type falls
    back to the plain node mark.
    """
    glyph, color = _NODE_GLYPHS.get(node_type or -1, _DEFAULT_GLYPH)
    return glyph, parse_hex(color)


def self_marker() -> tuple[str, RGB]:
    """Our own node's map marker — the yellow ``★`` — as a ``(glyph, rgb)`` pair."""
    return _SELF[0], parse_hex(_SELF[1])

# A heat-map gradient for a node's heard age, hottest (most recently heard) to coldest: white
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


def format_ago(secs: Optional[float]) -> str:
    """The relative-age *phrase* — ``now``, ``5m ago``, ``never`` — for running prose.

    The canonical grammar for every "heard … (…)" and "delivered …" row: a fresh
    sighting reads as bare ``now`` and an unknown one as bare ``never`` (neither takes
    the suffix — "now ago" is nonsense), while any measured age reads ``5m ago``.
    Callers embedding an age in a sentence or parenthetical use this;
    :func:`_format_age` stays the bare column form for aligned age lanes.
    """
    age = _format_age(secs)
    return age if age in ("now", "never") else f"{age} ago"


def _recency_style(secs: Optional[float]) -> str:
    """The heat-map colour for a node's heard age of ``secs`` (hotter = more recent).

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


# The columns the contact list can be sorted by, left-to-right, and the direction each opens on
# — name A→Z, most-recently-heard first, most packets first — chosen so a fresh sort shows
# the "interesting" end at the top.
_SORT_COLUMNS: tuple[str, ...] = ("name", "heard", "packets")
_SORT_OPENS_ASCENDING: dict[str, bool] = {"name": True, "heard": True, "packets": False}


@dataclass
class ContactsSort:
    """Which column the contact list is sorted by, and in which direction.

    ``column`` is one of :attr:`columns`; ``ascending`` sorts the column's underlying
    metric low-to-high — name A→Z, *age* (so ascending = most recently heard first), packet
    count low-to-high. The interactive screen mutates this in place as the user presses the
    arrows.

    The sort *ring* is per-instance: :attr:`columns` and :attr:`opens_ascending` default to
    the Contacts list's three (:data:`_SORT_COLUMNS`), but the Time Machine picker passes a
    wider set — it adds a sortable ``hash`` column — so the same model drives both without a
    module-global column list that one screen would have to share with the other.
    """

    column: str = "name"
    ascending: bool = True
    columns: tuple[str, ...] = _SORT_COLUMNS
    opens_ascending: dict[str, bool] = field(default_factory=lambda: _SORT_OPENS_ASCENDING)

    @classmethod
    def from_name(
        cls,
        name: str,
        columns: tuple[str, ...] = _SORT_COLUMNS,
        opens_ascending: Optional[dict[str, bool]] = None,
    ) -> "ContactsSort":
        """Build a sort for ``name`` over ``columns``, opening in that column's natural direction.

        Falls back to the ring's first column when ``name`` isn't one of ``columns`` (so a
        stale saved key can't wedge the sort). ``opens_ascending`` defaults to the Nodes
        list's directions when omitted.
        """
        opens = opens_ascending if opens_ascending is not None else _SORT_OPENS_ASCENDING
        column = name if name in columns else columns[0]
        return cls(column, opens[column], columns, opens)

    def move(self, delta: int) -> None:
        """Step the active column ``delta`` places (wrapping), adopting its natural direction."""
        index = (self.columns.index(self.column) + delta) % len(self.columns)
        self.column = self.columns[index]
        self.ascending = self.opens_ascending[self.column]


def _ordered_contacts(
    contacts: list[Contact], counts: dict[str, int], sort: ContactsSort
) -> list[Contact]:
    """Contacts sorted per ``sort`` (our own node is pinned separately, above these).

    The active column's metric drives the order (reversed for a descending sort); ties always
    break by case-folded name *ascending*, so two contacts sharing a metric (e.g. the same
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


def _sort_header(label: str, column: str, sort: ContactsSort) -> str:
    """A column header: plain-muted, or cyan with a direction triangle when it's the sort key.

    The active column's name and its triangle are lit cyan together (so the interactive
    left/right selection is obvious) — ``▲`` for ascending, ``▼`` for descending. The column
    reserves its width (see :func:`contacts_table`) so toggling the sort doesn't shift the row.
    """
    if column != sort.column:
        return label
    triangle = "▲" if sort.ascending else "▼"
    return f"[bold #22d3ee]{label} {triangle}[/]"


def node_type_legend(indent: str = "") -> Text:
    """The one-line key to the node-type marks: ``★ you  ▲ repeater  ● node  …``.

    Every glyph in its shared map colour (see :data:`_NODE_GLYPHS`), each named muted after
    it. THE legend for any surface that draws typed node markers — the contacts list under its
    table, the route graph under its lanes — so one glyph means one thing app-wide.

    Args:
        indent: Leading spaces to sit the legend under a table or graph body.
    """
    legend = Text(indent)
    legend.append(_SELF[0], style=_SELF[1])
    legend.append(" you", style="muted")
    for node_type in (NODE_TYPE_REPEATER, NODE_TYPE_CHAT, NODE_TYPE_ROOM, NODE_TYPE_SENSOR):
        glyph, color = _NODE_GLYPHS[node_type]
        legend.append("   ")
        legend.append(glyph, style=color)
        legend.append(f" {NODE_TYPE_LABELS[node_type]}", style="muted")
    return legend


def tab_strip(labels: Sequence[str], active: int, width: int) -> Group:
    """A boxed tab strip: every tab always boxed on top, the active one open into the page.

    THE navigation header for a screen that pages one full-height view at a time between a
    handful of named views (the node detail page's ``Info`` / ``Routes``) instead of stacking
    them. Every tab is always drawn as a full-topped box — a dim ``faint`` outline by
    default, lit ``accent`` when active — instead of only the active one; this keeps every
    tab's width fixed regardless of which is selected, so switching tabs never shifts the
    labels that come after it. Tabs share a vertical between neighbours (one glyph, not
    two), and each shared *top* corner is drawn as if the tab to the left sits on top of the
    tab to the right: it "opens" (rounds toward the tab starting there) only at the very
    first tab and at the active tab's own left edge, and "closes" (rounds back, ceding the
    position to the tab on its left) everywhere else — so the active tab is the one exception
    that always wins both of its top corners, reading as lifted in front of its neighbours.

    The *bottom* border is a single continuous accent-coloured rule — it's one line, so it
    carries one colour throughout, the active tab's — that the inactive tabs sit flush
    against: they get no corners or seams of their own down there, just the rule passing
    behind them, broken only under the active tab, which is why it reads as merged into the
    page below it: the rule turns up into ``╯`` (closing off the flat run from the west,
    turning north into the tab's wall), leaves the active tab's width open (no line — that
    gap *is* the page starting), then turns back down through ``╰`` (the wall meeting the
    flat run continuing east) and carries on flat. A lone tab collapses to the plain accent
    heading (:func:`~meshterm.ui.menus.section_heading`'s ``── Label ──`` form) since there
    is nothing to switch between or layer. When there's slack in ``width``, the tab boxes
    (top and label rows) sit two columns in from the left; the bottom rule is one continuous
    line regardless, so it fills that margin rather than being indented with them. The
    owning screen switches the active index (``Tab``/``Shift+Tab``); the strip itself is
    pure presentation.

    Args:
        labels: The tab names in display order.
        active: Index of the lit tab.
        width: The strip's render width — the closing rule fills out to it.

    Returns:
        A :class:`~rich.console.Group` of one line (a lone tab, or none) or three (the shared
        top border, the boxed labels, and the shared bottom rule).
    """
    if not labels:
        return Group(Text(""))
    if len(labels) == 1:
        return Group(Text(f"── {labels[0]} ──", style="accent", no_wrap=True))

    n = len(labels)
    inner = [f"  {label}  " for label in labels]
    content_width = sum(len(cell) for cell in inner) + (n + 1)
    margin = 2 if content_width + 2 <= width else 0
    pad = " " * margin

    def top_owner(junction: int) -> int:
        """Which tab's top corner glyph sits at this junction — see the docstring's rule."""
        return junction if junction == 0 or junction == active else junction - 1

    def top_style(junction: int) -> str:
        return "accent" if top_owner(junction) == active else "faint"

    def bot_glyph(junction: int) -> str:
        """The bottom rule's glyph at this junction — a corner turning into/out of the
        active tab's wall, else a flat pass-through. Always accent: see the docstring."""
        if junction == active:
            return "╯"
        if junction == active + 1:
            return "╰"
        return "─"

    top = Text(pad, no_wrap=True)
    mid = Text(pad, no_wrap=True)
    bot = Text("─" * margin, style="accent", no_wrap=True)
    for i, label in enumerate(labels):
        opens = i == 0 or i == active
        top.append("╭" if opens else "╮", style=top_style(i))
        mid.append("│", style=top_style(i))
        bot.append(bot_glyph(i), style="accent")
        tab_style = "accent" if i == active else "faint"
        top.append("─" * len(inner[i]), style=tab_style)
        mid.append(inner[i], style=tab_style)
        bot.append(" " * len(inner[i]) if i == active else "─" * len(inner[i]), style="accent")
    top.append("╮", style=top_style(n))
    mid.append("│", style=top_style(n))
    bot.append(bot_glyph(n), style="accent")
    bot.append("─" * max(0, width - len(bot.plain)), style="accent")
    return Group(top, mid, bot)


def _contacts_legend() -> Text:
    """The node-type legend, indented to sit under the contacts table body."""
    return node_type_legend(indent="  ")


def contacts_table(
    self_name: str,
    self_key: str,
    contacts: list[Contact],
    prefix_bytes: int,
    counts: dict[str, int],
    sort: Optional[ContactsSort] = None,
) -> Group:
    """List this node and its known contacts with recency, packets, type, key, and legend.

    Our own node is the first row (``★``, name in the white ``you`` style); contacts follow
    in ``sort`` order.
    A per-type glyph marks each node in the app's shared colours, the name takes the node's
    hash-derived palette hue, the heard age glows with recency heat (brighter = fresher),
    and the full key is shown with its path-hash prefix lit — chopped with an ellipsis only
    when the terminal is too narrow.

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
    sort = sort if sort is not None else ContactsSort()

    # expand=True lets the key column (the only flexible one) soak up all spare width and be
    # the sole column Rich squeezes when narrow — the fixed columns keep their natural size.
    table = Table(
        title=f"[accent]Contacts[/accent]  [muted]· {len(contacts)} known[/muted]",
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
        # Our own name is the app-wide pure-white "you" style, never a palette hue.
        Text.assemble((self_name, "you"), ("  (you)", "muted")),
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
            Text(c.name, style=name_style(c.name, c.public_key or c.key_prefix)),
            Text(_format_age(secs), style=_recency_style(secs)),
            Text(str(pkts), style="muted") if pkts else Text("—", style="faint"),
            highlighted_hash(c.public_key, prefix_bytes) if c.public_key else unknown,
        )
    return Group(table, Text(""), _contacts_legend())


def tx_opt_table(result: TxOptResult) -> Table:
    """Render every measured TX level of an optimization sweep, best row highlighted.

    Args:
        result: The optimization result to display.

    Returns:
        A Rich :class:`Table` of TX level, target SNR, success rate, and sample count.
    """
    table = Table(
        title=f"TX sweep · {result.admin_node} → {result.target}",
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


# -- battery gauge --------------------------------------------------------------------------

#: Unicode braille pattern base; add an 8-bit dot mask (a 2×4 cell) to get the glyph.
_BRAILLE_BASE = 0x2800

#: Dot bit per (column, row) within a braille cell — the Unicode standard layout, the same
#: mapping the map canvas rasters with. Kept here so the one-cell battery glyph needn't reach
#: into the canvas module for two constants.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))

#: Battery charge tiers: ``(percent floor, braille rows lit from the bottom, colour)``,
#: highest first. The glyph is one braille cell (2×4 dots) filled from its bottom row up, so
#: four coarse levels read as a shrinking fuel gauge: the whole cell above three-quarters,
#: six dots above half, four above a quarter, two below it — green while healthy, orange at a
#: quarter, red near empty (the app's ok/warn/err semantics as a gauge).
_BATTERY_TIERS: tuple[tuple[int, int, str], ...] = (
    (75, 4, "batt.high"),
    (50, 3, "batt.high"),
    (25, 2, "batt.mid"),
    (0, 1, "batt.low"),
)

#: At or below this charge the red glyph blinks with a dim frame — a can't-miss low warning.
_BATTERY_CRITICAL = 10

#: Frames in the charging sweep: empty → four fills → round again (bottom-to-full loop).
_CHARGE_FRAMES = 5


def _braille_fill(rows: int) -> str:
    """A single braille cell with its bottom ``rows`` (0–4) dot rows lit."""
    rows = max(0, min(4, rows))
    bits = 0
    for row in range(4 - rows, 4):
        bits |= _BRAILLE_DOTS[0][row] | _BRAILLE_DOTS[1][row]
    return chr(_BRAILLE_BASE + bits)


def _battery_tier(percent: int) -> tuple[int, str]:
    """The ``(rows lit, colour)`` a charge level draws at (see :data:`_BATTERY_TIERS`)."""
    for floor, rows, color in _BATTERY_TIERS:
        if percent >= floor:
            return rows, color
    return 1, "batt.low"


def battery_cell(percent: int, *, charging: bool = False, frame: int = 0) -> Text:
    """The status-bar battery gauge: one braille cell coloured by charge, then its ``%``.

    The braille cell fills from the bottom up in four coarse steps (see
    :data:`_BATTERY_TIERS`), coloured green while healthy, orange at a quarter, red near
    empty. Two live states animate off the caller's ``frame`` counter (advanced one step per
    repaint tick), so the gauge moves without any per-frame plumbing:

    * **Charging** overrides the fill: the cell sweeps empty-to-full on a loop, the charge
      colour held, so a plugged-in pack visibly climbs — while the number beside it stays the
      *true* charge, never the animation's.
    * **Critically low** (≤ :data:`_BATTERY_CRITICAL`%, not charging) blinks the red cell to a
      dim slate on alternate frames — an unmissable pulse in the corner.

    Args:
        percent: State of charge, 0–100 (clamped).
        charging: Whether the pack is taking charge (drives the fill sweep).
        frame: A monotonically advancing tick; only its phase is read, so any
            steadily-incrementing integer animates the two live states.

    Returns:
        A Rich :class:`Text`: the coloured glyph, a space, and ``NN%`` in muted text.
    """
    pct = max(0, min(100, int(percent)))
    rows, color = _battery_tier(pct)
    if charging:
        # The charge colour is held; only the fill sweeps, so it reads as "filling", not
        # as the charge itself jumping around.
        rows = frame % _CHARGE_FRAMES
    elif pct <= _BATTERY_CRITICAL and frame % 2:
        color = "batt.dim"
    out = Text(_braille_fill(rows), style=color)
    out.append(f" {pct}%", style="muted")
    return out
