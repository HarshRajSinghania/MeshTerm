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
  at hop boundaries under a hanging indent (never mid-name, never mid-chip),
  folding where the route *means* something: the fewest lines it can take, evened
  out across them rather than greedily crammed, and preferring the seam where the
  path fades from composed to mirrored when that fold is free.
* **The same hop semantics everywhere.** A :class:`PathHop` carries what the app's
  conventions need: the label (a name, or a hash standing as identity), the key any
  known prefix of which picks the hue, the ``you`` flag, the trace-flavour
  ``(3d)`` annotation, the lit-prefix width for hash labels (the
  ``highlighted_hash`` two-tone), the ``dim`` fade for resolved return legs, and an
  explicit style override for context colourings that outrank identity. Chips keep
  the exact same words as arrows — only colours and separators change — so a route
  reads identically whichever mode drew it. A hop with *no* label is drawn as pure
  seam — the arrow alone, no chip, no word — which is how a surface says "this end
  is us" without spending cells on saying so (see ``bare_self``).

Existing call sites still render through ``path_text``; migrating them here is a
separate, per-surface pass. The composer's insertion cursor is the one deliberate
mode exception: a render asked for ``cursor_arrow`` always draws plain arrows, since
an insertion point lives *between* hops and chips fuse that seam shut.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from rich.cells import cell_len
from rich.text import Text

from ..core.models import LOCAL_DEVICE_LABEL
from .termfont import powerline_enabled
from .theme import MESH_THEME, node_style

#: The powerline solid right-pointing triangle (U+E0B0) — the male point every chip
#: seam is drawn with, deliberately from the *core* set so every recommended font
#: qualifies (the rounded caps at U+E0B4+ exist only in full Nerd Font patches).
POWERLINE_SEP = "\ue0b0"

#: Its mirror (U+E0B2, core too): the female point opening a *wrapped* line's first
#: chip, so a continuation never reads as a fresh path. Only the very first chip of the
#: whole line keeps the plain square left edge.
POWERLINE_CAP = "\ue0b2"

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
            identity (set ``lit_bytes`` for the two-tone prefix in that case). An
            empty label (with no annotation) makes the hop *bare*: it draws as its
            seam alone — a bit of arrow, no chip, no word — which is how a route
            whose ends are obviously us says so without spending cells on it.
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


