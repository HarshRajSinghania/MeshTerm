"""The interactive full-screen map: a pannable, zoomable slippy map in the terminal.

This drives the braille street map inside the TUI. It owns a :class:`~meshterm.core.geo.
Viewport` over the mesh's nodes, fetches the vector tiles covering it in the background (so
the UI never blocks on the network), and redraws via :func:`~meshterm.ui.map_render.
render_map`. Keys:

* the **arrow keys** pan; holding **Shift** pans by a single character cell for fine
  positioning,
* ``PgUp`` / ``PgDn`` zoom in / out,
* ``Home`` recenters and refits to the dense core of the nodes (the same default view the
  map opens on),
* **typing finds nodes**: every letter key feeds a live name filter — matching nodes keep
  bright labels while the rest dim to context, ``Enter`` frames the matches, ``Backspace``
  edits, and ``Esc`` clears the filter (a second ``Esc`` leaves the map). This is why no
  letters are bound to actions here,
* ``Esc`` leaves the map.

With no network (and no cached tiles) the basemap is simply absent and nodes are plotted on a
blank grid — the map still works, it just has no streets.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Callable, Optional

from ..core.geo import DEFAULT_VIEW_FRACTION, EARTH_RADIUS_KM, Viewport, clamp_lat
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

#: How far past the tile source's max zoom the display may go (lower tiles are magnified).
_OVERZOOM = 2

#: The zoom an Enter-to-frame homes in at when the matches set no extent of their own — a
#: single node (or several at one spot) has nothing to frame, so Enter zooms to this
#: street-level closeness rather than the fit's neutral default. Capped at the tile
#: source's max so it never over-zooms onto blank tiles.
_FIND_ZOOM = 16


class MapScreen(Screen):
    """A full-screen, keyboard-driven map of the mesh's located nodes over an OSM basemap."""

    floating = False

    #: Whether printable keys feed the find-as-you-type node filter. The location
    #: picker turns this off: there, typing has no job and a silent filter would
    #: mysteriously dim the context markers.
    find_enabled = True

    @property
    def fkey_lane(self):
        """The PicoCalc lane: reset on F1, zoom on F4/F5 — nothing that doesn't act here.

        The map is the one screen that repurposes the nav actions: PgUp/PgDn zoom and
        Home refits the view, while ``end`` is bound to nothing at all. The shared lane
        would label those keys as paging and offer a fifth that does nothing, so the map
        names its own verbs and leaves F2 unassigned. All three are always live — a map
        can always zoom or refit — so no slot dims.
        """
        from .tui.fkeys import DEFAULT_LANE, FPair

        lane = list(DEFAULT_LANE)
        lane[0] = FPair("Reset", "home")
        lane[1] = None  # End does nothing on a map
        lane[3] = FPair("Zoom +", "pageup")
        lane[4] = FPair("Zoom -", "pagedown")
        return lane

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
        find: str = "",
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
            find: A find query to open with, exactly as if the user had typed it — the
                matching nodes light and the rest dim (the node-detail page seeds its node's
                name here). Editable and Esc-clearable like any typed find; ``""`` = off.
        """
        super().__init__()
        self.title = "Map"
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
        #: The live find-as-you-type node filter ("" = off). Every printable key lands
        #: here — the map binds no letters to actions — and rendering highlights the
        #: matching markers while dimming the rest. A caller may seed it (``find``), which
        #: behaves exactly like a query the user had already typed.
        self._filter = find
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
        """Key hints — or the live find query — plus a tile-loading indicator.

        While a find filter is active the hints give way to the query itself with its
        editing keys, so the typed text is always visible somewhere fixed.
        """
        if self._filter:
            base = f"find: {self._filter}▏ · Enter frame · ⌫ erase · Esc clear"
        else:
            base = "↑↓←→ pan · ⇧ fine · PgUp/PgDn zoom · Home reset · type to find · Esc back"
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
        return render_map(self._viewport, tiles, self._markers, find=self._filter)

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

    def _matches(self) -> list[MapMarker]:
        """The markers the live find filter currently matches (all of them when off)."""
        needle = self._filter.strip().casefold()
        if not needle:
            return self._markers
        return [m for m in self._markers if needle in m.label.casefold()]

    def _title(self, vp: Viewport) -> str:
        """A compact status title: zoom, node count (find matches), and ground scale."""
        # Ground metres per braille dot at the view centre, for a rough sense of scale.
        m_per_dot = (
            2 * math.pi * EARTH_RADIUS_KM * 1000
            * math.cos(math.radians(vp.center_lat))
            / (256 * (2**vp.zoom))
        )
        scale = f"{m_per_dot * vp.dot_w:.0f} m across" if m_per_dot * vp.dot_w < 1000 else \
            f"{m_per_dot * vp.dot_w / 1000:.1f} km across"
        if self._filter:
            nodes = f"{len(self._matches())} of {len(self._markers)} match"
        else:
            nodes = f"{len(self._markers)} nodes"
        return f"Map · z{vp.zoom} · {nodes} · {scale}"

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
        """Pan, zoom, reset, edit the find filter, or exit.

        Every printable key feeds the find filter — nothing pans or zooms by letter, so
        typing a node name can never fling the view around. Esc peels one layer: an
        active filter first, the map itself only once the filter is clear.
        """
        vp = self._viewport
        if action == "escape":
            if self._filter:
                self._filter = ""
            else:
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
        elif action in ("home", "ctrl_home"):
            self._reset_view(vp)
        elif action == "text" and self.find_enabled:
            if not data.isspace() or self._filter:  # never begin the filter with a space
                self._filter += data
        elif action == "space" and self._filter:
            self._filter += " "  # node names carry spaces; only meaningful mid-query
        elif action == "backspace":
            self._filter = self._filter[:-1]
        elif action == "enter" and self._filter:
            self._frame_matches(vp)
        # Any handled key may have redrawn the body, so clean the right edge next paint.
        self._needs_scrub = True
        self._persist()

    def _reset_view(self, vp: Viewport) -> None:
        """Refit the view to the nodes' dense core (the map's opening frame)."""
        self._viewport = Viewport.fit(
            [(m.lat, m.lon) for m in self._markers],
            vp.dot_w,
            vp.dot_h,
            max_zoom=self._max_tile_zoom,
            fraction=self._view_fraction,
        )

    def _frame_matches(self, vp: Viewport) -> None:
        """Refit the view around the find filter's matches (Enter on an active find).

        All matches are framed (``fraction=1.0`` — the user asked for exactly these
        nodes, so no dense-core trimming). A single match — or several at one spot — has
        no extent to frame, so instead of the fit's neutral default the view homes in
        close on it (:data:`_FIND_ZOOM`, capped at the tile source's max so it never
        over-zooms onto blank tiles). No matches at all leaves the view alone.
        """
        matches = self._matches()
        if not matches:
            return
        self._viewport = Viewport.fit(
            [(m.lat, m.lon) for m in matches],
            vp.dot_w,
            vp.dot_h,
            max_zoom=self._max_tile_zoom,
            default_zoom=min(_FIND_ZOOM, self._max_tile_zoom),
            fraction=1.0,
        )

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


class LocationPickScreen(MapScreen):
    """The map, repurposed as a coordinate picker: pan the crosshair, Enter to choose.

    Used by the config editor to set the node's advertised location by *pointing at the
    map* instead of typing degrees. It is a :class:`MapScreen` with three changes: a
    crosshair marker rides the view centre (labelled with the live coordinates, so the
    user always sees exactly what they're about to pick), Enter resolves with the
    centre's ``(lat, lon)`` instead of framing find matches (find is off here — see
    :attr:`MapScreen.find_enabled`), and ``Home`` recentres on the *initial* location
    rather than refitting the node cloud. The surrounding mesh nodes are still drawn, so
    placing yourself relative to a known repeater is easy. Esc cancels (resolves CANCEL,
    surfaced as ``None`` by the caller).
    """

    find_enabled = False

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        markers: list[MapMarker],
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        initial: Optional[tuple[float, float]] = None,
        zoom: int = 13,
    ) -> None:
        """Create the picker.

        Args:
            session: The running TUI session (for size + repaint scheduling).
            markers: Located mesh nodes to draw for context (may be empty).
            source: The vector-tile source (already resolved/warmed).
            max_tile_zoom: The source's max zoom, captured off the event loop at open time.
            initial: The location to open centred on (the node's current position), or
                ``None`` to frame the mesh instead (falling back to a world view when no
                nodes are located either).
            zoom: The zoom to open at when ``initial`` is given.
        """
        saved = (initial[0], initial[1], zoom) if initial is not None else None
        super().__init__(session, markers, source, max_tile_zoom, saved_view=saved)
        self._initial = initial
        self._pick_zoom = zoom

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Key hints for picking, plus the live tile-loading indicator."""
        base = "↑↓←→ pan · ⇧ fine · PgUp/PgDn zoom · Enter set location · Esc cancel"
        if self._pending:
            return f"{base} · [muted]loading {len(self._pending)} tiles…[/muted]"
        if not self._source.available:
            return f"{base} · [warn]offline — no basemap[/warn]"
        return base

    def _initial_viewport(self, dot_w: int, dot_h: int) -> Viewport:
        """Open on the initial location, else frame the nodes, else a world view."""
        if self._saved_view is None and not self._markers:
            return Viewport(20.0, 0.0, 2, dot_w, dot_h)  # nothing to frame — the world
        return super()._initial_viewport(dot_w, dot_h)

    def render_body(self, width: int) -> list[str]:
        """Render the map with the crosshair marker pinned to the view centre.

        The crosshair is a transient marker appended for just this frame (never stored in
        :attr:`_markers`), drawn in the "self" style so it reads as *your* position-to-be
        and labelled with the live coordinates it would commit.
        """
        if self._viewport is None:
            # First paint: let the base class establish the viewport so the crosshair can
            # ride the centre from the very first frame (the extra render is one-off).
            super().render_body(width)
        real = self._markers
        vp = self._viewport
        if vp is not None:
            cross = MapMarker(
                label=f"⌖ {vp.center_lat:.5f}, {vp.center_lon:.5f}",
                lat=vp.center_lat,
                lon=vp.center_lon,
                is_self=True,
            )
            self._markers = real + [cross]
        try:
            return super().render_body(width)
        finally:
            self._markers = real

    def _title(self, vp: Viewport) -> str:
        """A live status title: the coordinates under the crosshair and the scale."""
        base = super()._title(vp)
        scale = base.rsplit("·", 1)[-1].strip()
        return f"Set location · {vp.center_lat:.5f}, {vp.center_lon:.5f} · z{vp.zoom} · {scale}"

    def handle(self, action: str, data: str = "") -> None:
        """Commit the centre on Enter; ``Home`` returns to the initial spot; else map keys."""
        if action == "enter":
            vp = self._viewport
            if vp is not None:
                self.resolve((vp.center_lat, vp.center_lon))
            return
        if action in ("home", "ctrl_home") and self._viewport is not None:
            # Reset returns to the *starting* view — the initial location when one was
            # given, else the node frame — rather than refitting a cloud that now includes
            # nowhere in particular. With neither, fall back to the world view.
            vp = self._viewport
            if self._initial is not None:
                lat, lon = self._initial
                self._viewport = Viewport(
                    clamp_lat(lat), lon, self._pick_zoom, vp.dot_w, vp.dot_h
                )
            elif not self._markers:
                self._viewport = Viewport(20.0, 0.0, 2, vp.dot_w, vp.dot_h)
            else:
                super().handle(action, data)
                return
            self._needs_scrub = True
            return
        super().handle(action, data)


async def pick_location(
    ctx: "AppContext", *, initial: Optional[tuple[float, float]] = None
) -> Optional[tuple[float, float]]:
    """Open the full-screen map as a coordinate picker; return ``(lat, lon)`` or ``None``.

    Gathers the mesh's located nodes for context (best-effort — an unreachable radio just
    means a barer map), warms the tile source off the event loop, then runs a
    :class:`LocationPickScreen` until the user commits a spot with Enter or backs out
    with Esc.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        initial: The location to open centred on (e.g. the node's current coordinates),
            or ``None`` to frame the mesh.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    import asyncio

    from ..tools.map import gather_markers
    from .surface import TuiUi
    from .tui.screen import CANCEL

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive map is only available in the menu")
    session = ctx.ui.session
    try:
        markers = await gather_markers(ctx)
    except Exception:  # noqa: BLE001 - context markers are a nicety, never a requirement
        markers = []
    source = basemap_source(ctx)
    max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
    screen = LocationPickScreen(
        session, markers, source, max_zoom, initial=initial
    )
    try:
        result = await session.run_screen(screen)
    finally:
        # Same clean-slate repaint as open_map: the braille may have smeared the terminal.
        session.request_full_repaint()
    return None if result is CANCEL or result is None else result


def basemap_source(ctx: "AppContext") -> BasemapSource:
    """The session's shared vector-tile source (see :attr:`AppContext.basemap_source`).

    Memoized on the context, so the one-off TileJSON resolve is paid once for the whole
    session instead of on every map open or Node-detail location preview.
    """
    return ctx.basemap_source


async def open_map(
    ctx: "AppContext",
    markers: list[MapMarker],
    *,
    focus: Optional[tuple[float, float]] = None,
    find: Optional[str] = None,
    fraction: float = DEFAULT_VIEW_FRACTION,
) -> None:
    """Open the interactive full-screen map over ``markers`` and run until dismissed.

    Warms the tile source off the event loop (so the first paint doesn't block on the
    network), then pushes the :class:`MapScreen` and awaits its dismissal.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        markers: The located mesh nodes to plot (non-empty).
        focus: A ``(lat, lon)`` to open centred on — the node you opened the map from,
            say — instead of the persisted "where you left the map" view. A focused open
            is a transient peek: it deliberately wires no ``on_view_change``, so panning
            around it never overwrites that saved view and the Map tool still reopens
            where the user last left it.
        find: A find query to open with — the focused node's name, so it lights among the
            rest exactly as if the user had typed it. ``None`` opens with the find off.
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
    if focus is not None:
        # Open on the focused node — matching the detail preview's centre and zoom
        # (``min(13, max_zoom)``) so the full map is visibly the same place, larger. No
        # ``on_view_change``: a focused peek must leave the persisted global view alone.
        lat, lon = focus
        screen = MapScreen(
            session,
            markers,
            source,
            max_zoom,
            saved_view=(clamp_lat(lat), lon, min(13, max_zoom)),
            view_fraction=fraction,
            find=find or "",
        )
    else:
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
