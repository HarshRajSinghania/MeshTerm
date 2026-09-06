"""A small, static, non-interactive map region embeddable inside any screen.

The full-screen :class:`~meshterm.ui.map_screen.MapScreen` is a whole layer of its own —
it pans, zooms, finds nodes, and persists its view. Some screens only want a *glance*: a
few rows of basemap fixed on one place, with the mesh's markers on it, no controls. The
Node detail screen's "Location" preview is the first such caller — it shows where a node
sits without leaving for the big map (which its *Open full map* action still reaches).

This is the map's tile plumbing distilled to that job: a fixed centre and zoom, its own
little tile cache, background fetches off the event loop (so the host screen never blocks
on the network), and a synchronous :meth:`render` that draws whatever tiles have landed —
refining in place as more arrive, exactly like the big map. With no network and no cached
tiles the basemap is simply absent and the markers plot on a blank grid, so the preview is
always *something*. It owns no keys and resolves nothing; the screen that embeds it just
calls :meth:`render` from its own ``render_body`` and forwards nothing back.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

from ..core.geo import Viewport, clamp_lat
from ..core.mvt import Layer
from .map_render import MapMarker, render_map

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..services.basemap import BasemapSource

#: How far past the tile source's max zoom a preview may sit — the lower-zoom tiles are
#: magnified to fill it. Matches the big map's overzoom so a close preview still has a
#: (blurred) basemap rather than blank tiles.
_OVERZOOM = 2

#: How long a tile the source gave no answer about is left alone before the preview asks
#: for it again — the big map's cooldown, for the same reason (see
#: :meth:`meshterm.ui.map_screen.MapScreen._load`).
_TILE_RETRY_SECONDS = 20.0


def _loop_running() -> bool:
    """Whether there is an event loop to hand background work to.

    Asked *before* building a coroutine, not after: ``ensure_future`` without a loop
    raises, but by then the coroutine exists and never gets awaited, which Python reports
    as a resource warning on a path that is otherwise perfectly correct (a screen rendered
    in a test, or a one-shot CLI draw).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class MiniMap:
    """A fixed, non-interactive slippy-map region drawn into a host screen's body.

    Build one with the resolved tile source (as :func:`~meshterm.ui.map_screen.open_map`
    resolves it) plus the centre, zoom, and markers to plot, then call :meth:`render`
    from the host's ``render_body`` with the cell width and row height the region should
    take. Tiles fetch in the background and the region redraws as they arrive; nothing
    here transmits over the radio.
    """

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, untyped to avoid an import cycle
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        center_lat: float,
        center_lon: float,
        zoom: int,
        markers: list[MapMarker],
    ) -> None:
        """Create the preview over a fixed view.

        Args:
            session: The running TUI session — used only to repaint the host screen when
                a background tile lands (:meth:`_load`).
            source: The vector-tile source, already resolved/warmed by the caller.
            max_tile_zoom: The source's max zoom, captured off the event loop at open time
                (reading it can touch the network, so the host resolves it once up front).
            center_lat: Latitude the preview centres on.
            center_lon: Longitude the preview centres on.
            zoom: Display zoom; clamped to a sane range against the source's max.
            markers: The located mesh nodes to overlay (may be empty).
        """
        self._session = session
        self._source = source
        self._max_tile_zoom = max_tile_zoom
        self._center_lat = clamp_lat(center_lat)
        self._center_lon = center_lon
        self._zoom = max(2, min(int(zoom), max_tile_zoom + _OVERZOOM))
        self._markers = markers
        # Decoded tiles keyed by (z, x, y); a stored ``None`` is the source's own word that
        # there is no tile there. Silence is not that answer and is not stored here.
        self._tiles: dict[tuple[int, int, int], list[Layer] | None] = {}
        self._pending: set[tuple[int, int, int]] = set()
        # Tiles we got no answer about, and when each may be asked for again.
        self._unanswered: dict[tuple[int, int, int], float] = {}

    @property
    def has_basemap(self) -> bool:
        """Whether the source has resolved a basemap to draw from — not yet is not never.

        The preview keeps asking either way (see :meth:`_ensure_tiles`); this is for a
        host that wants to caption the wait.
        """
        return self._source.available

    @property
    def pending(self) -> int:
        """How many visible tiles are still in flight — for a host's loading note."""
        return len(self._pending)

    def render(self, width: int, rows: int) -> list[str]:
        """Render the preview at ``width`` cells by ``rows`` rows, scheduling tile loads.

        Builds a :class:`~meshterm.core.geo.Viewport` sized to this region (each cell is
        two braille dots wide, each row four tall) fixed on the preview's centre and zoom,
        kicks off background fetches for any not-yet-loaded tiles, and draws the frame from
        whatever has landed. Called every paint; the viewport is cheap to rebuild, so a
        resize just re-sizes it.

        Args:
            width: Region width in character cells.
            rows: Region height in character cells.

        Returns:
            One ANSI string per row (exactly ``rows`` of them, as the canvas fills its box).
        """
        viewport = Viewport(
            self._center_lat, self._center_lon, self._zoom, max(2, width) * 2, max(1, rows) * 4
        )
        self._ensure_tiles(viewport)
        tiles = {t: self._tiles.get(t) for t in viewport.tiles(self._max_tile_zoom)}
        return render_map(viewport, tiles, self._markers)

    def _ensure_tiles(self, viewport: Viewport) -> None:
        """Schedule background fetches for any visible tile we don't have and aren't owed.

        Not gated on :attr:`~meshterm.services.basemap.BasemapSource.available`, and a
        tile the source gave no answer about is asked for again once its cooldown runs
        out — the big map's rules, and for its reasons (see
        :meth:`meshterm.ui.map_screen.MapScreen._ensure_tiles`).
        """
        if not _loop_running():  # nothing to fetch onto — draw whatever is already here
            return
        if self._unanswered:  # the silences that have served their time
            now = monotonic()
            self._unanswered = {t: at for t, at in self._unanswered.items() if at > now}
        for t in viewport.tiles(self._max_tile_zoom):
            if t in self._tiles or t in self._pending or t in self._unanswered:
                continue
            self._pending.add(t)
            asyncio.ensure_future(self._load(t))

    async def _load(self, t: tuple[int, int, int]) -> None:
        """Fetch+decode one tile off the event loop, then repaint the host screen."""
        try:
            layers = await asyncio.to_thread(self._source.load_tile, *t)
        except Exception:  # noqa: BLE001 - a failed tile is just an absent one
            layers = None
        self._pending.discard(t)
        if layers is not None or self._source.answered_empty(*t):
            self._tiles[t] = layers  # an answer, settled for the session
        else:
            self._unanswered[t] = monotonic() + _TILE_RETRY_SECONDS  # silence — ask again
        self._session.invalidate()
