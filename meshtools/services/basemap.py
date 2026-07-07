"""Fetch and cache OpenStreetMap vector tiles for the terminal map.

This is the only networked part of the map. It resolves a tile source's (versioned) tile-URL
template from its TileJSON, fetches ``.pbf`` vector tiles over HTTPS, and caches them on disk
so panning back over ground you've seen is instant and later sessions work offline. Decoding
is delegated to the pure :mod:`meshtools.core.mvt`.

The default source is **OpenFreeMap** (openfreemap.org) — full-planet OpenStreetMap vector
tiles, free and requiring no API key. Everything here is best-effort: with no network and no
cached tiles the loader simply returns ``None`` and the map falls back to plotting nodes on a
blank grid, never raising into the UI.

Fetches are blocking (stdlib ``urllib``); callers on an event loop should run
:meth:`BasemapSource.load_tile` via ``asyncio.to_thread`` so the UI stays responsive.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Optional

from ..core.mvt import Layer, decode_tile

#: OpenFreeMap planet TileJSON — its ``tiles`` array holds the current versioned template.
DEFAULT_TILEJSON_URL = "https://tiles.openfreemap.org/planet"

#: Sent on every request; identifies the client per common tile-usage etiquette.
_USER_AGENT = "MeshTools/0.1 (+https://github.com/; mesh node map)"

#: Fallback max tile zoom if the TileJSON doesn't declare one (OpenFreeMap serves 14).
_DEFAULT_MAX_ZOOM = 14

_log = logging.getLogger(__name__)


class BasemapSource:
    """A cached OpenStreetMap vector-tile source.

    Attributes:
        cache_dir: Directory under which tiles and the resolved TileJSON are cached.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        tilejson_url: str = DEFAULT_TILEJSON_URL,
        timeout: float = 12.0,
    ) -> None:
        """Open a tile source backed by an on-disk cache.

        Args:
            cache_dir: Where fetched tiles and TileJSON metadata are stored.
            tilejson_url: URL of the source's TileJSON document.
            timeout: Per-request network timeout in seconds.
        """
        self.cache_dir = cache_dir
        self._tilejson_url = tilejson_url
        self._timeout = timeout
        self._template: Optional[str] = None
        self._max_zoom: Optional[int] = None
        self._resolved = False  # whether we've tried (success or offline) this session

    # -- metadata ---------------------------------------------------------------

    def _tilejson_path(self) -> Path:
        return self.cache_dir / "tilejson.json"

    def _resolve(self) -> None:
        """Resolve and cache the tile-URL template and max zoom (best-effort, once)."""
        if self._resolved:
            return
        self._resolved = True
        # Prefer a freshly fetched TileJSON (the template is versioned and rotates), but fall
        # back to a previously cached copy so a session started offline can still use disk
        # tiles and even re-fetch if the template is still valid.
        data = self._http_get(self._tilejson_url)
        if data is not None:
            try:
                self._tilejson_path().parent.mkdir(parents=True, exist_ok=True)
                self._tilejson_path().write_bytes(data)
            except OSError:  # pragma: no cover - cache write failure is non-fatal
                pass
        if data is None and self._tilejson_path().exists():
            try:
                data = self._tilejson_path().read_bytes()
            except OSError:  # pragma: no cover
                data = None
        if data is None:
            return
        try:
            doc = json.loads(data)
            tiles = doc.get("tiles") or []
            if tiles:
                self._template = str(tiles[0])
            self._max_zoom = int(doc.get("maxzoom", _DEFAULT_MAX_ZOOM))
        except (ValueError, TypeError):  # pragma: no cover - malformed TileJSON
            pass

    @property
    def max_zoom(self) -> int:
        """The highest zoom the source serves (resolved lazily; sensible default offline)."""
        self._resolve()
        return self._max_zoom if self._max_zoom is not None else _DEFAULT_MAX_ZOOM

    @property
    def available(self) -> bool:
        """Whether a tile-URL template is known (network reachable, or one was cached)."""
        self._resolve()
        return self._template is not None

    # -- tiles ------------------------------------------------------------------

    def _tile_path(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / "tiles" / str(z) / str(x) / f"{y}.pbf"

    def load_tile(self, z: int, x: int, y: int) -> Optional[list[Layer]]:
        """Return the decoded layers for a tile, from cache or the network.

        Args:
            z: Tile zoom.
            x: Tile x index.
            y: Tile y index.

        Returns:
            The decoded layers, or ``None`` if the tile is unavailable (offline and
            uncached, or a genuinely empty/missing tile).
        """
        path = self._tile_path(z, x, y)
        raw = self._read_cached(path)
        if raw is None:
            raw = self._fetch_tile(z, x, y)
            if raw is not None:
                self._write_cached(path, raw)
        if not raw:
            return None
        try:
            return decode_tile(raw)
        except Exception as exc:  # noqa: BLE001 - a corrupt tile must not crash the map
            _log.debug("failed to decode tile %s/%s/%s: %s", z, x, y, exc)
            return None

    def _fetch_tile(self, z: int, x: int, y: int) -> Optional[bytes]:
        """Fetch a tile's raw bytes from the network, or ``None`` if unavailable."""
        self._resolve()
        if self._template is None:
            return None
        url = (
            self._template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        )
        data = self._http_get(url)
        # A 404/empty response is a legitimately empty tile; cache an empty marker so we
        # don't re-request it every repaint.
        return data if data is not None else b""

    @staticmethod
    def _read_cached(path: Path) -> Optional[bytes]:
        try:
            return path.read_bytes() if path.exists() else None
        except OSError:  # pragma: no cover
            return None

    @staticmethod
    def _write_cached(path: Path, raw: bytes) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except OSError:  # pragma: no cover - cache write failure is non-fatal
            pass

    def _http_get(self, url: str) -> Optional[bytes]:
        """GET a URL, returning the body, or ``None`` on any network/HTTP failure."""
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - offline / 404 / timeout are all "no tile"
            _log.debug("tile fetch failed for %s: %s", url, exc)
            return None
