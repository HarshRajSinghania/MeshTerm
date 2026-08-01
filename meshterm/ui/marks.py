"""The shared mark constants: node-type glyphs, their colours, and path sentinels.

The app's spatial surfaces — the map, the path graph, the braille rasters, the node
rows — all pin nodes with the same marks in the same hues, and several of them need the
constants *without* the machinery around them. Before this module, ``ui.widgets`` pulled
``map_render``, ``mapcanvas`` and ``pathgraph`` onto the boot path for a handful of
tuples and type aliases (~60 ms of import on the PicoCalc); now the constants live here,
dependency-free, and the heavy modules import *from* the constants instead of hosting
them. ``map_render``/``mapcanvas``/``pathgraph`` re-export their old names, so existing
importers of those modules are untouched.
"""

from __future__ import annotations

from typing import Callable, Optional

#: An ``(r, g, b)`` colour triple, 0–255 per channel (the raster/canvas colour type).
RGB = tuple[int, int, int]


def parse_hex(color: str) -> RGB:
    """Convert ``"#rrggbb"`` (or ``"rrggbb"``) to an ``(r, g, b)`` tuple."""
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


# Marker palette, shared across the whole app. The basemap is blue (water), green (parks)
# and warm amber (roads), so markers use the hues a map never contains — pink/violet — to
# stand out against it. Ordinary nodes are the loudest (bright pink); repeaters recede a
# step (a calmer violet); our own node stays the fixed yellow star; a node heard of but
# never identified is a muted grey ring. Every glyph is in the PicoCalc console font.
SELF_MARK = ("★", "#facc15")
REPEATER_MARK = ("▲", "#a78bfa")
NODE_MARK = ("●", "#f472b6")
UNKNOWN_MARK = ("○", "#94a3b8")

#: Sentinels naming a path's endpoints on the route graph. ``\x00`` never appears in a
#: node hash, so they can share the node-id namespace without colliding with one.
SRC_NODE = "\x00src"
DST_NODE = "\x00dst"

#: Maps a node id to its graph marker: a ``(glyph, colour)`` pair.
GlyphOf = Callable[[str], tuple[str, str]]

#: A node's graph label, or ``None``/``""`` to leave the marker bare.
LabelOf = Callable[[str], Optional[str]]

#: The colour a node's label is drawn in (usually the node's name hue).
LabelRgbOf = Callable[[str], RGB]
