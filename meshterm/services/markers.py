# SPDX-License-Identifier: Apache-2.0
"""Collecting the mesh's located nodes into map markers.

One list, three surfaces: the ``map`` tool's full-screen map, the node page's preview minimap,
and the editors' pick-a-location map all plot the same mesh, so they all come through
:func:`gather_markers`. It sits in ``services/`` because it is an assembly job over the
device's contacts and the observation history — the layer below anything that draws — and
because two screens and one tool need it, none of which may import from the others.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.geo import usable_fix
from ..core.models import NODE_TYPE_REPEATER, MapMarker

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext


async def gather_markers(ctx: AppContext, *, wait: bool = True) -> list[MapMarker]:
    """Collect every located node to plot: the device's contacts, plus our own node.

    The companion's **contact list** is the authoritative source for a node's name,
    type (repeater vs. leaf), and advertised position — the passive-monitor
    observations only carry a location for the rare node that broadcasts one in an
    advert. So contacts drive the markers, and each contact is enriched with signal
    detail from the observations when we've overheard it directly.

    Args:
        ctx: Shared application context.
        wait: Whether to wait on the radio for contacts and our own position the session
            has not cached yet. ``False`` builds the markers from what is already in hand —
            the cache and our own history — and never makes a round trip, for a surface
            whose nodes are only context (the location picker): a companion refusing the
            contacts read can otherwise hold it closed for twenty-odd seconds.
    """
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
