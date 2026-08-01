"""The ``map`` tool: plot the mesh's located nodes on an OpenStreetMap terminal map.

Nodes that share their location in adverts are collected from the passive-monitor history
(:meth:`~meshterm.persistence.repository.Repository.heard_nodes`), our own node is added when
its position is known, and everything is drawn over a real street basemap rendered as Unicode
braille (streets, rivers, place names — the same terminal-map idea as ``mapscii``).

In the menu it opens a full-screen, pannable/zoomable map (see
:mod:`meshterm.ui.map_screen`). On the CLI it prints a one-shot render fitted to the nodes.
Repeaters are prioritised over ordinary nodes: a distinct marker, drawn on top, listed first.
The basemap is best-effort — with no network (and no cached tiles) the nodes are plotted on a
blank grid instead, and the tool still works offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

import typer
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.geo import DEFAULT_VIEW_FRACTION, usable_fix
from ..core.models import NODE_TYPE_REPEATER
from .base import Tool, ToolResult, register

if TYPE_CHECKING:
    from ..ui.map_render import MapMarker


@register
class MapTool(Tool):
    """Show the mesh's location-sharing nodes on a braille OpenStreetMap map."""

    name = "map"
    title = "Map"
    icon = "🌍"
    help = "Show mesh nodes on a street map (pannable; repeaters highlighted)"
    category = "Mesh"
    order = 30

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Gather located nodes and open the interactive map, or render a static one.

        Args:
            ctx: Shared application context.
            params: Optional ``static`` (force one-shot render), ``width``, ``zoom``,
                ``basemap`` (set ``False`` to skip fetching tiles).

        Returns:
            A :class:`ToolResult` summarizing how many nodes were plotted.
        """
        from ..ui.surface import TuiUi

        markers = await self._gather(ctx)
        if not markers:
            ctx.ui.note(
                "[muted]no contacts or heard nodes have shared a location yet — a node "
                "appears here once it advertises coordinates[/muted]"
            )
            return ToolResult(summary={"located": 0})

        interactive = isinstance(ctx.ui, TuiUi) and not params.get("static")
        if interactive:
            from ..ui.map_screen import open_map

            await open_map(
                ctx, markers, fraction=params.get("fraction", DEFAULT_VIEW_FRACTION)
            )
        else:
            await self._render_static(ctx, markers, params)

        repeaters = sum(1 for m in markers if m.is_repeater and not m.is_self)
        self_located = any(m.is_self for m in markers)
        return ToolResult(
            summary={
                "located": len(markers),
                "repeaters": repeaters,
                "nodes": len(markers) - repeaters - (1 if self_located else 0),
                "self_located": self_located,
            },
            message=None if interactive else (
                f"[ok]✓[/ok] mapped [brand]{len(markers)}[/brand] located nodes "
                f"([accent]{repeaters}[/accent] repeaters)"
            ),
        )

    # -- marker gathering -------------------------------------------------------

    async def _gather(self, ctx: AppContext) -> list["MapMarker"]:
        """Collect every located node to plot (see :func:`gather_markers`)."""
        return await gather_markers(ctx)

    # -- static (CLI) render ----------------------------------------------------

    async def _render_static(
        self, ctx: AppContext, markers: list["MapMarker"], params: dict[str, Any]
    ) -> None:
        """Fetch tiles synchronously and print a one-shot map fitted to the nodes."""
        import asyncio

        from ..core.geo import Viewport
        from ..ui.map_render import render_map
        from ..ui.map_screen import basemap_source

        cell_w = int(params.get("width") or min(ctx.console.size.width - 2, 160))
        cell_h = max(12, cell_w * 4 // 13)  # keep roughly the terminal's aspect
        dot_w, dot_h = cell_w * 2, cell_h * 4

        source = basemap_source(ctx)
        max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
        coords = [(m.lat, m.lon) for m in markers]
        if params.get("zoom") is not None:
            from ..core.geo import BBox

            center = BBox.around(coords).center
            vp = Viewport(center[0], center[1], int(params["zoom"]), dot_w, dot_h)
        else:
            vp = Viewport.fit(
                coords,
                dot_w,
                dot_h,
                max_zoom=max_zoom,
                fraction=params.get("fraction", DEFAULT_VIEW_FRACTION),
            )

        tiles = {}
        if params.get("basemap", True):
            for tile in vp.tiles(max_zoom):
                tiles[tile] = await asyncio.to_thread(source.load_tile, *tile)

        lines = render_map(vp, tiles, markers)
        body = Text.from_ansi("\n".join(lines))
        ctx.ui.show(Group(body, Text(""), _legend(markers)))

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``map`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _map(
            width: Optional[int] = typer.Option(
                None, "--width", "-w", help="Map width in character cells"
            ),
            zoom: Optional[int] = typer.Option(
                None, "--zoom", "-z", help="Fixed zoom level (omit to fit the nodes)"
            ),
            fraction: float = typer.Option(
                DEFAULT_VIEW_FRACTION,
                "--fraction",
                "-f",
                min=0.0,
                max=1.0,
                help="Fraction of nodes to frame: the densest that many, so distant "
                "outliers don't zoom the view out. 1.0 fits every node. Ignored with --zoom",
            ),
            basemap: bool = typer.Option(
                True, "--basemap/--no-basemap", help="Draw the OpenStreetMap street basemap"
            ),
        ) -> None:
            if not 0.0 < fraction <= 1.0:
                raise typer.BadParameter("--fraction must be greater than 0 and at most 1")
            params: dict[str, Any] = {
                "static": True, "basemap": basemap, "fraction": fraction,
            }
            if width is not None:
                params["width"] = width
            if zoom is not None:
                params["zoom"] = zoom
            run_tool_command(self, params)


async def gather_markers(ctx: AppContext) -> list["MapMarker"]:
    """Collect every located node to plot: the device's contacts, plus our own node.

    The companion's **contact list** is the authoritative source for a node's name,
    type (repeater vs. leaf), and advertised position — the passive-monitor
    observations only carry a location for the rare node that broadcasts one in an
    advert. So contacts drive the markers, and each contact is enriched with signal
    detail from the observations when we've overheard it directly.

    Shared by the ``map`` tool and the config editor's pick-a-location map, so both
    show the same mesh.
    """
    from ..ui.map_render import MapMarker

    observed = {n.node: n for n in ctx.repo.heard_nodes() if n.node}
    markers: list[MapMarker] = []
    seen: set[str] = set()

    for contact in await _contacts(ctx):
        if not contact.has_location or not usable_fix(contact.lat, contact.lon):
            continue
        key = contact.key_prefix or (contact.public_key or "")[:12]
        if key:
            seen.add(key)
        markers.append(
            MapMarker(
                label=contact.name or key or "?",
                lat=float(contact.lat),
                lon=float(contact.lon),
                is_repeater=contact.is_repeater,
                detail=_signal_detail(observed.get(key)),
                key=contact.public_key or contact.key_prefix or None,
            )
        )

    # A node we overheard advertising a location but that isn't in our contacts.
    for node in observed.values():
        if not node.has_location or node.node in seen:
            continue
        if not usable_fix(node.lat, node.lon):
            continue
        markers.append(
            MapMarker(
                label=node.name or node.node or "?",
                lat=float(node.lat),
                lon=float(node.lon),
                is_repeater=node.is_repeater,
                detail=_signal_detail(node),
                key=node.public_key or node.node or None,
            )
        )

    self_marker = await _self_marker(ctx)
    if self_marker is not None:
        markers.append(self_marker)
    return markers


async def _contacts(ctx: AppContext) -> list:
    """Fetch the device's contacts, best-effort (an unreachable radio yields none)."""
    try:
        return await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the map still works from observations alone
        return []


