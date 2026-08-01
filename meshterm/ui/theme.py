"""Shared Rich theme and console factory for a consistent, modern look.

Two palettes, one vocabulary: every style *name* here exists in both
:data:`MESH_THEME` (the regular platform's truecolor look) and :data:`MESH_THEME_16`
(the PicoCalc console's 16-slot look), so screens never know which one is active — they
ask for ``"warn"`` or ``"snr.good"`` and the platform decides what that means. The
active theme, the name-colouring rule, the icon funnel and the render-boundary fold are
all **bound at platform-switch time** through :func:`meshterm.platforms.on_platform`:
the hot render paths read module globals and never re-derive platform state per call.
"""

from __future__ import annotations

import colorsys
import re as _re
from functools import lru_cache
from typing import Callable, Optional

from rich.cells import cell_len
from rich.console import Console
from rich.theme import Theme

from ..core.nodetypes import node_type_name
from ..platforms import Platform, on_platform
from .fontset import FONT_CODEPOINTS

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
        # Node-type colours, used where the platform colours names by *type* instead of by
        # key (PicoCalc — see name_style). Defined in both themes so the names always
        # resolve; the regular platform simply never asks for them. Hues follow the map's
        # marker language where the 16-slot palette can: repeaters the calm violet, sensors
        # the map's orange; rooms take the brand teal and plain client nodes a quiet light
        # grey, so infrastructure pops while the crowd stays calm.
        "type.node": "#cbd5e1",
        "type.repeater": "#a5b4fc",
        "type.room": "#5eead4",
        "type.sensor": "#fb923c",
        # The heard-age heat scale's quantized steps (hot → cold). The regular platform
        # interpolates a continuous gradient instead (ui.widgets._recency_style); these
        # exist in both themes so the quantized impl's names resolve everywhere.
        "heat.hot": "#ffffff",
        "heat.warm": "#facc15",
        "heat.cool": "#fb923c",
        "heat.cold": "#94a3b8",
        "heat.never": "#64748b",
    }
)

#: The PicoCalc console's 16 palette slots and the RGB each is programmed to (via
#: ``setvtrgb``; see :func:`vtrgb_lines` and the boot oneshot installed by
#: ``scripts/calculinux-console-font.sh``). This table *is* the palette design:
#:
#: * Slots keep their conventional **hue families** (1/9 red, 2/10 green, 3/11
#:   yellow-orange, 4/12 blue-indigo, 7/15 light/white, 8 grey) so other console
#:   software — and Rich's own nearest-colour downsampling of any stray truecolor —
#:   still lands somewhere sane; only the exact RGBs are retuned to MeshTerm's theme.
#: * Two slots are repurposed outright for the theme's dark slates (5 ``track``,
#:   6 ``faint``): the app never uses magenta or dim cyan, and three greys don't fit
#:   in one "bright black" slot.
#: * The kernel VT renders **bold as brightness**: ``bold`` on a 0–7 foreground jumps
#:   it to slot N+8. Styles below therefore only combine ``bold`` with a slot whose
#:   +8 partner is the same hue family — and never with 5/6, whose partners (13/14)
#:   are unrelated colours.
#: * Backgrounds can only address slots 0–7 (SGR 40–47).
_VT_SLOTS: tuple[tuple[int, str, str], ...] = (
    (0, "background", "#0f172a"),
    (1, "red (hint.err)", "#ef4444"),
    (2, "green (hint.ok)", "#22c55e"),
    (3, "orange (hint.warn, batt.mid, sensors)", "#f59e0b"),
    (4, "indigo (hint.accent, bluetooth bg)", "#6366f1"),
    (5, "track slate (was magenta)", "#334155"),
    (6, "faint slate (was dim cyan)", "#64748b"),
    (7, "light grey (title.muted, client nodes)", "#cbd5e1"),
    (8, "muted grey", "#94a3b8"),
    (9, "err red", "#f87171"),
    (10, "ok green", "#4ade80"),
    (11, "warn amber", "#fbbf24"),
    (12, "accent indigo", "#818cf8"),
    (13, "lavender (title.accent, repeaters)", "#a5b4fc"),
    (14, "brand teal", "#5eead4"),
    (15, "white (you)", "#ffffff"),
)


