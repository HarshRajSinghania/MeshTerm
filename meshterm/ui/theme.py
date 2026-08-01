"""Shared Rich theme and console factory for a consistent, modern look."""

from __future__ import annotations

import colorsys
from functools import lru_cache
from typing import Optional

from rich.console import Console
from rich.theme import Theme

MESH_THEME = Theme(
    {
        "brand": "bold #5eead4",
        "accent": "bold #818cf8",
        # The highlighted-button fill (cyan block, dark text) shared by every popup dialog.
        # It must be a *single* theme name: Rich silently drops a style string that mixes a
        # theme name with an attribute (e.g. "reverse brand" renders as plain text), so the
        # reverse is baked into the definition here rather than tacked on at the call site.
        "selected": "reverse bold #5eead4",
        # Reversed error (the text-editor cursor sitting on an over-budget character). Baked
        # in for the same reason as ``selected`` — "reverse err" would render as plain text.
        "err.reverse": "reverse bold #f87171",
        # Our own node, anywhere it is named: pure white, deliberately outside the
        # per-node hue spectrum (see node_style) so "you" is always easy to spot.
        "you": "bold #ffffff",
        # A confirmed companion's name on the startup device picker: pure white so the
        # devices we've actually talked to before jump out above the merely-detected ports.
        "device.known": "bold #ffffff",
        # The picker's Bluetooth TYPE badge: a white rune on the official Bluetooth blue
        # (Pantone 300, #0057b8), echoing the real logo so BLE reads at a glance.
        "bluetooth": "bold #ffffff on #0057b8",
        # The badge's tapered edges: half-block glyphs drawn in the same blue as the
        # foreground (over the terminal's own background), so only their inner half fills and
        # the badge reads a touch wider than the single rune cell without a hard rectangle.
        "bluetooth.edge": "#0057b8",
        "ok": "bold #4ade80",
        "warn": "bold #fbbf24",
        "err": "bold #f87171",
        "muted": "#94a3b8",
        # Panel/dialog titles: the same hue as the border they sit in, one shade brighter,
        # so the title reads as part of its frame while still standing out from it. One
        # entry per border style the frame compositor is given (see theme.title_style).
        "title.accent": "bold #a5b4fc",
        "title.muted": "bold #cbd5e1",
        "title.warn": "bold #fcd34d",
        "title.err": "bold #fca5a5",
        "title.ok": "bold #86efac",
        "title.brand": "bold #99f6e4",
        # Panel/dialog footer hints: the border's hue one shade *darker* (and not bold), the
        # mirror image of the ``title.*`` brightening — the hint reads as part of the frame
        # while receding behind it. One entry per border style (see theme.hint_style).
        "hint.accent": "#6366f1",
        "hint.muted": "#64748b",
        "hint.warn": "#f59e0b",
        "hint.err": "#ef4444",
        "hint.ok": "#22c55e",
        "hint.brand": "#2dd4bf",
        # A step darker than ``muted`` for placeholder dashes (a node's missing packet count /
        # age) that should recede below the real, muted values around them.
        "faint": "#64748b",
        # A further step darker than ``faint``, for a meter's unlit track (the SNR quality
        # bars): dark enough to read as background, not as a dimmer version of the reading.
        "track": "#334155",
        "snr.good": "bold #4ade80",
        "snr.ok": "bold #fbbf24",
        "snr.bad": "bold #f87171",
        # The status-bar battery gauge, as a fuel-gauge palette: green healthy, orange at
        # a quarter, red near empty. ``batt.dim`` is the off-beat of the critically-low
        # blink — a dark slate the red glyph flickers to, so it pulses without vanishing.
        "batt.high": "bold #4ade80",
        "batt.mid": "bold #fb923c",
        "batt.low": "bold #f87171",
        "batt.dim": "#475569",
        # Type colours (PicoCalc 16-slot palette): keyed by node type when name_colour="type".
        "type.node": "bold #5eead4",       # client — teal (brand)
        "type.repeater": "#a5b4fc",        # repeater — indigo
        "type.room": "bold #4ade80",       # room — green (ok)
        "type.sensor": "bold #fbbf24",     # sensor — amber (warn)
    }
)