def _bare(hop: PathHop) -> bool:
    """Is this hop drawn as pure seam? (An empty label with nothing to annotate.)"""
    return not hop.label and not hop.annotation


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

    @property
    def hops(self) -> list[PathHop]:
        """The hops, for callers that splice lines together (endpoints + relays)."""
        return list(self._hops)

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

    def wrapped(
        self, width: int, *, indent: int = 0, cursor_arrow: Optional[int] = None
    ) -> list[Text]:
        """The line broken at hop boundaries, hanging under ``indent`` columns.

        The hanging-indent convention: the caller lays its label lane on the first
        line, so that line is returned *without* the indent prefix while every
        continuation starts with ``indent`` spaces; all lines fit ``width`` and the
        content column is uniformly ``width - indent`` wide. Plain-mode lines that
        continue end with the separator's own mark — a trailing ``→`` for an
        arrow-joined path, the bare ``,`` for a comma-joined wire spec — the "path
        goes on" cue; chip lines always close with their pointed edge and *open*, from
        the second line down, with the female one (:data:`POWERLINE_CAP`), so a
        continuation is never mistakable for a path starting over. A single hop wider
        than the content column stands alone, truncated with an ellipsis.

        Where the breaks fall is chosen for how it *reads* (see :meth:`_flow` and
        :meth:`_turn_seam`), not by cramming each line full: the fold takes the
        fewest lines the hops allow, spread evenly across them, and lands on the
        route's turn — where a leg gives way to its mirrored return — when folding
        there costs no extra line.

        Args:
            width: The full line budget, indent included.
            indent: The hanging-indent column the continuations align under.
            cursor_arrow: The composer's insertion cursor, as in :meth:`text` (0-based
                seam index). Forces plain arrows; a cursor sitting exactly on a line
                break lands on that line's trailing cue, so it is always visible.

        Returns:
            The lines, in order (a single ``empty`` note when there are no hops).
        """
        if not self._hops:
            return [Text(self._empty, style="muted")]
        budget = max(1, width - indent)
        plain = cursor_arrow is not None or self._resolved_mode() != "powerline"
        # The continuation cue is the separator's own mark, minus the space the next
        # hop would have sat in: " → " reads " →", a wire spec's "," stays ",".
        cue = self._separator.rstrip() if plain else ""
        groups = self._flow(self._hops, budget, plain, len(cue), 0)
        seam = self._turn_seam()
        if seam is not None and len(groups) > 1:
            # The outbound leg's own last line still continues into the return, so it
            # holds the cue back where the whole path's final line doesn't.
            legs = self._flow(self._hops[:seam], budget, plain, len(cue), len(cue))
            legs += self._flow(self._hops[seam:], budget, plain, len(cue), 0)
            if len(legs) <= len(groups):  # the turn folds for free — take it
                groups = legs

        lines: list[Text] = []
        base = 0  # the global index of the group's first hop, for cursor mapping
        for i, group in enumerate(groups):
            local: Optional[int] = None
            if cursor_arrow is not None and 0 <= cursor_arrow - base < len(group) - 1:
                local = cursor_arrow - base
            line = Text() if i == 0 else Text(" " * indent)
            body = self._render(group, cursor_arrow=local, force_plain=plain, cap=bool(i))
            # A lone hop too wide for the column is truncated — leaving room for the
            # cue it still has to carry, so even that line stays inside the width. A
            # column too narrow to hold both drops the cue: content wins the cells.
            continues = i < len(groups) - 1 and 0 < cell_len(cue) < budget
            room = budget - (cell_len(cue) if continues else 0)
            if body.cell_len > room:
                body.truncate(room, overflow="ellipsis")
            line.append_text(body)
            if continues:
                if cursor_arrow == base + len(group) - 1:  # the cursor rides the break
                    line.append(cue[:-1])
                    line.append(cue[-1], style="selected")
                else:
                    line.append(cue, style="muted")
            base += len(group)
            lines.append(line)
        return lines

    # --- where the breaks fall --------------------------------------------------------

    def _measure(self, hops: list[PathHop], plain: bool) -> tuple[list[int], int, int, int]:
        """Each hop's own width, plus what joining, opening and closing a line costs.

        Fitting hops to a column is arithmetic once every hop has been measured once —
        which matters because :meth:`_flow` tries a whole series of columns, and the
        screens re-fit on every repaint (a spinner tick included).

        Args:
            hops: The hops to measure, in order.
            plain: Measure as arrow text rather than as chips.

        Returns:
            ``(cells, join, tail, lead)`` — each hop's cells, the cells one join between
            two hops costs, the cells a line always closes with (the chip mode's pointed
            edge; nothing in arrow mode), and the cells every line *after the first*
            opens with (chip mode's female cap).

        Note:
            Joins are measured at their full width even where a bare neighbour trims one
            (:meth:`_join`), and the closing edge is charged even to a line ending bare —
            an over-estimate of a cell, which packs a hair early and never overflows.
        """
        if plain:
            widths = [self._plain_hop(hop).cell_len for hop in hops]
            return widths, cell_len(self._separator), 0, 0
        widths = [self._chip(hop, self._chip_fill(hop)).cell_len for hop in hops]
        return widths, cell_len(POWERLINE_SEP), cell_len(POWERLINE_SEP), cell_len(POWERLINE_CAP)

    @staticmethod
    def _fill(
        cells: list[int],
        join: int,
        tail: int,
        budget: int,
        reserve: int,
        last: int,
        lead: int = 0,
    ) -> list[int]:
        """Pack hop widths into lines of ``budget`` cells, greedily, never splitting one.

        Args:
            cells: Each hop's width, in order (see :meth:`_measure`).
            join: Cells one join between two hops costs.
            tail: Cells every line closes with.
            budget: The content column's width in cells.
            reserve: Cells held back on a line for the continuation cue it will carry.
            last: Cells held back on a line ending at the *final* hop — ``0`` when the
                path really ends there (nothing follows to cue), ``reserve`` when this
                is only one leg of a path that goes on.
            lead: Cells every line but the first opens with (the female cap).

        Returns:
            How many hops each line takes; a hop too wide for ``budget`` gets a line of
            its own (the caller truncates it).
        """
        sizes: list[int] = []
        width = count = 0
        for i, cell in enumerate(cells):
            grown = width + cell + (join if count else 0)
            held = last if i == len(cells) - 1 else reserve
            if count and grown + tail + held > budget:
                sizes.append(count)
                width, count = lead + cell, 1
            else:
                width, count = grown, count + 1
        sizes.append(count)
        return sizes

    def _flow(
        self, hops: list[PathHop], budget: int, plain: bool, reserve: int, last: int
    ) -> list[list[PathHop]]:
        """Break ``hops`` into the fewest lines, then even those lines out.

        Greedy packing alone widows the tail: a route whose last hop misses the first
        line by a cell or two strands ``us`` alone underneath a full one. Since the
        line *count* is what costs screen rows — and greedy already achieves the
        minimum — the same count is re-packed at the narrowest column that still
        needs it, which spreads the hops across the lines instead of cramming the
        first and starving the last (``us → a → b → c →`` / ``us`` becomes
        ``us → a →`` / ``b → c → us``).

        Args:
            hops: The hops to break up, in order.
            budget: The content column's width in cells.
            plain: Render as arrows rather than chips.
            reserve: Cells held back on a line for the continuation cue it carries.
            last: Cells held back on the line the hops end on (see :meth:`_fill`).

        Returns:
            The hops grouped per line.
        """
        cells, join, tail, lead = self._measure(hops, plain)
        lines = len(self._fill(cells, join, tail, budget, reserve, last, lead))
        low, high = 1, budget
        while low < high:  # the narrowest column still taking `lines` rows
            mid = (low + high) // 2
            if len(self._fill(cells, join, tail, mid, reserve, last, lead)) <= lines:
                high = mid
            else:
                low = mid + 1
        groups: list[list[PathHop]] = []
        at = 0
        for size in self._fill(cells, join, tail, low, reserve, last, lead):
            groups.append(hops[at : at + size])
            at += size
        return groups

    def _turn_seam(self) -> Optional[int]:
        """The hop index a fold would read best at — where the route turns — or ``None``.

        Two things mark a turn. A dimmed tail is one: the composed outbound leg ends
        and the mirrored return begins (see :attr:`PathHop.dim`). A walked boomerang is
        the other — nothing is dimmed once a trace has actually answered, but the hop
        sequence is its own mirror, so its middle *is* that same turn; folding the plan
        and the walk that answered it in the same place keeps the two readings of one
        route looking like one route. Either way the fold needs real legs on both
        sides — at least two hops each — so a path whose *only* dim hop is the
        automatic landing back on us never widows it onto a line of its own.
        """
        seam = next((i for i, hop in enumerate(self._hops) if hop.dim), None)
        if seam is None:
            marks = [(hop.label, hop.annotation) for hop in self._hops]
            if len(marks) % 2 and marks == marks[::-1]:
                seam = len(marks) // 2 + 1  # the first hop of the way home
        if seam is None or seam < 2 or len(self._hops) - seam < 2:
            return None
        return seam

    # --- rendering ----------------------------------------------------------------

    def _resolved_mode(self) -> str:
        """The effective mode: ``auto`` resolves against the terminal's verdict."""
        if self._mode != "auto":
            return self._mode
        return "powerline" if powerline_enabled() else "plain"

    def _render(
        self,
        hops: list[PathHop],
        *,
        cursor_arrow: Optional[int] = None,
        force_plain: bool = False,
        cap: bool = False,
    ) -> Text:
        """Join ``hops`` in the effective mode (plain whenever a cursor is asked).

        ``cap`` marks a wrapped continuation and only means anything to chips; arrow
        mode says the same thing with the trailing cue the line above ends on.
        """
        if not force_plain and cursor_arrow is None and self._resolved_mode() == "powerline":
            return self._render_chips(hops, cap=cap)
        return self._render_plain(hops, cursor_arrow)

    def _render_plain(self, hops: list[PathHop], cursor_arrow: Optional[int]) -> Text:
        """Arrow-joined hops — ``path_text``'s presentation, hop by hop."""
        text = Text()
        for i, hop in enumerate(hops):
            if i:
                gap = self._join(hops[i - 1], hop)
                if cursor_arrow is not None and i - 1 == cursor_arrow:
                    mark = gap.strip()
                    text.append(gap[: len(gap) - len(gap.lstrip())])
                    text.append(mark, style="selected")
                    text.append(gap[len(gap.rstrip()) :])
                else:
                    text.append(gap, style="faint" if hop.dim else "muted")
            text.append_text(self._plain_hop(hop))
        return text

    def _join(self, before: PathHop, after: PathHop) -> str:
        """The separator between two hops, minus the padding a bare neighbour vacates.

        A bare hop takes no cells, so the space the separator holds for it would sit
        against the line's edge: ``us → a → us`` with bare ends reads ``→ a →``, the
        arrows alone standing for the endpoints.
        """
        gap = self._separator
        if _bare(before):
            gap = gap.lstrip()
        if _bare(after):
            gap = gap.rstrip()
        return gap

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

    def _render_chips(self, hops: list[PathHop], *, cap: bool = False) -> Text:
        """Powerline chips: each hop filled with its hue, seams interlocked.

        Args:
            hops: The hops of this one line, in order.
            cap: Open the line with the female point (:data:`POWERLINE_CAP`) in the
                first chip's own fill — the mark of a wrapped continuation, where the
                whole line's very first chip instead keeps its square left edge.
        """
        fills = [self._chip_fill(hop) for hop in hops]
        text = Text()
        if cap:
            text.append(POWERLINE_CAP, style=fills[0])
        for i, hop in enumerate(hops):
            if i:
                # A bare hop has no field for the arrow to land in, so the seam points
                # into the page instead: that lone arrowhead *is* the endpoint.
                style = fills[i - 1] if _bare(hop) else f"{fills[i - 1]} on {fills[i]}"
                text.append(POWERLINE_SEP, style=style)
            text.append_text(self._chip(hop, fills[i]))
        if not _bare(hops[-1]):
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
        """One chip: same words as arrow mode, dark ink on the identity fill.

        A bare hop draws no chip at all — its seam alone carries it (see :meth:`_render_chips`).
        """
        if _bare(hop):
            return Text()
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


