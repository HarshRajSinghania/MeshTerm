"""Shared Rich theme and console factory for a consistent, modern look."""

from __future__ import annotations

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
        # per-name hue palette (see NAME_COLORS below) so "you" is always easy to spot.
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

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - stream not reconfigurable
                pass
    return Console(theme=MESH_THEME)


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


#: Palette of distinct, dark-theme-friendly colors cycled through to give each node or
#: sender name its own stable hue (red is reserved for errors, so it's excluded). The
#: app-wide rule: a node's *name* is always coloured — by this palette, unless the
#: context already colours it (a picker's recency heat, a chart's quality tint) — and
#: our own node is always the pure-white ``you`` style instead, so "us" never blends
#: into the crowd. Shared by the chat transcript, the dashboard feed, and the packet
#: viewer, so one node reads as one colour everywhere.
NAME_COLORS = (
    "bold #f472b6",  # pink
    "bold #60a5fa",  # blue
    "bold #34d399",  # green
    "bold #a78bfa",  # violet
    "bold #fb923c",  # orange
    "bold #22d3ee",  # cyan
    "bold #a3e635",  # lime
    "bold #e879f9",  # fuchsia
)


def name_style(name: str) -> str:
    """The stable per-name colour a node or sender is drawn in, keyed on its characters.

    Args:
        name: The display name (not a hash — hashes stay muted).

    Returns:
        A style string from :data:`NAME_COLORS`; the same name always maps to the
        same hue, so a node keeps its colour across screens and sessions.
    """
    return NAME_COLORS[sum(map(ord, name)) % len(NAME_COLORS)]


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