#: The 16-slot palette for PicoCalc (truecolor=False). Same style names as MESH_THEME so
#: the app's internal logic never changes; only the RGB values fit the console's limit.
MESH_THEME_16 = Theme(
    {
        "brand": "bold color(201)",        # slot 201: teal
        "accent": "bold color(63)",        # slot 63: indigo
        "selected": "reverse bold color(201)",
        "err.reverse": "reverse bold color(203)",
        "you": "bold color(15)",           # slot 15: white
        "device.known": "bold color(15)",
        "bluetooth": "bold color(15) on color(4)",
        "bluetooth.edge": "color(4)",
        "ok": "bold color(34)",            # slot 34: green
        "warn": "bold color(214)",         # slot 214: orange/amber
        "err": "bold color(203)",          # slot 203: red
        "muted": "color(8)",               # slot 8: muted grey
        "title.accent": "bold color(63)",
        "title.muted": "bold color(7)",
        "title.warn": "bold color(214)",
        "title.err": "bold color(203)",
        "title.ok": "bold color(34)",
        "title.brand": "bold color(201)",
        "hint.accent": "color(63)",
        "hint.muted": "color(8)",
        "hint.warn": "color(214)",
        "hint.err": "color(203)",
        "hint.ok": "color(34)",
        "hint.brand": "color(201)",
        "faint": "color(8)",
        "track": "color(0)",
        "snr.good": "bold color(34)",
        "snr.ok": "bold color(214)",
        "snr.bad": "bold color(203)",
        "batt.high": "bold color(34)",
        "batt.mid": "bold color(214)",
        "batt.low": "bold color(203)",
        "batt.dim": "color(0)",
        "type.node": "bold color(201)",
        "type.repeater": "color(63)",
        "type.room": "bold color(34)",
        "type.sensor": "bold color(214)",
    }
)


def make_console() -> Console:
    """Create the application's themed Rich console.

    On legacy Windows consoles the standard streams default to ``cp1252``, which cannot
    encode the box-drawing and marker glyphs (``◆ ● ★``) the UI uses; this reconfigures
    them to UTF-8 where the runtime supports it so output never raises ``UnicodeEncodeError``.

    Returns:
        A :class:`rich.console.Console` configured with the MeshTerm theme.
    """
    import sys

    from meshterm.platforms import get_platform

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - stream not reconfigurable
                pass
    theme = MESH_THEME if get_platform().truecolor else MESH_THEME_16
    return Console(theme=theme)


def title_style(border_style: str) -> str:
    """Return the title style matching a panel's border: the same hue, brighter.

    Args:
        border_style: The theme name the panel's border is drawn in (``"accent"``,
            ``"warn"``, ...).

    Returns:
        The matching ``title.*`` theme name, or ``border_style`` itself when no brighter
        variant is defined (so an unknown border still gets a consistently-tinted title).
    """
    name = f"title.{border_style}"
    return name if name in MESH_THEME.styles else border_style


def hint_style(border_style: str) -> str:
    """Return the footer-hint style matching a panel's border: the same hue, muted.

    The bottom-border counterpart of :func:`title_style` — where the title brightens the
    border's hue, the hint darkens it, so both read as part of the frame with the right
    emphasis.

    Args:
        border_style: The theme name the panel's border is drawn in (``"accent"``,
            ``"warn"``, ...).

    Returns:
        The matching ``hint.*`` theme name, or ``"muted"`` when no variant is defined (so
        an unknown border keeps the old neutral hint rather than a loud one).
    """
    name = f"hint.{border_style}"
    return name if name in MESH_THEME.styles else "muted"