def vtrgb_lines() -> str:
    """The ``setvtrgb`` palette file content for :data:`_VT_SLOTS` (three CSV lines).

    ``setvtrgb`` takes one line of 16 decimal values per colour channel. The deploy
    script carries this same content literally (a test keeps the two in sync), so a
    fresh SD card gets the palette without running Python.
    """
    channels = []
    for shift in (16, 8, 0):
        values = [(int(hex_.lstrip("#"), 16) >> shift) & 0xFF for _, _, hex_ in _VT_SLOTS]
        channels.append(",".join(str(v) for v in values))
    return "\n".join(channels) + "\n"


#: The 16-slot palette theme: the same style names as :data:`MESH_THEME`, expressed as
#: ``color(N)`` references into :data:`_VT_SLOTS`. Kept literal (rather than derived) so
#: a slot choice is reviewable next to its meaning; the bold-brightness and background
#: rules it must obey are documented on :data:`_VT_SLOTS` and pinned by tests.
MESH_THEME_16 = Theme(
    {
        "brand": "bold color(14)",
        "accent": "bold color(12)",
        "selected": "reverse bold color(14)",
        "err.reverse": "reverse bold color(9)",
        "you": "bold color(15)",
        "device.known": "bold color(15)",
        "bluetooth": "bold color(15) on color(4)",
        "bluetooth.edge": "color(4)",
        "ok": "bold color(10)",
        "warn": "bold color(11)",
        "err": "bold color(9)",
        "muted": "color(8)",
        "title.accent": "bold color(13)",
        "title.muted": "bold color(7)",
        "title.warn": "bold color(11)",
        "title.err": "bold color(9)",
        "title.ok": "bold color(10)",
        "title.brand": "bold color(14)",
        "hint.accent": "color(4)",
        "hint.muted": "color(6)",
        "hint.warn": "color(3)",
        "hint.err": "color(1)",
        "hint.ok": "color(2)",
        "hint.brand": "color(14)",
        "faint": "color(6)",
        "track": "color(5)",
        "snr.good": "bold color(10)",
        "snr.ok": "bold color(11)",
        "snr.bad": "bold color(9)",
        "batt.high": "bold color(10)",
        "batt.mid": "color(3)",
        "batt.low": "bold color(9)",
        "batt.dim": "color(5)",
        "type.node": "color(7)",
        "type.repeater": "color(13)",
        "type.room": "color(14)",
        "type.sensor": "color(3)",
        "heat.hot": "bold color(15)",
        "heat.warm": "color(11)",
        "heat.cool": "color(3)",
        "heat.cold": "color(8)",
        "heat.never": "color(6)",
    }
)


def active_theme() -> Theme:
    """The platform's theme — :data:`MESH_THEME`, or :data:`MESH_THEME_16` on PicoCalc."""
    return _ACTIVE_THEME


def make_console() -> Console:
    """Create the application's themed Rich console.

    On legacy Windows consoles the standard streams default to ``cp1252``, which cannot
    encode the box-drawing and marker glyphs (``◆ ● ★``) the UI uses; this reconfigures
    them to UTF-8 where the runtime supports it so output never raises ``UnicodeEncodeError``.

    Returns:
        A :class:`rich.console.Console` configured with the platform's theme.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - stream not reconfigurable
                pass
    return Console(theme=_ACTIVE_THEME)


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

    On the regular platform the hue is :func:`node_style`'s hash-derived spectrum, so a
    rename keeps the colour and every surface that knows the key agrees. On PicoCalc
    (``name_colour="type"``) the 16-slot palette can't afford a hue spectrum on top of
    the semantic colours, so the name takes its node's *type* colour instead
    (``type.node``/``type.repeater``/``type.room``/``type.sensor``), looked up in the
    :mod:`~meshterm.core.nodetypes` registry the reception layer feeds. The dispatch is
    bound at platform-switch time — this wrapper stays importable by name everywhere.

    Args:
        name: The display name (unused for the hue; kept so every call site reads
            ``name_style(name, key)`` and the pair stays greppable).
        key: Any known prefix of the node's key/hash. ``None``/empty marks a sender whose
            key we couldn't resolve — drawn ``muted``, because colour is reserved for
            keyed identities (callers with only a name resolve it first via
            :func:`~meshterm.services.trace_runner.make_name_key_resolver`).

    Returns:
        A style for the name; the same node always maps to the same style on a given
        platform, so it keeps its colour across screens and sessions.
    """
    return _name_impl(name, key)