def _shorten(value: str, hash_bytes: Optional[int]) -> str:
    """Bare lowercase hex, truncated to ``hash_bytes`` bytes (whole when falsy)."""
    raw = value.lower().removeprefix("0x")
    return raw[: hash_bytes * 2] if hash_bytes else raw


def path_line(
    hops: Sequence[Optional[str]],
    resolve: Callable[[str], Optional[str]] = lambda hop: hop,
    *,
    prefix_bytes: int = 0,
    self_name: Optional[str] = None,
    empty: str = "direct",
    show_hash: bool = False,
    hash_bytes: Optional[int] = None,
    device_hash: Optional[str] = None,
    dim_from: Optional[int] = None,
    hash_as_name: bool = False,
    bare_self: bool = False,
    mode: str = "auto",
) -> PathLine:
    """Build a :class:`PathLine` from raw hop hashes — ``path_text``'s vocabulary.

    The migration bridge: every parameter means exactly what it means to
    :func:`~meshterm.ui.widgets.path_text` (see there for the full rendering rules),
    so a call site swaps builders without changing what it says — a named hop reads
    ``Name`` (annotated ``Name (3d)`` under ``show_hash``), an unnamed hop its
    prefix-lit hash (or its muted identity hash under ``hash_as_name``), ``None`` is
    our own device in white — or, under ``bare_self``, its bit of arrow and nothing
    else — and ``dim_from`` fades a resolved tail. The plain rendering
    is character- and style-identical to ``path_text``; what the swap buys is the
    :class:`PathLine` shapes (ellipsized / wrapped) and the powerline mode.

    Args:
        hops: The hops in propagation order — hex hashes, ``None`` marking our own
            device (empty strings are skipped; ``dim_from`` counts rendered hops).
        resolve: Maps a hop hash to a friendly name when known.
        prefix_bytes: Path-hash width to light in unnamed hops' hashes (0 = none).
        self_name: Our own node's name — white when a resolved name matches it, and
            naming any ``None`` device hop.
        empty: The muted text shown when there are no hops (e.g. ``"direct"``).
        show_hash: Annotate named hops (and, with ``device_hash``, our device) with
            their hash in parentheses — the trace presentation.
        hash_bytes: Truncate shown/annotated hashes to this byte width.
        device_hash: Our own device's key, annotated onto ``None`` hops when
            ``show_hash`` is on.
        dim_from: Fade hops at/after this rendered index (``None`` dims nothing).
        hash_as_name: Present each unnamed hop as its muted identity hash at the
            ``prefix_bytes`` width, annotated with its addressed byte.
        bare_self: Draw our own device (a ``None`` hop) with no words at all — just
            its bit of arrow. For a surface whose route *always* begins and ends on
            us, the name and hash on both ends say nothing the reader doesn't know,
            and cost the cells the hops in between need.
        mode: The :class:`PathLine` mode (``"auto"``/``"powerline"``/``"plain"``).

    Returns:
        The assembled :class:`PathLine`.
    """
    shown = [h for h in hops if h is None or h]
    built: list[PathHop] = []
    for i, hop in enumerate(shown):
        dim = dim_from is not None and i >= dim_from
        if hop is None:
            if bare_self:
                built.append(PathHop("", you=True, dim=dim))
                continue
            note = _shorten(device_hash, hash_bytes) if (show_hash and device_hash) else None
            built.append(
                PathHop(self_name or LOCAL_DEVICE_LABEL, you=True, annotation=note, dim=dim)
            )
            continue
        named = resolve(hop)
        if named and named != hop:
            built.append(PathHop(
                named,
                key=hop,
                you=bool(self_name and named == self_name),
                annotation=_shorten(hop, hash_bytes) if show_hash else None,
                dim=dim,
            ))
            continue
        compact = _shorten(hop, hash_bytes)
        if dim:  # a faded unnamed hop shows its compact hash, exactly as path_text does
            built.append(PathHop(compact, key=hop, dim=True))
        elif hash_as_name:
            identity = _shorten(hop, prefix_bytes or None)
            note = compact if (show_hash and compact and compact != identity) else None
            built.append(PathHop(identity, annotation=note))
        else:
            built.append(PathHop(compact, key=hop, lit_bytes=prefix_bytes))
    return PathLine(built, mode=mode, empty=empty)