async def _self_marker(ctx: AppContext) -> Optional["MapMarker"]:
    """Build a marker for our own node from the device, if its location is known."""
    from ..ui.map_render import MapMarker

    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - the map is useful without our own position
        return None
    lat, lon = _as_float(info.get("adv_lat")), _as_float(info.get("adv_lon"))
    if lat is None or lon is None or not usable_fix(lat, lon):
        return None  # a device with no fix reports 0/0 (or nonsense out-of-range)
    return MapMarker(
        label=str(info.get("name") or "this node"),
        lat=lat,
        lon=lon,
        is_repeater=info.get("adv_type") == NODE_TYPE_REPEATER,
        is_self=True,
        key=str(info.get("public_key") or "") or None,
    )


def _legend(markers: list["MapMarker"]) -> Table:
    """A compact legend: self → repeaters → leaf nodes, with coordinates and detail.

    Names take their key-derived hue (our own the pure-white ``you``), matching the
    interactive map's labels; the glyph keeps the marker's type colour.
    """
    from ..ui.map_render import _NODE, _REPEATER, _SELF
    from ..ui.theme import name_style

    table = Table(box=None, padding=(0, 2, 0, 0), expand=False)
    table.add_column("")
    table.add_column("NODE")
    table.add_column("TYPE")
    table.add_column("COORDS", justify="right")
    table.add_column("HEARD")
    ordered = sorted(markers, key=lambda m: -m._rank())
    for m in ordered:
        glyph, color = _SELF if m.is_self else (_REPEATER if m.is_repeater else _NODE)
        kind = "you" if m.is_self else ("repeater" if m.is_repeater else "node")
        table.add_row(
            Text(glyph, style=color),
            Text(m.label, style="you" if m.is_self else name_style(m.label, m.key)),
            Text(kind, style="muted"),
            Text(f"{m.lat:.4f}, {m.lon:.4f}", style="muted"),
            Text(m.detail, style="muted"),
        )
    return table


def _signal_detail(node: object) -> str:
    """A compact 'N pkts · +x.x dB' reception note for a heard node, or '' if unheard."""
    if node is None:
        return ""
    detail = f"{node.count} pkts"  # type: ignore[attr-defined]
    if node.median_snr is not None:  # type: ignore[attr-defined]
        detail += f" · {node.median_snr:+.1f} dB"  # type: ignore[attr-defined]
    return detail


def _as_float(value: object) -> Optional[float]:
    """Best-effort float conversion, returning ``None`` on missing/garbage values."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
