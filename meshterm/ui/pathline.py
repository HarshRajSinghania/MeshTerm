"""Path lines — the flexible hop-sequence text widget (powerline chips or arrows).

This is the successor to :func:`~meshterm.ui.widgets.path_text` as THE way a hop
sequence renders as text — a trace's walked route, a packet's ``via`` chain, a
message's delivery paths, the composer's preview, a trophy row. Where ``path_text``
is one fixed presentation (arrow-joined, one line, overflow left entirely to the
caller), a :class:`PathLine` separates *what the hops are* from *how the line is
drawn*, so every surface the survey found can eventually route through it:

* **Two separator styles.** ``plain`` joins hops with muted ``→`` arrows — today's
  look, unchanged. ``powerline`` renders each hop as a colour-filled chip joined by
  the solid-triangle separator U+E0B0 (foreground = previous chip's fill, background
  = next's — the interlocking oh-my-posh look), the chip fill being the node's
  hash-derived hue (:func:`~meshterm.ui.theme.node_style`), our own node the pure
  ``you`` white, a keyless hop grey. ``auto`` (the default) picks powerline exactly
  when the terminal can draw it (:func:`~meshterm.ui.termfont.powerline_enabled` —
  a recommended font, a glyph-capable renderer, or the user's override) and falls
  back to arrows everywhere else, so no terminal ever sees tofu.
* **Three overflow answers**, matching the three patterns the surfaces already use:
  :meth:`PathLine.text` is the full one-liner (for callers that ellipsize or
  h-scroll it themselves), :meth:`PathLine.ellipsized` fits a width by eliding
  *middle* hops behind a ``⋯`` mark — both endpoints survive, unlike a tail
  truncation that amputates the destination — and :meth:`PathLine.wrapped` breaks
  at hop boundaries under a hanging indent (never mid-name, never mid-chip).
* **The same hop semantics everywhere.** A :class:`PathHop` carries what the app's
  conventions need: the label (a name, or a hash standing as identity), the key any
  known prefix of which picks the hue, the ``you`` flag, the trace-flavour
  ``(3d)`` annotation, the lit-prefix width for hash labels (the
  ``highlighted_hash`` two-tone), the ``dim`` fade for resolved return legs, and an
  explicit style override for context colourings that outrank identity. Chips keep
  the exact same words as arrows — only colours and separators change — so a route
  reads identically whichever mode drew it.

Existing call sites still render through ``path_text``; migrating them here is a
separate, per-surface pass. The composer's insertion cursor is the one deliberate
mode exception: a render asked for ``cursor_arrow`` always draws plain arrows, since
an insertion point lives *between* hops and chips fuse that seam shut.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from rich.text import Text

from .termfont import powerline_enabled
from .theme import MESH_THEME, node_style

#: The powerline solid right-pointing triangle (U+E0B0) — the one glyph the widget
#: uses, deliberately the *core* set so every recommended font qualifies (the rounded
#: caps at U+E0B4+ exist only in full Nerd Font patches).
POWERLINE_SEP = "\ue0b0"

#: The plain-mode joining arrow, exactly as ``path_text`` draws it today.
_ARROW = " → "

#: Chip ink: near-black slate for readable text on every spectrum hue and on white.
_CHIP_FG = "#0f172a"
#: Chip ink, softened — annotations and a hash label's unlit tail (the two-tone).
_CHIP_FG_SOFT = "#334155"
#: Chip fill for a keyless hop — the ``faint`` grey; colour stays reserved for
#: keyed identities in chips just as it is in arrow mode.
_KEYLESS_BG = "#64748b"
#: Our own node's chip fill: the pure ``you`` white.
_YOU_BG = "#ffffff"
#: A dimmed hop's chip: dark slate fill with muted ink, receding like ``faint`` text.
_DIM_BG = "#334155"
_DIM_FG = "#94a3b8"

#: The mark standing in for elided middle hops (see :meth:`PathLine.ellipsized`) —
#: rendered as a dim pseudo-hop so it recedes in both modes.
_ELISION = "⋯"


@dataclass(frozen=True)
class PathHop:
    """One hop of a path line — the semantics, independent of how it is drawn.

    Attributes:
        label: What is shown — a resolved name, or a hash standing as the node's
            identity (set ``lit_bytes`` for the two-tone prefix in that case).
        key: Any known prefix of the node's key/hash — picks the hash-derived hue
            (its first byte, so every prefix agrees). ``None`` renders muted/grey:
            colour is reserved for keyed identities.
        you: Our own node — pure white, in either mode.
        annotation: The trace-flavour hash note, rendered ``" (3d)"`` after the
            label in both modes (pass the bare ``3d``, no parentheses).
        lit_bytes: For a hash label: leading *bytes* drawn in the hue (the
            ``highlighted_hash`` two-tone), the rest muted/soft. ``0`` for names.
        dim: Fade the hop (and the arrow leading into it) — a planned route's
            resolved return leg, "not yours to compose".
        style: Explicit colour override — a hex style (``"#rrggbb"``, attributes
            allowed) or theme name — for context colourings that outrank identity.
            Plain mode uses it as the label style; powerline as the chip fill.
    """

    label: str
    key: Optional[str] = None
    you: bool = False
    annotation: Optional[str] = None
    lit_bytes: int = 0
    dim: bool = False
    style: Optional[str] = None


def _style_hex(style: str) -> Optional[str]:
    """The ``#rrggbb`` a style string or theme name resolves to, else ``None``."""
    for token in reversed(style.split()):
        if token.startswith("#") and len(token) == 7:
            return token
    themed = MESH_THEME.styles.get(style)
    if themed is not None and themed.color is not None:
        try:
            return "#" + themed.color.get_truecolor().hex.lstrip("#")
        except Exception:
            return None
    return None