def _name_style_by_key(name: str, key: Optional[str] = None) -> str:
    """Regular-platform name colouring: the hash-derived spectrum (``muted`` keyless)."""
    if key:
        return node_style(key)
    return "muted"


def _name_style_by_type(name: str, key: Optional[str] = None) -> str:
    """PicoCalc name colouring: the node's registered *type* picks a palette colour.

    An unknown type — never heard with one, or no key at all — stays ``muted``: colour
    remains reserved for identities we actually know something about. A first-byte
    prefix collision types by whichever node registered last (colour is a hint).
    """
    if not key:
        return "muted"
    node_type = node_type_name(key)
    return f"type.{node_type}" if node_type else "muted"


# -- the compact icon language (PicoCalc) ---------------------------------------------

#: Emoji → single console-font character: the PicoCalc icon language. One glyph per
#: concept, chosen from what the 512-glyph font actually holds (see ui.fontset);
#: concepts that only ever share a *family* (outbound ↑, route ∟) share deliberately.
#: The node-type marks (★●▲■◉○) and status marks (✓ ✗ ⚠ ● ○) are already font-native
#: and never appear here. ``glyph()`` consumes this table at explicit icon call sites
#: (a 1-cell lane the screen composes); the render-boundary fold consumes it for
#: everything else, padding to the emoji's measured width so layout survives.
_GLYPH_MAP: dict[str, str] = {
    # Packet classes (KIND_ICONS)
    "📢": "☼",   # advert — a node radiating its presence
    "📊": "≈",   # telemetry — a waveform of readings
    "📦": "▬",   # packet — a plain slab of payload
    "💬": "¶",   # message — text
    "✅": "✓",   # ack (the ok-family check)
    "❔": "·",   # unknown class — a neutral dot
    # Raw payload classes (PAYLOAD_ICONS); raw ADVERT/ACK reuse ☼/✓ above
    "📥": "↓",   # REQ — inbound ask
    "📮": "↑",   # RESPONSE — outbound answer
    "📩": "→",   # TEXT_MSG (overheard direct message) — text in flight
    "📻": "#",   # GRP_TXT — channel text (the # channel mark)
    "💽": "§",   # GRP_DATA — a data section on a channel
    "🎭": "?",   # ANON_REQ — a request from an unproven identity
    "🧭": "∟",   # PATH — a route with a bend in it
    "🎯": "⌖",   # TRACE — the crosshair (also the map's position mark)
    "🧩": "▒",   # MULTIPART — a frame in fragments
    "🧰": "↨",   # CONTROL — adjustment up-and-down
    # Channel openness (widgets.channel_glyph)
    "＃": "#",   # name-derived channel
    "🌐": "@",   # well-known public channel
    "🔒": "⚿",   # private channel (the padlock mark)
    # Concept icons (menu/list rows)
    "📡": "☼",   # advert tool — same concept as the advert class
    "🕒": "◷",   # clock/sync (the clock-face mark)
    "🔄": "°",   # reboot — the power dot
    "💾": "⌂",   # backup — put it somewhere safe
    "📂": "^",   # restore — bring it back up
    "🔑": "*",   # channel/credential key — masked-secret asterisk
    "🔐": "*",   # identity/auth secret — same secret-material mark
    "🗑": "✗",   # clear/delete — the destructive mark
    "✎": "~",   # compose/edit — a scribble
    "⚡": "!",   # explore/probe
    "⭐": "+",   # watch — added to the watchlist
    "📤": "↑",   # send now (outbound family)
    "📨": "=",   # courier/queue — stacked letters
    "🔔": "•",   # notify — the badge dot
    "🔕": "·",   # mute — the hollowed-out dot
    "📱": "▓",   # QR — a dense block
    "🔗": "&",   # link — the joining glyph
    "🏆": "★",   # trophy case — the best/winner star
    "⌨": "❯",   # command line — the prompt cursor
    "🚪": "",    # quit — no icon; the word carries it
    "🌍": "@",   # map/world (globe family)
    "🕸": "∟",   # mesh walk (route family)
    "🚨": "⚠",   # watchtower alert
    "🛣": "∟",   # longest-haul route (route family)
    "🧳": "→",   # trip/journey
    "🔆": "°",   # brightest sighting
    "📶": "≥",   # TX power sweep — the power ramp
    "🔧": "⚙",   # config (parameter concept)
    "🔨": "!",   # device actions (probe family)
    "📋": "i",   # info
    "📰": "…",   # live feed — a stream of items
    "🎧": "≈",   # monitor — listening to the waveform
    "🗼": "▲",   # repeater admin — the repeater mark itself
    "🔌": "~",   # serial port — the cable
    "👤": "%",   # a person (two-circle silhouette)
    "👥": "%",   # contacts — people
    "👋": "",    # a wave in prose — the words carry it
    "⏳": "…",   # pending/waiting
    "＋": "+",   # fullwidth plus (channels' add row)
}


