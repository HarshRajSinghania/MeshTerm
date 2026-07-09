"""The interactive full-screen map: a pannable, zoomable slippy map in the terminal.

This drives the braille street map inside the TUI. It owns a :class:`~meshterm.core.geo.
Viewport` over the mesh's nodes, fetches the vector tiles covering it in the background (so
the UI never blocks on the network), and redraws via :func:`~meshterm.ui.map_render.
render_map`. Keys:

* ``w`` / ``a`` / ``s`` / ``d`` (or the arrow keys) pan north / west / south / east,
* holding **Shift** (Shift+arrows, or the uppercase ``W`` / ``A`` / ``S`` / ``D``) pans by a
  single character cell for fine positioning,
* ``=`` / ``+`` zoom in, ``-`` / ``_`` zoom out,
* ``r`` recenters and refits to the dense core of the nodes (the same default view the map
  opens on),
* ``Esc`` / ``q`` leaves the map.

With no network (and no cached tiles) the basemap is simply absent and nodes are plotted on a
blank grid — the map still works, it just has no streets.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Callable, Optional

from ..core.geo import EARTH_RADIUS_KM, Viewport, clamp_lat
from ..core.mvt import Layer
from ..services.basemap import BasemapSource
from .map_render import MapMarker, render_map
from .tui.screen import Screen

if TYPE_CHECKING:
    from ..context import AppContext

#: Fraction of the view a single (coarse) pan keypress moves.
_PAN_STEP = 0.30

#: Unit pan direction (east, south) for each directional action / key.
_PAN_DIRS: dict[str, tuple[int, int]] = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

#: Which pan direction each letter key drives (w/a/s/d ≈ north/west/south/east).
_PAN_KEYS: dict[str, str] = {"w": "up", "a": "left", "s": "down", "d": "right"}

#: How far past the tile source's max zoom the display may go (lower tiles are magnified).
_OVERZOOM = 2

#: Default fraction of nodes the map frames on open — the densest half, so a few distant
#: outliers don't zoom the whole mesh out to a continent. See :meth:`geo.Viewport.fit`.
DEFAULT_VIEW_FRACTION = 0.5


class MapScreen(Screen):
    """A full-screen, keyboard-driven map of the mesh's located nodes over an OSM basemap."""

    floating = False

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        markers: list[MapMarker],
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        saved_view: Optional[tuple[float, float, int]] = None,
        on_view_change: Optional[Callable[[Viewport], None]] = None,
        view_fraction: float = DEFAULT_VIEW_FRACTION,
    ) -> None:
        """Create the map screen.

        Args:
            session: The running TUI session (for size + repaint scheduling).
            markers: The located mesh nodes to plot (must be non-empty).
            source: The vector-tile source (already resolved/warmed).
            max_tile_zoom: The source's max zoom, captured off the event loop at open time.
            saved_view: A previously persisted ``(center_lat, center_lon, zoom)`` to reopen
                on, or ``None`` to frame the nodes instead.
            on_view_change: Called with the viewport whenever the centre or zoom changes, so
                the caller can persist it. Deduplicated — only actual changes fire it.
            view_fraction: Fraction of the nodes the default frame (and ``r`` reset) fits —
                the densest that many, so outliers don't dominate. See :meth:`geo.Viewport.fit`.
        """
        super().__init__()
        self.title = "mesh map"
        self._session = session
        self._markers = markers
        self._source = source
        self._max_tile_zoom = max_tile_zoom
        self._saved_view = saved_view
        self._on_view_change = on_view_change
        self._view_fraction = view_fraction
        # The view last handed to ``on_view_change``; seeded with the restored view so
        # reopening unchanged doesn't rewrite it.
        self._last_saved = saved_view
        self._viewport: Optional[Viewport] = None
        self._size: tuple[int, int] = (0, 0)  # (dot_w, dot_h) the viewport is built for
        # Ask the session to scrub the panel's right edge on the next paint (see
        # :meth:`consume_edge_scrub`). Seeded ``True`` so the first braille frame's edge is
        # cleaned even before the first pan.
        self._needs_scrub = True
        # Decoded tiles keyed by (z, x, y); a stored ``None`` means "fetched, empty/absent".
        self._tiles: dict[tuple[int, int, int], Optional[list[Layer]]] = {}
        self._pending: set[tuple[int, int, int]] = set()

    # --- rendering -----------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Key hints plus a live tile-loading indicator."""
        base = (
            "wasd/↑↓←→ pan (⇧ fine) · +/-/PgUp/PgDn zoom · r reset · Esc back"
        )
        if self._pending:
            return f"{base} · [muted]loading {len(self._pending)} tiles…[/muted]"
        if not self._source.available:
            return f"{base} · [warn]offline — no basemap[/warn]"
        return base

    def consume_edge_scrub(self) -> int:
        """Right-edge columns the session should force-repaint on the next paint (0 = none).

        A braille glyph the terminal font lacks is substituted by a *double-width* fallback,
        which shoves the row and smears the panel's right padding and border. The map body
        itself redraws wholesale as it pans, so those cells self-heal — but the static edge
        does not change frame-to-frame, so prompt_toolkit's differential paint never rewrites
        it and the smear lingers there. After a move we ask the session to force just those
        two columns (right padding + border) to repaint, scrubbing the smear without the
        whole-frame flicker a full repaint would cause.
        """
        if not self._needs_scrub:
            return 0
        self._needs_scrub = False
        return 2  # the panel's right padding cell and its right border cell

    def render_body(self, width: int) -> list[str]:
        """Build (or resize) the viewport, ensure its tiles, and render the frame."""
        _, cell_h = self._session.base_body_size()
        cell_w = width
        dot_w, dot_h = cell_w * 2, cell_h * 4

        if self._viewport is None:
            self._viewport = self._initial_viewport(dot_w, dot_h)
            self._size = (dot_w, dot_h)
        elif self._size != (dot_w, dot_h):
            self._viewport = self._viewport.resized(dot_w, dot_h)
            self._size = (dot_w, dot_h)

        self._ensure_tiles(self._viewport)
        self.title = self._title(self._viewport)
        self._persist()
        tiles = {t: self._tiles.get(t) for t in self._viewport.tiles(self._max_tile_zoom)}
        return render_map(self._viewport, tiles, self._markers)

    def _initial_viewport(self, dot_w: int, dot_h: int) -> Viewport:
        """Restore the saved view (clamped to sane bounds) or frame the nodes' dense core.

        With no saved view the default frames the half of the nodes nearest the median
        centre, so distant outliers don't zoom the whole mesh out to a useless scale.
        """
        if self._saved_view is not None:
            lat, lon, zoom = self._saved_view
            z = max(2, min(int(zoom), self._max_tile_zoom + _OVERZOOM))
            return Viewport(clamp_lat(lat), lon, z, dot_w, dot_h)
        return Viewport.fit(
            [(m.lat, m.lon) for m in self._markers],
            dot_w,
            dot_h,
            max_zoom=self._max_tile_zoom,
            fraction=self._view_fraction,
        )

    def _persist(self) -> None:
        """Hand the current centre/zoom to ``on_view_change`` if it changed since last time."""
        vp = self._viewport
        if vp is None or self._on_view_change is None:
            return
        view = (vp.center_lat, vp.center_lon, vp.zoom)
        if view == self._last_saved:
            return
        self._last_saved = view
        self._on_view_change(vp)

    def _title(self, vp: Viewport) -> str:
        """A compact status title: zoom, node count, and scale (metres per dot)."""
        # Ground metres per braille dot at the view centre, for a rough sense of scale.
        m_per_dot = (
            2 * math.pi * EARTH_RADIUS_KM * 1000
            * math.cos(math.radians(vp.center_lat))
            / (256 * (2**vp.zoom))
        )
        scale = f"{m_per_dot * vp.dot_w:.0f} m across" if m_per_dot * vp.dot_w < 1000 else \
            f"{m_per_dot * vp.dot_w / 1000:.1f} km across"
        return f"mesh map · z{vp.zoom} · {len(self._markers)} nodes · {scale}"

    # --- tiles ---------------------------------------------------------------

    def _ensure_tiles(self, vp: Viewport) -> None:
        """Schedule background fetches for any visible tiles not yet loaded or pending."""
        if not self._source.available:  # offline (resolved at open time) — nodes only
            return
        for t in vp.tiles(self._max_tile_zoom):
            if t in self._tiles or t in self._pending:
                continue
            self._pending.add(t)
            try:
                asyncio.ensure_future(self._load(t))
            except RuntimeError:  # pragma: no cover - no running loop (non-interactive)
                self._pending.discard(t)

    async def _load(self, t: tuple[int, int, int]) -> None:
        """Fetch+decode one tile off the event loop, then repaint."""
        try:
            layers = await asyncio.to_thread(self._source.load_tile, *t)
        except Exception:  # noqa: BLE001 - a failed tile is just an absent one
            layers = None
        self._tiles[t] = layers
        self._pending.discard(t)
        self._session.invalidate()

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Pan, zoom, reset, or exit in response to a normalized key action."""
        vp = self._viewport
        if action == "escape":
            self.resolve(None)
            return
        if vp is None:
            return
        if action in _PAN_DIRS:
            self._pan(vp, action, fine=False)
        elif action.startswith("shift_") and action[len("shift_"):] in _PAN_DIRS:
            self._pan(vp, action[len("shift_"):], fine=True)
        elif action == "pageup":
            self._viewport = vp.zoomed(1, max_zoom=self._max_tile_zoom + _OVERZOOM)
        elif action == "pagedown":
            self._viewport = vp.zoomed(-1)
        elif action == "text":
            self._handle_key(data, vp)
        # Any handled key may have redrawn the body, so clean the right edge next paint.
        self._needs_scrub = True
        self._persist()

    def _pan(self, vp: Viewport, direction: str, *, fine: bool) -> None:
        """Pan by one coarse step, or — when ``fine`` — a single character cell.

        A character cell is 2 braille dots wide and 4 tall, so the fine step is that many
        dots expressed as a fraction of the current view.
        """
        dx, dy = _PAN_DIRS[direction]
        if fine:
            self._viewport = vp.panned(dx * 2 / vp.dot_w, dy * 4 / vp.dot_h)
        else:
            self._viewport = vp.panned(dx * _PAN_STEP, dy * _PAN_STEP)

    def _handle_key(self, key: str, vp: Viewport) -> None:
        """Handle a printable-key action (pan/zoom/reset/quit).

        An uppercase pan letter (Shift held) pans by a single cell for fine positioning.
        """
        low = key.lower()
        if low in _PAN_KEYS:
            self._pan(vp, _PAN_KEYS[low], fine=key.isupper())
        elif low in ("=", "+"):
            self._viewport = vp.zoomed(1, max_zoom=self._max_tile_zoom + _OVERZOOM)
        elif low in ("-", "_"):
            self._viewport = vp.zoomed(-1)
        elif low == "r":
            self._viewport = Viewport.fit(
                [(m.lat, m.lon) for m in self._markers],
                vp.dot_w,
                vp.dot_h,
                max_zoom=self._max_tile_zoom,
                fraction=self._view_fraction,
            )
        elif low == "q":
            self.resolve(None)


def basemap_source(ctx: "AppContext") -> BasemapSource:
    """Build the vector-tile source backed by the app's on-disk tile cache."""
    return BasemapSource(ctx.settings.config_dir / "tilecache")


