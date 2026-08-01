"""The visual-language specimen: every mark, icon, colour and fold on one screen.

``meshterm specimen`` prints this through the platform's real machinery — the active
theme, the ``glyph()`` icon funnel, the render-boundary fold, the heat and SNR scales —
so what it shows *is* what the platform draws, not a mock-up of it. On the PicoCalc
console it doubles as the font/palette acceptance card (all nine P3 marks, the node
glyphs, braille, the 16 programmed slots); on a desktop terminal it shows the same
content in the regular platform's emoji-and-truecolor dress. Sized to the PicoCalc
floor: every line ≤ 53 cells, the whole card ≤ 24 rows.
"""

from __future__ import annotations

from rich.console import RenderableType
from rich.text import Text

from ..platforms import get_platform
from .packet_viewer import KIND_ICONS, PAYLOAD_ICONS
from .theme import _VT_SLOTS, fold_text, glyph, name_style, snr_style
from .widgets import _recency_style, channel_glyph

#: Demo identities for the name-colour row: (name, key, node type registered for it).
#: Keys are synthetic and only ever registered inside the one-shot specimen process.
_DEMO_NAMES = (("Alice", "a1b2", 1), ("YUL-Poly", "3d63", 2), ("Lounge", "cc10", 3))


def _palette_row() -> Text:
    """The 16 slots as filled blocks, dim bank then bright bank.

    On PicoCalc the blocks address the console's real slots (``color(N)``), so the row
    shows exactly what ``setvtrgb`` programmed; on the regular platform they carry the
    design's RGB values instead — a preview of the same palette in truecolor.
    """
    row = Text()
    for slot, _, hex_ in _VT_SLOTS:
        style = f"color({slot})" if not get_platform().truecolor else hex_
        row.append("██", style=style)
    return row


def _ages_row() -> Text:
    """The heat scale over its own anchors, each age drawn in its heat colour."""
    row = Text()
    for label, secs in (("now", 0), ("5m", 300), ("3h", 10800), ("2d", 172800), ("never", None)):
        if row.plain:
            row.append("  ")
        row.append(label, style=_recency_style(secs))
    return row


def specimen_lines() -> list[RenderableType]:
    """The specimen card, one renderable per line (print through the themed console)."""
    from meshterm.core.nodetypes import register_node_type

    platform = get_platform()
    lines: list[RenderableType] = []
    lines.append(Text.assemble(("Specimen", "accent"), ("  ·  ", "muted"), platform.name))
    lines.append(Text())
    lines.append(_palette_row())
    legend = Text("stock palette · 8 muted · 13 clients · 15 you", style="muted")
    lines.append(legend)
    lines.append(Text())

    status = Text("status  ")
    for mark, style in (("✓", "ok"), ("✗", "err"), ("⚠", "warn"), ("●", "err"), ("○", "muted")):
        status.append(mark + " ", style=style)
    lines.append(status)

    # The node marks wear the *type* styles (not the map's raw hex colours): on the
    # console they resolve to exact palette slots, and the row then agrees with the
    # names row below it. Raw hex here would also trip Rich's per-Style ANSI memo —
    # a style first rendered by a truecolor console replays truecolor everywhere.
    nodes = Text("nodes   ")
    nodes.append("★ ", style="you")
    for mark, style in (
        ("● ", "type.node"), ("▲ ", "type.repeater"), ("■ ", "type.room"),
        ("◉ ", "type.sensor"), ("○", "muted"),
    ):
        nodes.append(mark, style=style)
    lines.append(nodes)

    classes = Text("classes ")
    for icon in KIND_ICONS.values():
        classes.append(glyph(icon) + " ", style="brand")
    classes.append("  payloads ", style="muted")
    for typename in ("REQ", "RESPONSE", "TEXT_MSG", "GRP_TXT", "GRP_DATA", "ANON_REQ"):
        classes.append(glyph(PAYLOAD_ICONS[typename]) + " ")
    for typename in ("PATH", "TRACE", "MULTIPART", "CONTROL"):
        classes.append(glyph(PAYLOAD_ICONS[typename]) + " ")
    lines.append(classes)

    channels = Text("channels ")
    channels.append(channel_glyph("#public", None) + " name-derived  ", style="brand")
    channels.append(glyph("🌐") + " public  ")
    channels.append(glyph("🔒") + " private")
    lines.append(channels)

    concepts = Text("concepts ")
    for icon in ("📡", "🕒", "🔄", "💾", "📂", "🔑", "🗑", "✎", "⚡", "⭐", "📤", "📨"):
        concepts.append(glyph(icon) + " ")
    for icon in ("🔔", "🔕", "📱", "🔗", "🏆", "⌨"):
        concepts.append(glyph(icon) + " ")
    lines.append(concepts)

    lines.append(Text("keys    ⌫ ⇧ … ↕ ↔ ↻ ⌖ ❯ ▸"))
    lines.append(Text())

    for name, key, node_type in _DEMO_NAMES:
        register_node_type(key, node_type)
    names = Text("names   ")
    for name, key, _ in _DEMO_NAMES:
        names.append(name + "  ", style=name_style(name, key))
    names.append("me", style="you")
    lines.append(names)

    lines.append(Text("heard   ").append_text(_ages_row()))

    snr = Text("snr     ")
    for reading in (8.0, -2.0, -12.0):
        snr.append(f"{reading:+.1f} ", style=snr_style(reading))
    lines.append(snr)

    # What the render boundary makes of stored text: accents fold on PicoCalc (the raw
    # form would be tofu there — the console font has no accented Latin), Cyrillic is
    # font-native and passes through on both platforms.
    folded = fold_text("café Montréal")
    lines.append(Text.assemble("text    ", (folded, "brand"), ("  ·  ", "muted"), "привет мир"))
    lines.append(Text())

    chart = Text("chart   ")
    chart.append("⡀⡄⡆⣆⣦⣶⣷⣿", style="snr.good")
    chart.append("⣶⣤⣀", style="snr.ok")
    chart.append("⡀⠄⠂", style="snr.bad")
    chart.append("  braille", style="muted")
    lines.append(chart)
    return lines