def glyph(icon: str) -> str:
    """The platform's rendering of an icon: the emoji itself, or its compact glyph.

    On the regular platform this is the identity — emoji icons render as themselves.
    On PicoCalc every icon funnels to a single console-font character via
    :data:`_GLYPH_MAP` (an unmapped icon passes through and is caught by the glyph
    whitelist test / render-boundary fold, not silently invented here). Note the
    compact form is *one cell* where the emoji was two: call sites compose their lanes
    from the returned glyph, so the lane simply tightens on PicoCalc.

    Args:
        icon: An emoji, a node/status mark, or any literal character.

    Returns:
        The character(s) to render for it on the active platform.
    """
    return _glyph_impl(icon)


def _glyph_identity(icon: str) -> str:
    """Regular platform: icons render as themselves."""
    return icon


def _glyph_compact(icon: str) -> str:
    """PicoCalc: icons collapse to their single-cell console-font glyph."""
    return _GLYPH_MAP.get(icon, icon)


# -- the render-boundary fold (PicoCalc) ----------------------------------------------

#: What may survive the fold: every font codepoint, plus the C0 controls the rendered
#: ANSI itself is built from (ESC in its sequences, the newlines between lines).
_FOLD_ALLOWED: frozenset[int] = FONT_CODEPOINTS.union(range(0x00, 0x20))

#: Width-1 characters outside the font with a natural width-1 stand-in. Applied by the
#: fold's translation table (storage is never touched). Characters *in* the font —
#: ``— … ⋯ ⚠ ⌫ ⇧ ⚙ ↻ ◷ ⌖ ⚿ ← ↑ → ↓ ↔ ↕ • ·`` and the Cyrillic block — never appear
#: here: they pass through untranslated.
_FOLD_SINGLES: dict[str, str] = {
    "–": "-", "−": "-", "‒": "-", "―": "—",
    "‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"',
    "‹": "<", "›": ">", "«": "<", "»": ">",
    "×": "x", "÷": "/", "⁄": "/", "∙": "·", "∘": "·",
    "⇒": "→", "⇐": "←", "⇣": "↓", "⇡": "↑", "↩": "←", "↪": "→",
    "⇄": "↔", "⟷": "↔", "⟺": "↔", "⟲": "↻", "⟳": "↻",
    "✕": "✗", "✖": "✗", "✔": "✓",
    "ᛒ": "B",   # the Bluetooth badge rune
    "œ": "o", "Œ": "O", "æ": "a", "Æ": "A", "ø": "o", "Ø": "O",
    "ß": "s", "þ": "p", "Þ": "P", "ð": "d", "Ð": "D", "đ": "d", "Đ": "D",
    "ł": "l", "Ł": "L", "ı": "i",
    # Powerline path-pill chrome (private-use): caps become half-blocks, the separator
    # a plain wedge.
    "": ">", "": "▌", "": "▐",
    # Zero-width machinery folds away entirely (width 0 → empty keeps cell math exact):
    # VS16, ZWJ, ZWSP.
    "️": "", "‍": "", "​": "",
}