#: The per-node hue *spectrum*: a node's first key byte maps straight onto the HSV colour
#: wheel — ``0x00`` is red, sweeping the full spectrum round to ``0xff`` — at a fixed
#: saturation and value tuned to stay vivid and readable on the dark theme across every hue.
#: So all 256 first-byte values give 256 distinct hues, rather than folding onto a handful
#: of palette entries. The app-wide rule is unchanged: a node's *name* is always coloured —
#: by this spectrum, keyed on the node's key (see :func:`node_style`) so the colour is the
#: node's identity, surviving renames and colouring every surface that knows any prefix of
#: the key identically — and our own node is always the pure-white ``you`` style instead,
#: so "us" never blends into the crowd. Shared by the node lists, the chat transcript, the
#: dashboard feed, and the packet viewer, so one node reads as one colour everywhere.
_NODE_HUE_SAT = 0.65
_NODE_HUE_VAL = 0.95


def node_style(key: str) -> str:
    """The stable spectrum hue a node's *key* selects — the hash-derived node colour.

    The node's first key byte is mapped straight onto the HSV colour wheel (``0x00`` red,
    sweeping round to ``0xff``) at a fixed saturation/value (:data:`_NODE_HUE_SAT`,
    :data:`_NODE_HUE_VAL`), so all 256 first-byte values give 256 distinct hues. Only the
    first byte picks the colour, so any prefix of the key a surface happens to hold — a
    2-hex path hop, the stored 12-hex id, the full 64-hex public key — lands on the same
    hue: one node, one colour, however it was learned.

    Args:
        key: The node's key/hash as hex (any length ≥ 1 byte, ``0x``/mixed-case tolerated).

    Returns:
        A ``"bold #rrggbb"`` style string.
    """
    raw = key.lower().removeprefix("0x")
    try:
        byte = int(raw[:2], 16)
    except ValueError:  # not hex — fall back to the character sum so *something* stable shows
        byte = sum(map(ord, raw)) % 256
    r, g, b = colorsys.hsv_to_rgb(byte / 256, _NODE_HUE_SAT, _NODE_HUE_VAL)
    return f"bold #{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def name_style(name: str, key: Optional[str] = None) -> str:
    """The stable colour a node or sender name is drawn in — keyed on the node's key.

    Dispatches to either hash-derived colouring (REGULAR, ``name_colour="key"``) or
    type-based colouring (PICOCALC, ``name_colour="type"``) based on the active platform.

    Args:
        name: The display name (unused for the hue; kept so every call site reads
            ``name_style(name, key)`` and the pair stays greppable).
        key: Any known prefix of the node's key/hash; when given, the hue is
            :func:`node_style`'s (on REGULAR) — hash-derived, so a rename keeps the
            colour and every surface that knows the key agrees. ``None``/empty marks a
            sender whose key we couldn't resolve — drawn ``muted``, because colour is
            reserved for keyed identities (callers with only a name resolve it first via
            :func:`~meshterm.services.trace_runner.make_name_key_resolver`).

    Returns:
        A spectrum hue (or ``muted`` for the keyless); the same node always maps to the
        same hue, so it keeps its colour across screens and sessions.
    """
    from meshterm.platforms import get_platform

    if get_platform().name_colour == "type":
        return _name_style_by_type(name, key)
    if key:
        return node_style(key)
    return "muted"


def _name_style_by_type(name: str, key: Optional[str] = None) -> str:
    """Type-based node colouring (PicoCalc: 16-slot palette can't afford hash spectrum).

    Looks up the node's first-byte prefix in a type registry and returns its colour;
    unknown prefixes stay ``muted``. Called by :func:`name_style` when the platform
    specifies ``name_colour="type"``.

    Args:
        name: The display name (kept for signature compatibility with :func:`name_style`).
        key: Any known prefix of the node's key/hash; typed by first-byte prefix lookup.

    Returns:
        A ``type.*`` style name or ``muted``.
    """
    if not key:
        return "muted"
    node_type = _node_type_by_prefix(key)
    if node_type is None:
        return "muted"
    return f"type.{node_type}"


