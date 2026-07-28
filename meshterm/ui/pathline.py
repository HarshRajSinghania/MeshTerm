"""Path lines — the flexible hop-sequence text widget (powerline chips or arrows).

This is the successor to :func:`~meshterm.ui.widgets.path_text` as THE way a hop
sequence renders as text — a trace's walked route, a packet's ``via`` chain, a
message's delivery paths, the composer's preview, a trophy row. Where ``path_text``
is one fixed presentation (arrow-joined, one line, overflow left entirely to the
caller), a :class:`PathLine` separates *what the hops are* from *how the line is
drawn*, so every surface the survey found can eventually route through it:

* **Two separator styles.** ``plain`` joins hops with muted ``→`` arrows — today's
  look, unchanged. ``powerline`` renders each hop as a colour-filled chip, seams drawn
  as two solid triangles U+E0B0 — the previous chip tapering out to a point, the next
  one notched inward out of its own fill, a sliver of bare page between them: the gapped
  oh-my-posh look, where a route reads as a row of distinct blocks rather than one
  fused ribbon. The chip fill is the node's hash-derived hue
  (:func:`~meshterm.ui.theme.node_style`), our own node the pure ``you`` white, a
  faded hop dark slate, a keyless hop grey. ``auto`` (the default) picks powerline exactly
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
  reads identically whichever mode drew it. A surface whose route always starts and
  ends on us can ask for ``bare_self``, replacing our name and hash with the app-wide
  ``★`` — the reader already knows who both ends are, and the cells belong to the hops
  that *are* news.

Existing call sites still render through ``path_text``; migrating them here is a
separate, per-surface pass. The composer's insertion cursor rides along as a hop in
its own right (``cursor=True``, drawn as the :data:`CURSOR_GLYPH` slot): an insertion
point *is* a position in the route, not a gap between two of them, so it measures,
wraps and colours like every other hop — the preview keeps whichever mode the terminal
earned, and a cursor can never be stranded on a line break.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from rich.cells import cell_len
from rich.text import Text

from ..core.models import LOCAL_DEVICE_LABEL
from .termfont import powerline_enabled, powerline_full
from .theme import MESH_THEME, node_style

#: The powerline solid right-pointing triangle (U+E0B0) — the only glyph the widget
#: draws, seam and closing edge alike, deliberately from the *core* set so every
#: recommended font qualifies (the rounded caps at U+E0B4+ exist only in full Nerd
#: Font patches). A path only ever flows one way, so the point only ever faces right;
#: what changes is whether it lands on a field (a seam) or on the page (an edge).
POWERLINE_SEP = "\ue0b0"

#: The rounded caps (U+E0B6 opening, U+E0B4 closing) that finish a path's two outer
#: ends as a lozenge. These live in the *extended* block, which only a full Nerd Font
#: patch carries (:func:`~meshterm.ui.termfont.powerline_full`) — a terminal with just
#: the core four squares those ends off instead of showing tofu.
POWERLINE_ROUND_OPEN = ""
POWERLINE_ROUND_CLOSE = ""

#: Our own node, wherever a surface asks for it stripped of name and hash
#: (``bare_self``): the app-wide ``★`` — the same mark the map plants on us.
SELF_GLYPH = "★"

#: The composer's insertion cursor, standing in the route as a hop of its own: the
#: empty slot the next chosen hop drops into. A ``+`` because that is exactly what
#: Enter does there — and because no node is ever named one, so the slot can't be
#: misread as a hop that is already in the route.
CURSOR_GLYPH = "+"

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

#: Extra columns a wrapped line steps in past the hanging indent. The continuation
#: marks (a trailing ``→``, a notched chip edge) say the path goes on; the step says
#: it at a glance, from the shape of the block alone — a route that folds reads as one
#: value that ran long, not as a second value stacked under the first.
WRAP_OFFSET = 2


@dataclass(frozen=True)
class PathHop:
    """One hop of a path line — the semantics, independent of how it is drawn.

    Attributes:
        label: What is shown — a resolved name, a hash standing as the node's
            identity (set ``lit_bytes`` for the two-tone prefix in that case), or the
            bare :data:`SELF_GLYPH` where a surface has no need to name us.
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
        cursor: This hop is an editor's insertion slot rather than a node — the
            :data:`CURSOR_GLYPH` in the app's brand accent, the same colour the list
            cursor (``❯``) below it wears, so the two halves of one gesture (the row
            you pick, the slot it lands in) read as one thing.
    """

    label: str
    key: Optional[str] = None
    you: bool = False
    annotation: Optional[str] = None
    lit_bytes: int = 0
    dim: bool = False
    style: Optional[str] = None
    cursor: bool = False


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

    def text(self) -> Text:
        """The full one-line rendering; overflow is the caller's call, per surface.

        Returns:
            A one-line :class:`Text` (the ``empty`` note when there are no hops).
        """
        if not self._hops:
            return Text(self._empty, style="muted")
        return self._render(self._hops)

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
        continuation starts with ``indent`` spaces — plus :data:`WRAP_OFFSET`, the
        step that makes a fold legible as a fold. Every line fits ``width``.
        Plain-mode lines that
        continue end with the separator's own mark — a trailing ``→`` for an
        arrow-joined path, the bare ``,`` for a comma-joined wire spec — the "path
        goes on" cue; chip lines always close with their pointed edge and, from the
        second line down, open by redrawing the seam the break interrupted — the point
        arriving out of the previous line's last fill — so a continuation is never
        mistakable for a path starting over. A single hop wider than the content column
        stands alone, truncated with an ellipsis.

        Where the breaks fall is chosen for how it *reads* (see :meth:`_flow` and
        :meth:`_turn_seam`), not by cramming each line full: the fold takes the
        fewest lines the hops allow, spread evenly across them, and lands on the
        route's turn — where a leg gives way to its mirrored return — when folding
        there costs no extra line.

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
        # The continuation cue is the separator's own mark, minus the space the next
        # hop would have sat in: " → " reads " →", a wire spec's "," stays ",".
        cue = self._separator.rstrip() if plain else ""
        groups = self._flow(self._hops, budget, plain, len(cue), 0)
        seam = self._turn_seam()
        if seam is not None and len(groups) > 1:
            # The outbound leg's own last line still continues into the return, so it
            # holds the cue back where the whole path's final line doesn't.
            legs = self._flow(self._hops[:seam], budget, plain, len(cue), len(cue))
            legs += self._flow(self._hops[seam:], budget, plain, len(cue), 0, carry_in=True)
            if len(legs) <= len(groups):  # the turn folds for free — take it
                groups = legs

        lines: list[Text] = []
        for i, group in enumerate(groups):
            step = 0 if i == 0 else WRAP_OFFSET
            line = Text() if i == 0 else Text(" " * (indent + step))
            body = self._render(
                group, force_plain=plain,
                carry_in=bool(i), carry_on=i < len(groups) - 1,
            )
            # A lone hop too wide for the column is truncated — leaving room for the
            # cue it still has to carry, so even that line stays inside the width. A
            # column too narrow to hold both drops the cue: content wins the cells.
            continues = i < len(groups) - 1 and 0 < cell_len(cue) < budget
            room = budget - step - (cell_len(cue) if continues else 0)
            if body.cell_len > room:
                body.truncate(room, overflow="ellipsis")
            line.append_text(body)
            if continues:
                line.append(cue, style="muted")
            lines.append(line)
        return lines

    # --- where the breaks fall --------------------------------------------------------

    def _measure(
        self, hops: list[PathHop], plain: bool
    ) -> tuple[list[int], int, int, int, int]:
        """Each hop's own width, plus what joining, opening and closing a line costs.

        Fitting hops to a column is arithmetic once every hop has been measured once —
        which matters because :meth:`_flow` tries a whole series of columns, and the
        screens re-fit on every repaint (a spinner tick included).

        Args:
            hops: The hops to measure, in order.
            plain: Measure as arrow text rather than as chips.

        Returns:
            ``(cells, join, tail, lead, head)`` — each hop's cells, the cells one join
            between two hops costs (chip mode's seam is two: the point tapering out and
            the notch cut into the next chip), the cells a line always closes with (chip
            mode's edge; nothing in arrow mode), the cells every line *after the first*
            opens with (chip mode's notch, the seam's second half redrawn), and the
            cells the *first* line opens with (chip mode's rounded cap, nothing where
            the font has none).
        """
        if plain:
            return [self._plain_hop(hop).cell_len for hop in hops],                 cell_len(self._separator), 0, 0, 0
        widths = [self._chip(hop, self._chip_fill(hop)).cell_len for hop in hops]
        sep = cell_len(POWERLINE_SEP)
        head = cell_len(POWERLINE_ROUND_OPEN) if powerline_full() else 0
        return widths, sep * 2, sep, sep, head

    @staticmethod
    def _fill(
        cells: list[int],
        join: int,
        tail: int,
        budget: int,
        reserve: int,
        last: int,
        lead: int = 0,
        head: int = 0,
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
            lead: Cells every line but the first opens with — the reopened seam plus
                the :data:`WRAP_OFFSET` step it hangs past the indent.
            head: Cells the first line opens with (the rounded cap, when drawn).

        Returns:
            How many hops each line takes; a hop too wide for ``budget`` gets a line of
            its own (the caller truncates it).
        """
        sizes: list[int] = []
        count = 0
        width = head
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
        self,
        hops: list[PathHop],
        budget: int,
        plain: bool,
        reserve: int,
        last: int,
        carry_in: bool = False,
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
            carry_in: These hops are a *later* leg of a path already under way, so
                even their first line opens as a continuation, not as a beginning.

        Returns:
            The hops grouped per line.
        """
        cells, join, tail, lead, head = self._measure(hops, plain)
        lead += WRAP_OFFSET  # every continuation steps in past the hanging indent
        if carry_in:
            head = lead
        lines = len(self._fill(cells, join, tail, budget, reserve, last, lead, head))
        low, high = 1, budget
        while low < high:  # the narrowest column still taking `lines` rows
            mid = (low + high) // 2
            if len(self._fill(cells, join, tail, mid, reserve, last, lead, head)) <= lines:
                high = mid
            else:
                low = mid + 1
        groups: list[list[PathHop]] = []
        at = 0
        for size in self._fill(cells, join, tail, low, reserve, last, lead, head):
            groups.append(hops[at : at + size])
            at += size
        return groups

    def _turn_seam(self) -> Optional[int]:
        """The hop index a fold would read best at — where the route turns — or ``None``.

        Two things mark a turn. A dimmed *tail* is one: the composed outbound leg ends
        and the mirrored return begins (see :attr:`PathHop.dim`). It is read as the run
        of faded hops that reaches the end of the line, not merely the first faded hop —
        a route's opening ``★`` fades too (setting out from us is nobody's choice
        either), and that lone fixture at the head marks nothing. A walked boomerang is
        the other — nothing is dimmed once a trace has actually answered, but the hop
        sequence is its own mirror, so its middle *is* that same turn; folding the plan
        and the walk that answered it in the same place keeps the two readings of one
        route looking like one route. Either way the fold needs real legs on both
        sides — at least two hops each — so a path whose only faded tail is the
        automatic landing back on us never widows it onto a line of its own; that lone
        hop marks no turn, and the walk's own mirror still gets to name one.
        """
        seam: Optional[int] = len(self._hops)
        while seam and self._hops[seam - 1].dim:
            seam -= 1
        if seam == len(self._hops):  # nothing faded at the tail
            seam = None
        if seam is None or len(self._hops) - seam < 2:
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
        force_plain: bool = False,
        carry_in: bool = False,
        carry_on: bool = False,
    ) -> Text:
        """Join ``hops`` in the effective mode.

        The two carry flags mark a wrapped line's ends and only mean anything to
        chips; arrow mode says the same thing with the trailing cue.
        """
        if not force_plain and self._resolved_mode() == "powerline":
            return self._render_chips(hops, carry_in=carry_in, carry_on=carry_on)
        return self._render_plain(hops)

    def _render_plain(self, hops: list[PathHop]) -> Text:
        """Arrow-joined hops — ``path_text``'s presentation, hop by hop."""
        text = Text()
        for i, hop in enumerate(hops):
            if i:
                text.append(self._separator, style="faint" if hop.dim else "muted")
            text.append_text(self._plain_hop(hop))
        return text

    def _plain_hop(self, hop: PathHop) -> Text:
        """One arrow-mode hop: label in its identity style, annotation muted."""
        note_style = "faint" if hop.dim else "muted"
        text = Text()
        if hop.cursor:
            # No chip fill to carry the accent, so the slot takes the app's reverse-video
            # `selected` — the same block the editor's cursor has always been.
            text.append(hop.label, style="selected")
        elif hop.dim:
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

    def _render_chips(
        self, hops: list[PathHop], *, carry_in: bool = False, carry_on: bool = False
    ) -> Text:
        """Powerline chips: each hop filled with its hue, a two-cell gap at every seam.

        The two outer ends say whether this line *is* the path or only part of it. A
        line that opens the path opens rounded (squared off where the font has no
        rounded caps); one that continues a wrapped path opens on the break's other
        half — which is exactly the seam's own second cell, so a fold looks like the
        seam it interrupted. A line that ends the path closes rounded; one the path
        outruns closes on the point, the same "goes on" cue arrow mode spells with a
        trailing ``→``.

        Args:
            hops: The hops of this one line, in order.
            carry_in: This line continues a wrapped path (it does not open one).
            carry_on: The path continues past this line (it does not end here).
        """
        fills = [self._chip_fill(hop) for hop in hops]
        rounded = powerline_full()
        text = Text()
        if carry_in:
            text.append_text(self._notch(fills[0]))  # the break's other half
        elif rounded:
            text.append(POWERLINE_ROUND_OPEN, style=fills[0])
        for i, hop in enumerate(hops):
            if i:
                text.append_text(self._seam(fills[i - 1], fills[i]))
            text.append_text(self._chip(hop, fills[i]))
        close = POWERLINE_SEP if carry_on or not rounded else POWERLINE_ROUND_CLOSE
        text.append(close, style=fills[-1])  # the edge into the page: pointed or round
        return text

    @classmethod
    def _seam(cls, before: str, after: str) -> Text:
        """The two cells between two chips: a point tapering out, a notch cut back in.

        The previous chip's point carries *no* background, so the page shows through the
        wedge it tapers into; the next chip's left edge then opens on the same point in
        the page's own colour, notched inward out of its fill. Both faces angle the way
        the path flows, and between them sits a sliver of bare page — the small gap
        oh-my-posh leaves between its segments.

        The alternative (one cell, the previous fill interlocked *on* the next) packs
        the row tighter but fuses the route into a continuous ribbon, and two neighbours
        that happen to land on the same fill — a hue collision, a run of faded hops, two
        keyless greys — then read as a single block where the route has two nodes. One
        gap, every seam: a chip is always a chip.

        Args:
            before: The fill the seam tapers out of (the previous chip's).
            after: The fill the seam notches back into (the next chip's).

        Returns:
            The seam's two cells, styled.
        """
        text = Text()
        text.append(POWERLINE_SEP, style=before)
        text.append_text(cls._notch(after))
        return text

    @staticmethod
    def _notch(fill: str) -> Text:
        """A chip's left edge: the point cut inward out of its own fill.

        Reverse video paints the notch in the terminal's own background — the only way
        to name the page's colour without knowing it — so nothing of whatever sits left
        of the chip (the previous chip's taper, the line above a wrapped break) bleeds
        into it, and the point still faces the way the path flows.

        Args:
            fill: The chip's fill colour, which the notch is cut out of.

        Returns:
            The one styled cell.
        """
        text = Text()
        text.append(POWERLINE_SEP, style=f"{fill} reverse")
        return text

    def _chip_fill(self, hop: PathHop) -> str:
        """A chip's fill: cursor accent, override, dim slate, white you, hue, grey.

        The insertion slot outranks everything — it is the one chip that isn't a node,
        and it wears the brand accent so it reads as chrome among identities rather than
        as a hop with an unlucky hue. The fade comes next, outranking identity including
        our own. A dimmed hop is one nobody composed (an automatic landing back home, a
        mirrored return leg), and the white ``you`` chip is the loudest thing on the
        line: our automatic end must recede with the rest of the automatic half, not
        shout over the hops that *are* news. Plain mode says the same thing by fading
        the name.
        """
        if hop.cursor:
            return _style_hex("brand") or _KEYLESS_BG
        if hop.style:
            resolved = _style_hex(hop.style)
            if resolved:
                return resolved
        if hop.dim:
            return _DIM_BG
        if hop.you:
            return _YOU_BG
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
    cursor: Optional[int] = None,
    mode: str = "auto",
) -> PathLine:
    """Build a :class:`PathLine` from raw hop hashes — ``path_text``'s vocabulary.

    The migration bridge: every parameter means exactly what it means to
    :func:`~meshterm.ui.widgets.path_text` (see there for the full rendering rules),
    so a call site swaps builders without changing what it says — a named hop reads
    ``Name`` (annotated ``Name (3d)`` under ``show_hash``), an unnamed hop its
    prefix-lit hash (or its muted identity hash under ``hash_as_name``), ``None`` is
    our own device in white — or, under ``bare_self``, the lone ``★`` — and
    ``dim_from`` fades a resolved tail. The plain rendering
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
        bare_self: Draw our own device (a ``None`` hop) as the bare
            :data:`SELF_GLYPH`. For a surface whose route *always* begins and ends on
            us, the name and hash on both ends say nothing the reader doesn't know,
            and cost the cells the hops in between need. Both stars also fade: setting
            out from us and coming home to us are fixtures of every such route, no more
            composed — or removable — than a mirrored return leg is, so they wear the
            same automatic grey.
        cursor: Splice an editor's insertion slot (:data:`CURSOR_GLYPH`) in *at* this
            rendered-hop index — the position a chosen hop would take, so index ``0``
            opens the route and ``len`` closes it. Counted over rendered hops exactly
            as ``dim_from`` is, and applied after them, so a slot never shifts what a
            hop shows or fades. ``None`` draws no cursor.
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
                # Both our ends are fixed: a route leaves us and comes home to us, and
                # neither end is anybody's to compose or remove. So the stars fade like
                # every other automatic hop — the colour on the line belongs to the
                # nodes actually being chosen.
                built.append(PathHop(SELF_GLYPH, you=True, dim=True))
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
    if cursor is not None:
        built.insert(max(0, min(cursor, len(built))), PathHop(CURSOR_GLYPH, cursor=True))
    return PathLine(built, mode=mode, empty=empty)