#: Built lazily on first fold: ``str.translate`` table = accent folds (NFKD, computed
#: over the Latin ranges once) + :data:`_FOLD_SINGLES` + the emoji map padded to each
#: emoji's measured cell width.
_FOLD_TABLE: Optional[dict[int, str]] = None

#: Truecolor / 256-colour SGR sequences embedded in *pre-rendered* ANSI. The rasterizer
#: console downsamples everything it renders itself, but the braille canvases
#: (``ui.mapcanvas``) emit their own truecolor escapes which pass through Rich verbatim
#: — the fold quantizes those to the 16 slots so the contract holds for every byte out.
_SGR_RGB = _re.compile(r"\x1b\[([34])8;2;(\d+);(\d+);(\d+)m")
_SGR_256 = _re.compile(r"\x1b\[([34])8;5;(\d+)m")

_SLOT_RGBS: tuple[tuple[int, int, int], ...] = tuple(
    (int(h.lstrip("#")[0:2], 16), int(h.lstrip("#")[2:4], 16), int(h.lstrip("#")[4:6], 16))
    for _, _, h in _VT_SLOTS
)

#: (is_background, r, g, b) → the replacement SGR string. The app uses a few dozen
#: distinct colours; this stays tiny.
_SLOT_CACHE: dict[tuple[bool, int, int, int], str] = {}


def _nearest_slot_sgr(background: bool, r: int, g: int, b: int) -> str:
    """The plain 16-colour SGR closest to ``(r, g, b)`` in the :data:`_VT_SLOTS` palette.

    Foregrounds may land on any slot (30–37 / 90–97); backgrounds only on 0–7 (the VT has
    no bright backgrounds), so a bright colour used as a fill picks its dim-bank cousin.
    """
    key = (background, r, g, b)
    cached = _SLOT_CACHE.get(key)
    if cached is None:
        candidates = _SLOT_RGBS[:8] if background else _SLOT_RGBS
        slot = min(
            range(len(candidates)),
            key=lambda i: (
                (candidates[i][0] - r) ** 2
                + (candidates[i][1] - g) ** 2
                + (candidates[i][2] - b) ** 2
            ),
        )
        if background:
            code = 40 + slot
        else:
            code = 30 + slot if slot < 8 else 90 + slot - 8
        cached = _SLOT_CACHE[key] = f"\x1b[{code}m"
    return cached