async def open_map(
    ctx: "AppContext",
    markers: list[MapMarker],
    *,
    fraction: float = DEFAULT_VIEW_FRACTION,
) -> None:
    """Open the interactive full-screen map over ``markers`` and run until dismissed.

    Warms the tile source off the event loop (so the first paint doesn't block on the
    network), then pushes the :class:`MapScreen` and awaits its dismissal.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        markers: The located mesh nodes to plot (non-empty).
        fraction: Fraction of the nodes the default view frames (see :class:`MapScreen`).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive map is only available in the menu")
    session = ctx.ui.session
    source = basemap_source(ctx)
    # Resolve the tile template/zoom in a worker thread so the UI thread never blocks.
    max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
    screen = MapScreen(
        session,
        markers,
        source,
        max_zoom,
        saved_view=ctx.repo.get_map_view(),
        on_view_change=lambda vp: ctx.repo.set_map_view(
            vp.center_lat, vp.center_lon, vp.zoom
        ),
        view_fraction=fraction,
    )
    try:
        await session.run_screen(screen)
    finally:
        # The map's braille may have smeared the terminal via double-width fallback glyphs
        # that prompt_toolkit's diff can't see; force one full repaint so the menu drawn
        # underneath starts from a clean slate rather than inheriting that garbage.
        session.request_full_repaint()
