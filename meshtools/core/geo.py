"""Pure geographic helpers for the terminal map: Web Mercator, tiles, and viewports.

This module is deliberately I/O-free and rendering-free so it can be unit-tested without a
radio, a terminal, or the network. It turns latitude/longitude into the Web Mercator "world
pixel" space that slippy-map vector tiles live in, models the on-screen :class:`Viewport`
(which braille dot maps to which coordinate), and measures great-circle distances.

The map renders one braille **dot** per Web Mercator pixel at the viewport's zoom, so a
viewport of ``dot_w`` × ``dot_h`` dots shows exactly that many mercator pixels — panning and
zooming are then just moving and scaling this window over the world. Braille sub-cells are
about square in a monospace font (2 dots wide, 4 tall, in a cell roughly twice as tall as
wide), so mercator pixels land on screen without obvious distortion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Mean radius of the Earth in kilometres, used for haversine distances.
EARTH_RADIUS_KM = 6371.0088

#: Web Mercator tile edge in pixels at its own zoom — the slippy-map convention. The world
#: is ``TILE_PX * 2**zoom`` pixels square at a given zoom.
TILE_PX = 256


@dataclass(frozen=True, slots=True)
class BBox:
    """An axis-aligned latitude/longitude bounding box (decimal degrees)."""

    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    @classmethod
    def around(cls, points: list[tuple[float, float]]) -> "BBox":
        """Return the tightest box containing every ``(lat, lon)`` point.

        Args:
            points: One or more latitude/longitude pairs (must be non-empty).

        Returns:
            The enclosing bounding box.

        Raises:
            ValueError: If ``points`` is empty.
        """
        if not points:
            raise ValueError("cannot build a bounding box from no points")
        lats = [lat for lat, _ in points]
        lons = [lon for _, lon in points]
        return cls(min(lats), min(lons), max(lats), max(lons))

    @property
    def center(self) -> tuple[float, float]:
        """The box's centre as a ``(lat, lon)`` pair."""
        return ((self.min_lat + self.max_lat) / 2, (self.min_lon + self.max_lon) / 2)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points in kilometres.

    Args:
        lat1: First point's latitude (degrees).
        lon1: First point's longitude (degrees).
        lat2: Second point's latitude (degrees).
        lon2: Second point's longitude (degrees).

    Returns:
        The distance along the Earth's surface in kilometres.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def clamp_lat(lat: float) -> float:
    """Clamp latitude to the Web Mercator limit (~±85.051°) where the projection is finite."""
    return max(-85.05112878, min(85.05112878, lat))