#: Process-wide registry: first-byte hex prefix → node type name (e.g., "a1" → "repeater").
#: Fed by the reception layer as nodes are first heard, so a unique prefix always types
#: consistently. Unknown prefixes stay out, defaulting to muted.
_TYPE_REGISTRY: dict[str, str] = {}


def register_node_type(key: str, node_type: Optional[int]) -> None:
    """Register a node's first-byte prefix to its type for colour lookup.

    Called by the reception layer when a new node is first heard, ensuring every known
    prefix types consistently. A prefix collision (two nodes with the same first byte but
    different types) colours by whichever was registered last — acceptable since colour
    is a hint, not a guarantee. On PicoCalc (16-slot palette), this is how
    :func:`_name_style_by_type` maps a key to a colour without dedicating spectrum space.

    Args:
        key: The node's public key or any prefix (extracted to the first byte).
        node_type: The node's type from the advert (NODE_TYPE_CHAT, NODE_TYPE_REPEATER, etc.),
            or ``None`` if unknown.
    """
    from meshterm.core.models import (
        NODE_TYPE_CHAT, NODE_TYPE_REPEATER, NODE_TYPE_ROOM, NODE_TYPE_SENSOR,
    )

    if not key or node_type is None:
        return
    raw = key.lower().removeprefix("0x")
    try:
        prefix = raw[:2]
    except (IndexError, ValueError):
        return
    type_name = {
        NODE_TYPE_CHAT: "node",
        NODE_TYPE_REPEATER: "repeater",
        NODE_TYPE_ROOM: "room",
        NODE_TYPE_SENSOR: "sensor",
    }.get(node_type)
    if type_name:
        _TYPE_REGISTRY[prefix] = type_name


def _node_type_by_prefix(key: str) -> Optional[str]:
    """Look up a node's type by its first-byte prefix."""
    if not key:
        return None
    raw = key.lower().removeprefix("0x")
    try:
        prefix = raw[:2]
    except (IndexError, ValueError):
        return None
    return _TYPE_REGISTRY.get(prefix)


@lru_cache(maxsize=1024)
def fold_text(text: str) -> str:
    """NFKD-fold accents and replace emoji on PicoCalc (storage untouched).

    On REGULAR, returns text unchanged (emoji and accents are both drawn).
    On PICOCALC, strips accents and replaces emoji with single-glyph placeholders so
    the text fits the 512-glyph font contract. Called at the render boundary for names,
    message bodies, and any user-facing text that might carry non-ASCII.

    Args:
        text: The text to fold (or pass through).

    Returns:
        The folded text, or the input unchanged on REGULAR.
    """
    from meshterm.platforms import get_platform

    if get_platform().ascii_fold:
        import unicodedata
        # NFKD fold: decompose accents into separate combining marks, then drop them
        folded = unicodedata.normalize("NFKD", text)
        # Strip combining marks (category Mn = Mark, nonspacing)
        folded = "".join(c for c in folded if unicodedata.category(c) != "Mn")
        # Replace common emoji and brackets with ASCII equivalents
        folded = _EMOJI_FOLD_TABLE.get(folded, folded)
        return folded
    return text


#: Simple emoji→placeholder table for text that must fit the 512-glyph font.
_EMOJI_FOLD_TABLE = {
    "🔐": "[key]",
    "🔑": "[key]",
    "📡": "[radio]",
    "💬": "[msg]",
    "✅": "[ok]",
    "✗": "[err]",
    "⚠": "[warn]",
    "●": "[node]",
    "▲": "[rep]",
    "■": "[room]",
    "◉": "[sens]",
}

