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
        # Our own messages in a chat: pure white, deliberately outside the per-sender hue
        # palette (see meshterm.ui.chat._SENDER_COLORS) so "you" is always easy to spot.
        "you": "bold #ffffff",
        "ok": "bold #4ade80",
        "warn": "bold #fbbf24",
        "err": "bold #f87171",
        "muted": "#94a3b8",
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