def lonlat_to_world(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """Project ``(lat, lon)`` to Web Mercator world pixels at ``zoom``.

    Args:
        lat: Latitude in degrees (clamped to the mercator limit).
        lon: Longitude in degrees.
        zoom: Zoom level (may be fractional).

    Returns:
        ``(x, y)`` in world pixels, where the world spans ``TILE_PX * 2**zoom`` on each axis
        and ``y`` grows southward (north is up / smaller ``y``).
    """
    scale = TILE_PX * (2.0**zoom)
    x = (lon + 180.0) / 360.0 * scale
    s = math.sin(math.radians(clamp_lat(lat)))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def world_to_lonlat(x: float, y: float, zoom: float) -> tuple[float, float]:
    """Invert :func:`lonlat_to_world`: world pixels at ``zoom`` back to ``(lat, lon)``.

    Args:
        x: World-pixel x.
        y: World-pixel y.
        zoom: The zoom the pixels were computed at.

    Returns:
        The ``(lat, lon)`` in degrees.
    """
    scale = TILE_PX * (2.0**zoom)
    lon = x / scale * 360.0 - 180.0
    n = math.pi - 2 * math.pi * y / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lon


@dataclass(frozen=True, slots=True)
class Viewport:
    """The on-screen window over the world: what each braille dot maps to.

    One dot equals one Web Mercator pixel at :attr:`zoom`, so the visible area is exactly
    ``dot_w`` × ``dot_h`` mercator pixels centred on ``(center_lat, center_lon)``. Panning and
    zooming return new viewports; nothing here mutates.

    Attributes:
        center_lat: Latitude at the centre of the view.
        center_lon: Longitude at the centre of the view.
        zoom: Display zoom (integer here; may exceed the tile source's max, in which case
            lower-zoom tiles are magnified — see :meth:`tiles` / :meth:`feature_to_dot`).
        dot_w: Viewport width in braille dots.
        dot_h: Viewport height in braille dots.
    """

    center_lat: float
    center_lon: float
    zoom: int
    dot_w: int
    dot_h: int

    @property
    def origin_world(self) -> tuple[float, float]:
        """Top-left corner of the view in world pixels at :attr:`zoom`."""
        cx, cy = lonlat_to_world(self.center_lat, self.center_lon, self.zoom)
        return cx - self.dot_w / 2, cy - self.dot_h / 2

    def lonlat_to_dot(self, lat: float, lon: float) -> tuple[float, float]:
        """Project a coordinate to a (possibly off-screen) dot position in the view."""
        ox, oy = self.origin_world
        wx, wy = lonlat_to_world(lat, lon, self.zoom)
        return wx - ox, wy - oy

    def tile_zoom(self, max_tile_zoom: int) -> int:
        """The tile zoom to fetch: the display zoom, capped at the source's max."""
        return max(0, min(self.zoom, max_tile_zoom))

    def tiles(self, max_tile_zoom: int) -> list[tuple[int, int, int]]:
        """Return the ``(z, x, y)`` tiles covering the view at the fetchable tile zoom.

        When the display zoom exceeds ``max_tile_zoom`` the lower-zoom tiles that cover the
        same ground are returned (the renderer magnifies them), so zooming in past the
        source's limit still works.

        Args:
            max_tile_zoom: The highest zoom the tile source actually serves.

        Returns:
            Tile coordinates, clamped to the valid range, de-duplicated in row-major order.
        """
        tz = self.tile_zoom(max_tile_zoom)
        scale = 2.0 ** (self.zoom - tz)  # display px per tile-zoom px
        ox, oy = self.origin_world
        n = 2**tz
        # Visible rect in tile-zoom world pixels, then in tile indices.
        tx0 = int((ox / scale) // TILE_PX)
        tx1 = int(((ox + self.dot_w) / scale) // TILE_PX)
        ty0 = int((oy / scale) // TILE_PX)
        ty1 = int(((oy + self.dot_h) / scale) // TILE_PX)
        out: list[tuple[int, int, int]] = []
        for ty in range(ty0, ty1 + 1):
            if not 0 <= ty < n:
                continue
            for tx in range(tx0, tx1 + 1):
                out.append((tz, tx % n, ty))  # wrap x around the antimeridian
        return out

    def feature_to_dot(
        self, tile_x: int, tile_y: int, tile_zoom: int, extent: int, lx: float, ly: float
    ) -> tuple[float, float]:
        """Project a tile-local point to a dot position in this viewport.

        Args:
            tile_x: The tile's x index at ``tile_zoom``.
            tile_y: The tile's y index at ``tile_zoom``.
            tile_zoom: The zoom the tile was fetched at.
            extent: The tile's internal coordinate extent (e.g. 4096).
            lx: Local x within the tile, ``0..extent``.
            ly: Local y within the tile, ``0..extent``.

        Returns:
            The ``(x, y)`` dot position (may be outside the canvas; the caller clips).
        """
        scale = 2.0 ** (self.zoom - tile_zoom)
        wx = (tile_x + lx / extent) * TILE_PX * scale
        wy = (tile_y + ly / extent) * TILE_PX * scale
        ox, oy = self.origin_world
        return wx - ox, wy - oy

    def panned(self, frac_x: float, frac_y: float) -> "Viewport":
        """Return a viewport shifted by a fraction of its own width/height.

        Args:
            frac_x: Eastward shift as a fraction of the view width (negative = west).
            frac_y: Southward shift as a fraction of the view height (negative = north).

        Returns:
            A new :class:`Viewport` at the shifted centre, same zoom and size.
        """
        cx, cy = lonlat_to_world(self.center_lat, self.center_lon, self.zoom)
        cx += frac_x * self.dot_w
        cy += frac_y * self.dot_h
        lat, lon = world_to_lonlat(cx, cy, self.zoom)
        return Viewport(clamp_lat(lat), lon, self.zoom, self.dot_w, self.dot_h)

    def zoomed(self, delta: int, *, min_zoom: int = 2, max_zoom: int = 19) -> "Viewport":
        """Return a viewport zoomed by ``delta`` levels about the same centre (clamped)."""
        z = max(min_zoom, min(max_zoom, self.zoom + delta))
        return Viewport(self.center_lat, self.center_lon, z, self.dot_w, self.dot_h)

    def resized(self, dot_w: int, dot_h: int) -> "Viewport":
        """Return the same view centred as before but at a new canvas size."""
        return Viewport(self.center_lat, self.center_lon, self.zoom, dot_w, dot_h)

    @classmethod
    def fit(
        cls,
        points: list[tuple[float, float]],
        dot_w: int,
        dot_h: int,
        *,
        pad: float = 0.18,
        min_zoom: int = 2,
        max_zoom: int = 16,
        default_zoom: int = 14,
    ) -> "Viewport":
        """Build a viewport framing ``points`` — centred on them at the tightest fitting zoom.

        Args:
            points: Latitude/longitude pairs to frame (may be empty).
            dot_w: Canvas width in dots.
            dot_h: Canvas height in dots.
            pad: Fraction of the canvas kept as margin around the points.
            min_zoom: Lowest zoom to consider.
            max_zoom: Highest zoom to consider.
            default_zoom: Zoom used when the points don't constrain it (0 or 1 point).

        Returns:
            A :class:`Viewport` centred on the points at a zoom where they fit with margin.
            Empty input centres on the world at ``min_zoom``.
        """
        if not points:
            return cls(0.0, 0.0, min_zoom, dot_w, dot_h)
        box = BBox.around(points)
        center_lat, center_lon = box.center
        if len(points) == 1 or (box.min_lat == box.max_lat and box.min_lon == box.max_lon):
            return cls(center_lat, center_lon, default_zoom, dot_w, dot_h)
        avail_w = dot_w * (1 - pad)
        avail_h = dot_h * (1 - pad)
        chosen = min_zoom
        for z in range(max_zoom, min_zoom - 1, -1):
            x0, y0 = lonlat_to_world(box.max_lat, box.min_lon, z)  # NW corner
            x1, y1 = lonlat_to_world(box.min_lat, box.max_lon, z)  # SE corner
            if abs(x1 - x0) <= avail_w and abs(y1 - y0) <= avail_h:
                chosen = z
                break
        return cls(center_lat, center_lon, chosen, dot_w, dot_h)
