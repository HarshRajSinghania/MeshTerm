"""Render a string as a scannable QR code in the terminal, using half-block characters.

Each character cell stacks two vertical modules — ``▀`` (upper only), ``▄`` (lower only),
``█`` (both), space (neither) — so a code renders at half the row height of a full-block
one and comfortably fits an interactive window. Modules are always drawn black on a white
background (with the mandatory quiet-zone border), so a phone camera reads the code no
matter what colour theme the terminal itself is using.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Group
from rich.text import Text

if TYPE_CHECKING:
    from ..context import AppContext

#: Quiet-zone width (modules) around the code. Scanners need at least 4 to lock on.
_BORDER = 4

#: Half-block glyph for each (top-module-dark, bottom-module-dark) pair. Drawn as black
#: ink, so a dark module is black and a light module shows the white cell background.
_GLYPH = {
    (True, True): "█",  # █ full block
    (True, False): "▀",  # ▀ upper half
    (False, True): "▄",  # ▄ lower half
    (False, False): " ",
}

#: Constant style for every glyph: black ink on a white field. Naming both ends means the
#: contrast holds regardless of the surrounding terminal palette.
_STYLE = "black on white"


def qr_text(data: str, *, error: str = "m") -> Text:
    """Render ``data`` as a QR code built from half-block characters.

    Args:
        data: The string to encode (e.g. a ``meshcore://channel/add`` share URL).
        error: QR error-correction level — ``l``/``m``/``q``/``h`` (default ``m``).

    Returns:
        A Rich :class:`~rich.text.Text` (no-wrap) whose lines draw the code, ready to hand
        to ``ctx.ui.view`` / ``ctx.ui.show``.
    """
    import segno

    qr = segno.make(data, error=error)
    rows = [[bool(v) for v in row] for row in qr.matrix_iter(border=_BORDER)]
    # Pair rows top-to-bottom; pad an odd final row with light (white) modules so the last
    # half-block cell renders cleanly.
    if len(rows) % 2:
        rows.append([False] * len(rows[0]))

    text = Text(no_wrap=True)
    for top, bottom in zip(rows[0::2], rows[1::2], strict=True):
        line = "".join(_GLYPH[(t, b)] for t, b in zip(top, bottom, strict=True))
        text.append(line, style=_STYLE)
        text.append("\n")
    return text


async def share_popup(ctx: "AppContext", *, name: str, url: str, intro: str) -> None:
    """THE share popup: a muted intro, the scannable code, then the URL to copy out.

    One layout for every ``Share {name}`` surface — the channel share and the contact
    card both come through here, so a change to how MeshTerm presents a share code (a
    caption, a border, a "copied" affordance) lands on all of them at once.

    Args:
        ctx: Shared application context (provides the UI surface).
        name: What is being shared — the popup's title reads ``Share {name}``.
        url: The ``meshcore://…`` share URL, encoded as the QR code and printed under it.
        intro: The one-line instruction above the code ("Scan to add this channel:").
    """
    body = Group(
        Text(intro, style="muted"),
        Text(""),
        qr_text(url),
        Text(""),
        Text(url, style="accent"),
    )
    await ctx.ui.view(body, title=f"Share {name}", footer_hint="Esc close")