class PathLine:
    """A hop sequence, renderable as arrow-joined text or powerline chips.

    Build one from :class:`PathHop` entries, then ask for the shape the surface
    needs: :meth:`text` (full line), :meth:`ellipsized` (middle-elided to a width),
    or :meth:`wrapped` (hop-boundary lines under a hanging indent).

    Args:
        hops: The hops in display order (the widget never reorders; a reversed or
            mirrored walk is the caller's composition).
        mode: ``"auto"`` (powerline when the terminal can draw it — the default),
            ``"powerline"``, or ``"plain"``.
        separator: The plain-mode joiner. Defaults to the app-wide ``" → "``; a
            surface with its own convention (the atlas trail's ``" › "``) may pass
            it. Ignored by powerline mode.
        empty: The muted text a hopless path reads as (``"direct"``).
    """

    def __init__(
        self,
        hops: Sequence[PathHop],
        *,
        mode: str = "auto",
        separator: str = _ARROW,
        empty: str = "direct",
    ) -> None:
        self._hops = list(hops)
        self._mode = mode
        self._separator = separator
        self._empty = empty

    # --- the three shapes ---------------------------------------------------------

    def text(self, *, cursor_arrow: Optional[int] = None) -> Text:
        """The full one-line rendering; overflow is the caller's call, per surface.

        Args:
            cursor_arrow: Draw this joining gap (0-based, between hops *j* and
                *j+1*) as the composer's reverse-video insertion cursor. Forces
                plain mode for the render — chips fuse the seam an insertion
                point needs to sit in.

        Returns:
            A one-line :class:`Text` (the ``empty`` note when there are no hops).
        """
        if not self._hops:
            return Text(self._empty, style="muted")
        return self._render(self._hops, cursor_arrow=cursor_arrow)

    def ellipsized(self, width: int) -> Text:
        """The line fitted to ``width`` by eliding *middle* hops behind ``⋯``.

        Endpoints matter most — a route reads origin and destination first — so the
        fit keeps the first hop and as much of the tail as possible, then drops the
        head too if it must, and only as a last resort truncates a single over-long
        hop the classic way.

        Args:
            width: The cell budget the returned line must fit in.

        Returns:
            A one-line :class:`Text` no wider than ``width``.
        """
        full = self.text()
        if full.cell_len <= width or not self._hops:
            return full
        mark = PathHop(_ELISION, dim=True)
        count = len(self._hops)
        for head in (1, 0):
            for tail in range(count - 1 - head, 0, -1):
                kept = self._hops[:head] + [mark] + self._hops[-tail:]
                candidate = self._render(kept)
                if candidate.cell_len <= width:
                    return candidate
        last = self._render([mark, self._hops[-1]]) if count > 1 else full
        last.truncate(width, overflow="ellipsis")
        return last

    def wrapped(self, width: int, *, indent: int = 0) -> list[Text]:
        """The line broken at hop boundaries, hanging under ``indent`` columns.

        The hanging-indent convention: the caller lays its label lane on the first
        line, so that line is returned *without* the indent prefix while every
        continuation starts with ``indent`` spaces; all lines fit ``width`` and the
        content column is uniformly ``width - indent`` wide. Plain-mode lines that
        continue end with a trailing ``→`` (the "path goes on" cue); chip lines
        always close with their pointed edge. A single hop wider than the content
        column stands alone, truncated with an ellipsis.

        Args:
            width: The full line budget, indent included.
            indent: The hanging-indent column the continuations align under.

        Returns:
            The lines, in order (a single ``empty`` note when there are no hops).
        """
        if not self._hops:
            return [Text(self._empty, style="muted")]
        budget = max(1, width - indent)
        plain = self._resolved_mode() != "powerline"
        trail = Text.assemble((" →", "muted")) if plain else Text()

        groups: list[list[PathHop]] = [[]]
        for hop in self._hops:
            candidate = groups[-1] + [hop]
            rendered = self._render(candidate)
            # Reserve the continuation cue: any line but the last will carry it.
            if groups[-1] and rendered.cell_len + trail.cell_len > budget:
                groups.append([hop])
            else:
                groups[-1] = candidate

        lines: list[Text] = []
        for i, group in enumerate(groups):
            line = Text() if i == 0 else Text(" " * indent)
            body = self._render(group)
            if body.cell_len > budget:
                body.truncate(budget, overflow="ellipsis")
            line.append_text(body)
            if plain and i < len(groups) - 1:
                line.append_text(trail.copy())
            lines.append(line)
        return lines

    # --- rendering ----------------------------------------------------------------

    def _resolved_mode(self) -> str:
        """The effective mode: ``auto`` resolves against the terminal's verdict."""
        if self._mode != "auto":
            return self._mode
        return "powerline" if powerline_enabled() else "plain"

    def _render(self, hops: list[PathHop], *, cursor_arrow: Optional[int] = None) -> Text:
        """Join ``hops`` in the effective mode (plain whenever a cursor is asked)."""
        if cursor_arrow is None and self._resolved_mode() == "powerline":
            return self._render_chips(hops)
        return self._render_plain(hops, cursor_arrow)

    def _render_plain(self, hops: list[PathHop], cursor_arrow: Optional[int]) -> Text:
        """Arrow-joined hops — ``path_text``'s presentation, hop by hop."""
        text = Text()
        for i, hop in enumerate(hops):
            if i:
                if cursor_arrow is not None and i - 1 == cursor_arrow:
                    text.append(" ")
                    text.append("→", style="selected")
                    text.append(" ")
                else:
                    text.append(self._separator, style="faint" if hop.dim else "muted")
            text.append_text(self._plain_hop(hop))
        return text

    def _plain_hop(self, hop: PathHop) -> Text:
        """One arrow-mode hop: label in its identity style, annotation muted."""
        note_style = "faint" if hop.dim else "muted"
        text = Text()
        if hop.dim:
            text.append(hop.label, style="faint")
        elif hop.style:
            text.append(hop.label, style=hop.style)
        elif hop.you:
            text.append(hop.label, style="you")
        elif hop.key and hop.lit_bytes > 0:
            split = hop.lit_bytes * 2  # the highlighted_hash two-tone, in hex digits
            text.append(hop.label[:split], style=node_style(hop.key))
            text.append(hop.label[split:], style="muted")
        elif hop.key:
            text.append(hop.label, style=node_style(hop.key))
        else:
            text.append(hop.label, style="muted")
        if hop.annotation:
            text.append(f" ({hop.annotation})", style=note_style)
        return text

    def _render_chips(self, hops: list[PathHop]) -> Text:
        """Powerline chips: each hop filled with its hue, seams interlocked."""
        fills = [self._chip_fill(hop) for hop in hops]
        text = Text()
        for i, hop in enumerate(hops):
            if i:
                text.append(POWERLINE_SEP, style=f"{fills[i - 1]} on {fills[i]}")
            text.append_text(self._chip(hop, fills[i]))
        text.append(POWERLINE_SEP, style=fills[-1])  # the pointed edge into the page
        return text

    def _chip_fill(self, hop: PathHop) -> str:
        """A chip's fill colour: override, white you, dim slate, hue, keyless grey."""
        if hop.style:
            resolved = _style_hex(hop.style)
            if resolved:
                return resolved
        if hop.you:
            return _YOU_BG
        if hop.dim:
            return _DIM_BG
        if hop.key:
            return _style_hex(node_style(hop.key)) or _KEYLESS_BG
        return _KEYLESS_BG

    def _chip(self, hop: PathHop, fill: str) -> Text:
        """One chip: same words as arrow mode, dark ink on the identity fill."""
        ink = _DIM_FG if hop.dim else _CHIP_FG
        soft = _DIM_FG if hop.dim else _CHIP_FG_SOFT
        text = Text()
        text.append(" ", style=f"on {fill}")
        if hop.lit_bytes > 0 and not hop.dim:
            split = hop.lit_bytes * 2
            text.append(hop.label[:split], style=f"bold {ink} on {fill}")
            text.append(hop.label[split:], style=f"{soft} on {fill}")
        else:
            weight = "" if hop.dim else "bold "
            text.append(hop.label, style=f"{weight}{ink} on {fill}")
        if hop.annotation:
            text.append(f" ({hop.annotation})", style=f"{soft} on {fill}")
        text.append(" ", style=f"on {fill}")
        return text