def _rgb_of_256(index: int) -> tuple[int, int, int]:
    """The canonical RGB of xterm-256 ``index`` (cube and grayscale ramps)."""
    if index < 16:
        return _SLOT_RGBS[index]
    if index < 232:
        index -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[index // 36], steps[index // 6 % 6], steps[index % 6])
    grey = 8 + (index - 232) * 10
    return (grey, grey, grey)


def _quantize_sgr(text: str) -> str:
    """Fold any embedded truecolor / 256-colour SGR down to the 16 palette slots."""
    if "[38;2;" not in text and "[48;2;" not in text and "8;5;" not in text:
        return text
    text = _SGR_RGB.sub(
        lambda m: _nearest_slot_sgr(
            m.group(1) == "4", int(m.group(2)), int(m.group(3)), int(m.group(4))
        ),
        text,
    )
    return _SGR_256.sub(
        lambda m: _nearest_slot_sgr(m.group(1) == "4", *_rgb_of_256(int(m.group(2)))),
        text,
    )


def _build_fold_table() -> dict[int, str]:
    """Compose the full translation table (see :data:`_FOLD_TABLE`)."""
    import unicodedata

    table: dict[int, str] = {}
    # Accented Latin (the font's base table is Cyrillic-coverage Terminus: *no* accented
    # Latin at all) plus fullwidth forms: NFKD-decompose, drop combining marks, keep a
    # clean single ASCII survivor. é→e, Å→A, ＃→#, ﬁ→(skipped: two chars).
    for first, last in ((0x00A1, 0x024F), (0x1E00, 0x1EFF), (0xFF01, 0xFF5E)):
        for cp in range(first, last + 1):
            if cp in FONT_CODEPOINTS:
                continue
            decomposed = unicodedata.normalize("NFKD", chr(cp))
            base = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
            if len(base) == 1 and base.isascii() and base.isprintable():
                table[cp] = base
    for char, replacement in _FOLD_SINGLES.items():
        table[ord(char)] = replacement
    for emoji, compact in _GLYPH_MAP.items():
        pad = max(0, cell_len(emoji) - cell_len(compact))
        table[ord(emoji)] = compact + " " * pad
    return table


@lru_cache(maxsize=4096)
def _fold_to_font(text: str) -> str:
    """Fold ``text`` down to the console font's inventory, cell widths preserved.

    Three stages, cheapest first: the translation table (accents, symbol stand-ins,
    the emoji map — one C-level pass), then only if something non-ASCII survives, a
    per-character sweep replacing anything still outside the font with ``?`` at the
    character's own cell width. The sweep is the safety net that makes the platform's
    no-wide-glyphs guarantee (see ``session._has_wide_glyph``) true *by construction*:
    an emoji this module has never heard of still leaves as narrow ``?``s, never as a
    tofu box that breaks the frame's cell math. Cached — render output repeats heavily
    frame to frame, and the fold is platform-independent once this impl is bound.
    """
    global _FOLD_TABLE
    if _FOLD_TABLE is None:
        _FOLD_TABLE = _build_fold_table()
    folded = _quantize_sgr(text).translate(_FOLD_TABLE)
    if folded.isascii():
        return folded
    if all(ord(ch) in _FOLD_ALLOWED for ch in folded):
        return folded
    return "".join(
        ch if ord(ch) in _FOLD_ALLOWED else "?" * max(0, cell_len(ch)) for ch in folded
    )


def fold_text(text: str) -> str:
    """The render-boundary text filter for the active platform.

    Identity on the regular platform. On PicoCalc, folds any string that is about to be
    drawn — names, message bodies, whole rendered ANSI lines — down to characters the
    console font can shape (see :func:`_fold_to_font`); storage is never touched. ANSI
    escape sequences pass through untouched (they are pure ASCII, and the fold never
    rewrites ASCII). Applied once, centrally, in :func:`meshterm.ui.tui.render.render_to_ansi`
    — individual screens should not need to call it.

    Args:
        text: The text to fold (or pass through).

    Returns:
        The folded text; cell widths are preserved (wide emoji become glyph + pad).
    """
    return _fold_impl(text)


def _no_fold(text: str) -> str:
    """Regular platform: text renders as stored."""
    return text


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


# -- platform binding ------------------------------------------------------------------

_ACTIVE_THEME: Theme = MESH_THEME
_name_impl: Callable[[str, Optional[str]], str] = _name_style_by_key
_glyph_impl: Callable[[str], str] = _glyph_identity
_fold_impl: Callable[[str], str] = _no_fold


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the theme's platform-dependent choices (runs now and on every switch)."""
    global _ACTIVE_THEME, _name_impl, _glyph_impl, _fold_impl, _FOLD_TABLE
    _ACTIVE_THEME = MESH_THEME if platform.truecolor else MESH_THEME_16
    _name_impl = _name_style_by_key if platform.name_colour == "key" else _name_style_by_type
    _glyph_impl = _glyph_identity if platform.emoji else _glyph_compact
    _fold_impl = _fold_to_font if platform.ascii_fold else _no_fold
    # The fold table's emoji pads are computed with cell_len at build time. Cell widths
    # can be re-measured/patched (the emoji-width calibration on the regular platform),
    # so a platform switch drops the table and cache rather than trusting stale pads.
    _FOLD_TABLE = None
    _fold_to_font.cache_clear()
