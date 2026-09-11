"""The ``map`` tool: plot the mesh's located nodes on an OpenStreetMap terminal map.

Nodes that share their location in adverts are collected from the passive-monitor history
(:meth:`~meshterm.persistence.repository.Repository.heard_nodes`), our own node is added when
its position is known, and everything is drawn over a real street basemap rendered as Unicode
braille (streets, rivers, place names — the same terminal-map idea as ``mapscii``).

Menu-only: it opens a full-screen, pannable/zoomable map (see
:mod:`meshterm.ui.map_screen`) and registers no CLI subcommand, because a map is a picture
and the scripted CLI deals in facts (see :meth:`MapTool.register_cli`). Repeaters are
prioritised over ordinary nodes: a distinct marker, drawn on top. The basemap is
best-effort — with no network (and no cached tiles) the nodes are plotted on a blank grid
instead, and the tool still works offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core.geo import usable_fix
from ..core.models import NODE_TYPE_REPEATER
from .base import Tool, ToolResult, register


def _fraction(ctx: AppContext, params: dict[str, Any]) -> float:
    """How much of the mesh a map opens framed on: the flag if one was passed, else the preference.

    ``--fraction`` is resolved here rather than as the Typer option's default because the
    options are declared at CLI *registration* time — before a context, and so before the
    preferences file has been read.

    Args:
        ctx: Shared application context.
        params: This invocation's parameters.

    Returns:
        The fraction to frame.
    """
    fraction = params.get("fraction")
    return ctx.preferences.map_view_fraction if fraction is None else float(fraction)


if TYPE_CHECKING:
    from ..ui.map_render import MapMarker


@register
class MapTool(Tool):
    """Show the mesh's location-sharing nodes on a braille OpenStreetMap map."""

    name = "map"
    title = "Map"
    icon = "🌍"
    help = "Mesh nodes on a pannable street map"
    category = "Explore"
    order = 10

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Gather the located nodes and open the interactive map.

        Menu-only (see :meth:`register_cli`), so there is one path through here.

        Args:
            ctx: Shared application context.
            params: Optional ``fraction`` — how much of the mesh to open framed on.

        Returns:
            A :class:`ToolResult` summarizing how many nodes were plotted.
        """
        from ..ui.map_screen import open_map

        markers = await self._gather(ctx)
        if not markers:
            ctx.ui.note(
                "[muted]no contacts or heard nodes have shared a location yet — a node "
                "appears here once it advertises coordinates[/muted]"
            )
            return ToolResult(summary={"located": 0})

        await open_map(ctx, markers, fraction=_fraction(ctx, params))

        repeaters = sum(1 for m in markers if m.is_repeater and not m.is_self)
        self_located = any(m.is_self for m in markers)
        return ToolResult(
            summary={
                "located": len(markers),
                "repeaters": repeaters,
                "nodes": len(markers) - repeaters - (1 if self_located else 0),
                "self_located": self_located,
            },
        )

    # -- marker gathering -------------------------------------------------------

    async def _gather(self, ctx: AppContext) -> list[MapMarker]:
        """Collect every located node to plot (see :func:`gather_markers`)."""
        return await gather_markers(ctx)

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command — a map is a picture, and pictures are menu-only.

        Every other feature has a scripted face because its answer is a set of facts a
        script can act on. A map's answer is a *drawing*: braille cells whose meaning is
        their position on a grid and whose nodes are told apart by colour, which is
        exactly what the scripted CLI does not have (see :mod:`meshterm.ui.script`). A
        one-shot render there would be an unparseable block of glyphs, and stripping its
        colour to match the rest of the CLI would make it unreadable as well.

        The located nodes themselves are still scriptable — ``meshterm contacts`` lists
        every one of them, coordinates included.

        Args:
            app: The Typer application (untouched).
        """


async def gather_markers(ctx: AppContext, *, wait: bool = True) -> list[MapMarker]:
    """Collect every located node to plot: the device's contacts, plus our own node.

    The companion's **contact list** is the authoritative source for a node's name,
    type (repeater vs. leaf), and advertised position — the passive-monitor
    observations only carry a location for the rare node that broadcasts one in an
    advert. So contacts drive the markers, and each contact is enriched with signal
    detail from the observations when we've overheard it directly.

    Shared by the ``map`` tool, the node page's preview and the editors' pick-a-location
    map, so all of them show the same mesh.

    Args:
        ctx: Shared application context.
        wait: Whether to wait on the radio for contacts and our own position the session
            has not cached yet. ``False`` builds the markers from what is already in hand —
            the cache and our own history — and never makes a round trip, for a surface
            whose nodes are only context (the location picker): a companion refusing the
            contacts read can otherwise hold it closed for twenty-odd seconds.
    """
    from ..ui.map_render import MapMarker

    observed = {n.node: n for n in ctx.repo.heard_nodes() if n.node}
    markers: list[MapMarker] = []
    seen: set[str] = set()

    contacts = await _contacts(ctx) if wait else (ctx.devstate.peek_contacts() or [])
    for contact in contacts:
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

    if wait:
        self_marker = await _self_marker(ctx)
    else:
        info = ctx.devstate.peek_self_info()
        self_marker = _marker_for_self(info) if info is not None else None
    if self_marker is not None:
        markers.append(self_marker)
    return markers


async def _contacts(ctx: AppContext) -> list:
    """Fetch the device's contacts, best-effort (an unreachable radio yields none)."""
    try:
        return await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the map still works from observations alone
        return []


async def _self_marker(ctx: AppContext) -> MapMarker | None:
    """Build a marker for our own node from the device, if its location is known."""
    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - the map is useful without our own position
        return None
    return _marker_for_self(info)


def _marker_for_self(info: dict) -> MapMarker | None:
    """Our own node's marker from a self-info payload, or ``None`` where it has no fix."""
    from ..ui.map_render import MapMarker

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


def _signal_detail(node: object) -> str:
    """A compact 'N pkts · +x.x dB' reception note for a heard node, or '' if unheard."""
    if node is None:
        return ""
    detail = f"{node.count} pkts"  # type: ignore[attr-defined]
    if node.median_snr is not None:  # type: ignore[attr-defined]
        detail += f" · {node.median_snr:+.1f} dB"  # type: ignore[attr-defined]
    return detail


def _as_float(value: object) -> float | None:
    """Best-effort float conversion, returning ``None`` on missing/garbage values."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
