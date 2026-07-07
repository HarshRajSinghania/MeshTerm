"""A tiny, dependency-free Mapbox Vector Tile (MVT) decoder.

Vector tiles are Protocol-Buffer messages (spec: github.com/mapbox/vector-tile-spec). Rather
than pull in a protobuf runtime and a geometry library, this decodes the small subset the map
needs by hand: the protobuf wire format (varints, length-delimited fields) and the MVT
geometry command encoding (MoveTo/LineTo/ClosePath with zig-zag deltas). It is pure and
offline — the network fetch lives in :mod:`meshtools.services.basemap`.

The result is a list of :class:`Layer`, each with decoded :class:`Feature` geometry in
*tile-local* integer coordinates (``0..extent``) and a resolved ``tags`` dict, ready to be
projected to the screen by :class:`meshtools.core.geo.Viewport`.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from typing import Any, Optional

# MVT geometry types (Feature.type).
GEOM_POINT = 1
GEOM_LINE = 2
GEOM_POLYGON = 3

# Geometry command ids (low 3 bits of a command integer).
_CMD_MOVE_TO = 1
_CMD_LINE_TO = 2
_CMD_CLOSE = 7


class _Reader:
    """A minimal protobuf wire-format reader over a byte buffer."""

    __slots__ = ("b", "i")

    def __init__(self, b: bytes) -> None:
        self.b = b
        self.i = 0

    def eof(self) -> bool:
        return self.i >= len(self.b)

    def varint(self) -> int:
        """Read a base-128 varint."""
        result = shift = 0
        while True:
            byte = self.b[self.i]
            self.i += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def tag(self) -> tuple[int, int]:
        """Read a field tag, returning ``(field_number, wire_type)``."""
        key = self.varint()
        return key >> 3, key & 0x7

    def blob(self) -> bytes:
        """Read a length-delimited byte string."""
        n = self.varint()
        v = self.b[self.i : self.i + n]
        self.i += n
        return v

    def skip(self, wire_type: int) -> None:
        """Skip a field of the given wire type."""
        if wire_type == 0:
            self.varint()
        elif wire_type == 2:
            self.blob()
        elif wire_type == 5:
            self.i += 4
        elif wire_type == 1:
            self.i += 8
        else:  # pragma: no cover - groups are obsolete and never appear in MVT
            raise ValueError(f"unsupported wire type {wire_type}")


def _zigzag(n: int) -> int:
    """Decode a protobuf zig-zag encoded signed integer."""
    return (n >> 1) ^ -(n & 1)


@dataclass(slots=True)
class Feature:
    """One decoded vector-tile feature.

    Attributes:
        geom_type: :data:`GEOM_POINT`, :data:`GEOM_LINE`, or :data:`GEOM_POLYGON`.
        rings: The geometry as a list of parts, each a list of ``(x, y)`` integer points in
            tile-local coordinates (``0..extent``). Points have one part holding every point;
            lines/polygons have one part per line-string / ring.
        tags: Resolved attribute dict (e.g. ``{"class": "primary", "name": "Rue X"}``).
    """

    geom_type: int
    rings: list[list[tuple[int, int]]]
    tags: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        """Return a tag value, or ``default`` if absent."""
        return self.tags.get(key, default)

    @property
    def name(self) -> Optional[str]:
        """The feature's display name, preferring the local ``name`` then latin fallbacks."""
        for key in ("name", "name:latin", "name:en", "name_en", "name_int"):
            val = self.tags.get(key)
            if val:
                return str(val)
        return None


@dataclass(slots=True)
class Layer:
    """A named vector-tile layer and its decoded features.

    Attributes:
        name: Layer id (e.g. ``transportation``, ``water``, ``place``).
        extent: The tile's internal coordinate extent (typically 4096).
        features: The decoded features.
    """

    name: str
    extent: int
    features: list[Feature] = field(default_factory=list)