#: The compact glyph table: emoji → single BMP character for PicoCalc's 512-glyph font.
#: Every icon used in the UI (KIND_ICONS, PAYLOAD_ICONS, concept glyphs) maps to a single
#: narrow character. The node-type glyphs (★●▲■◉○) pass through unchanged (they're in
#: the font). On REGULAR (emoji=True), glyph() is identity; on PICOCALC (emoji=False),
#: it looks up the emoji and returns the mapped glyph or the original if not mapped.
_GLYPH_MAP = {
    "📢": "▶",   # advert / kind icon
    "📊": "╳",   # telemetry
    "📦": "□",   # packet
    "💬": "◇",   # message
    "✅": "✓",   # ack / ok
    "❔": "?",   # unknown / fallback
    "📥": "↓",   # REQ / request
    "📮": "◈",   # RESPONSE
    "📩": "◊",   # TEXT_MSG
    "📻": "~",   # GRP_TXT / channel
    "💽": "◐",   # GRP_DATA
    "🎭": "♫",   # ANON_REQ
    "🧭": "↗",   # PATH
    "🎯": "✕",   # TRACE
    "🧩": "◬",   # MULTIPART
    "🧰": "⚙",   # CONTROL
    "🕒": "⏰",   # clock / time
    "🔄": "◉",   # reboot / cycle (but ◉ is sensor, use ○)
    "💾": "⛐",   # backup / save
    "📂": "◇",   # restore / open
    "🔑": "◆",   # key / credential
    "🔐": "◆",   # auth / secret
    "🗑": "✕",   # delete / trash
    "✎": "╳",   # edit / compose
    "⚙": "⚙",   # parameter / gear
    "#": "#",    # count (ASCII)
    "▶": "▶",    # run / play
    "⚡": "◆",   # explore / probe
    "★": "★",    # best / winner (kept)
    "⭐": "★",   # watch / highlight → ★
    "📤": "↑",   # send now
    "📨": "◈",   # courier / queue
    "💬": "◊",   # chat (dup, overwrites above)
    "🔔": "◐",   # notify
    "🔕": "◑",   # mute / notifications off
    "📱": "□",   # QR
    "🔗": "○",   # link
    "↻": "⟲",   # re-read / refresh
    "↕": "⟷",   # reorder
    "⇄": "⟷",   # reverse (flip path)
    "🏆": "◆",   # trophy / record
    "⌨": "↤",   # command line / keyboard
    "🚪": "╬",   # quit / door
    # Node type glyphs pass through unchanged (they're in the font)
    "★": "★",   # "you" marker
    "●": "●",   # node / client
    "▲": "▲",   # repeater
    "■": "■",   # room
    "◉": "◉",   # sensor
    "○": "○",   # unknown
}


def glyph(emoji_or_char: str) -> str:
    """Return a single-cell glyph matching the emoji or character.

    On REGULAR, returns the emoji unchanged (emoji and rich rendering work). On PICOCALC,
    looks up the emoji in the compact glyph table and returns a single-cell character that
    fits the 512-glyph PSF font. Node-type glyphs (★●▲■◉○) always pass through unchanged.

    Args:
        emoji_or_char: An emoji, a node glyph, or a literal character.

    Returns:
        The input unchanged on REGULAR, or the mapped glyph on PICOCALC.
    """
    from meshterm.platforms import get_platform

    if get_platform().emoji:
        return emoji_or_char
    return _GLYPH_MAP.get(emoji_or_char, emoji_or_char)


def snr_style(snr: float | None) -> str:
    """Return a theme style name describing an SNR value's quality.

    Args:
        snr: An SNR reading in dB, or ``None``.

    Returns:
        ``"snr.good"``, ``"snr.ok"``, ``"snr.bad"``, or ``"muted"`` for ``None``.
    """
    if snr is None:
        return "muted"
    if snr >= 5:
        return "snr.good"
    if snr >= -5:
        return "snr.ok"
    return "snr.bad"