def _decode_geometry(cmds: list[int]) -> list[list[tuple[int, int]]]:
    """Decode an MVT geometry command stream into a list of point rings."""
    rings: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    x = y = 0
    i = 0
    n = len(cmds)
    while i < n:
        command = cmds[i]
        i += 1
        cmd_id = command & 0x7
        count = command >> 3
        if cmd_id == _CMD_MOVE_TO:
            for _ in range(count):
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                # Each MoveTo starts a new part (multipoint keeps them in one part below).
                if current:
                    rings.append(current)
                current = [(x, y)]
        elif cmd_id == _CMD_LINE_TO:
            for _ in range(count):
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                current.append((x, y))
        elif cmd_id == _CMD_CLOSE:
            if current:
                current.append(current[0])  # close the ring back to its start
    if current:
        rings.append(current)
    return rings


def _decode_value(buf: bytes) -> Any:
    """Decode an MVT attribute Value message into a Python scalar."""
    r = _Reader(buf)
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 1 and wire == 2:
            return r.blob().decode("utf-8", "ignore")  # string_value
        if field_no == 2 and wire == 5:  # float_value
            import struct

            v = struct.unpack("<f", r.b[r.i : r.i + 4])[0]
            r.i += 4
            return v
        if field_no == 3 and wire == 1:  # double_value
            import struct

            v = struct.unpack("<d", r.b[r.i : r.i + 8])[0]
            r.i += 8
            return v
        if field_no in (4, 5) and wire == 0:  # int_value / uint_value
            return r.varint()
        if field_no == 6 and wire == 0:  # sint_value
            return _zigzag(r.varint())
        if field_no == 7 and wire == 0:  # bool_value
            return bool(r.varint())
        r.skip(wire)
    return None


def _packed_uints(buf: bytes) -> list[int]:
    """Decode a packed repeated uint32 field into a list of ints."""
    r = _Reader(buf)
    out: list[int] = []
    while not r.eof():
        out.append(r.varint())
    return out


def _decode_feature(buf: bytes, keys: list[str], values: list[Any]) -> Optional[Feature]:
    """Decode a single Feature message, resolving its tags against the layer's key/value pools."""
    r = _Reader(buf)
    geom_type = 0
    tag_ints: list[int] = []
    geom_ints: list[int] = []
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 2 and wire == 2:
            tag_ints = _packed_uints(r.blob())
        elif field_no == 3 and wire == 0:
            geom_type = r.varint()
        elif field_no == 4 and wire == 2:
            geom_ints = _packed_uints(r.blob())
        else:
            r.skip(wire)
    if not geom_ints:
        return None
    tags: dict[str, Any] = {}
    for k, v in zip(tag_ints[0::2], tag_ints[1::2]):
        if 0 <= k < len(keys) and 0 <= v < len(values):
            tags[keys[k]] = values[v]
    return Feature(geom_type=geom_type, rings=_decode_geometry(geom_ints), tags=tags)


def _decode_layer(buf: bytes) -> Layer:
    """Decode a single Layer message and all of its features."""
    r = _Reader(buf)
    name = ""
    extent = 4096
    keys: list[str] = []
    values: list[Any] = []
    feature_blobs: list[bytes] = []
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 1 and wire == 2:
            name = r.blob().decode("utf-8", "ignore")
        elif field_no == 2 and wire == 2:
            feature_blobs.append(r.blob())
        elif field_no == 3 and wire == 2:
            keys.append(r.blob().decode("utf-8", "ignore"))
        elif field_no == 4 and wire == 2:
            values.append(_decode_value(r.blob()))
        elif field_no == 5 and wire == 0:
            extent = r.varint()
        else:
            r.skip(wire)
    layer = Layer(name=name, extent=extent)
    for blob in feature_blobs:
        feat = _decode_feature(blob, keys, values)
        if feat is not None:
            layer.features.append(feat)
    return layer


def decode_tile(data: bytes) -> list[Layer]:
    """Decode a vector tile (optionally gzip-compressed) into its layers.

    Args:
        data: Raw ``.pbf`` bytes, gzip-compressed or not.

    Returns:
        The decoded layers, in the order they appear in the tile. An empty or unparseable
        tile yields an empty list rather than raising.
    """
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    r = _Reader(data)
    layers: list[Layer] = []
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 3 and wire == 2:  # Tile.layers
            layers.append(_decode_layer(r.blob()))
        else:
            r.skip(wire)
    return layers
