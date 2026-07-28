"""The Message paths dialog: every way one chat message reached this radio.

The interactive face of :mod:`~meshterm.services.message_paths`, opened from the chat
with ``^P`` (or Enter on a picked direct message). The evidence — one logged frame per
arrival, each with the relay path it rode — is laid out twice, so the shape and the
detail read together:

* a **graph** up top draws every distinct path the message took through the shared
  route-graph widget (:mod:`~meshterm.ui.pathgraph`): the currently selected path
  white, the unused paths gray beneath it. The origin sits at the left, we sit at the
  right, and every relay in between gets its own node-type marker (``▲`` repeater, …)
  plus the first byte of its hash — set straight above or below the marker, in the mesh
  name's own colour, so the byte reads as that node and never crowds the line running
  through it. A node-type key sits under the graph. The row list carries the full names,
  so the graph's labels stay two cells wide and a many-path graph stays readable.
* the **arrival list** beneath is two lines per logged copy. On top, alone on its lane
  and introduced by nothing, the **route** as a path line (:mod:`~meshterm.ui.pathline`):
  each relay a chip in its own node hue, named where we know the node (``YUL-Poly``) and
  standing in its own hash at the device's path-hash width where we don't (``e839f2``,
  keyless grey). No hash is repeated after a name — the graph above is where the hash
  bytes live, and a chip and its label share the node's colour, so the two halves
  cross-reference by hue instead of by spelling the hex twice. Under it, hanging muted,
  the frame's own facts: time heard, reception SNR, and which resend it was.
  The route is the line you pick — it is what one arrival differs from another by, and
  what the graph highlights. ↑↓ move the selection (the graph's highlight follows); a
  route longer than the dialog **scrolls horizontally with ←→**, the whole line shifting
  under a ``…`` at whichever edge continues, and snaps back the moment the selection
  moves on.

Nothing here transmits; like the service beneath it, this is a read-model over what
the radio already heard.
"""

from __future__ import annotations

from typing import Optional

from rich.text import Text

from ..core.models import ChatMessage
from ..services.message_paths import Arrival
from .pathgraph import PathLayer, render_path_graph
from .pathline import path_line
from .theme import snr_style
from .tui.render import crop_cells, render_to_ansi
from .tui.screen import Screen
from .widgets import (
    NameKeyResolver,
    NodeResolver,
    TypeOf,
    node_type_legend,
    route_graph_style,
)

#: Cells one ←/→ press shifts the selected row by.
_HSTEP = 4

#: Columns the reception-facts line hangs in under the route it belongs to — past the
#: ``❯ `` pointer lane, so the pair reads as one arrival with its detail tucked under it.
_DETAIL_INDENT = 4

#: Edge colours: the selected path draws white over the unused paths' gray.
_EDGE_SELECTED = (255, 255, 255)
_EDGE_UNUSED = (110, 110, 110)


class MessagePathsScreen(Screen):
    """A floating dialog: one message's arrivals as a path graph over a row list."""

    def __init__(
        self,
        message: ChatMessage,
        arrivals: list[Arrival],
        *,
        matched: bool,
        resolve: NodeResolver,
        prefix_bytes: int,
        self_name: Optional[str],
        summary: str,
        source: Optional[str] = None,
        type_of: Optional[TypeOf] = None,
        key_of: Optional["NameKeyResolver"] = None,
    ) -> None:
        """Build the dialog over one message's matched arrivals.

        Args:
            message: The chat message whose delivery evidence is shown.
            arrivals: Its logged arrivals, oldest first (may be empty).
            matched: Whether the arrivals were matched by content (a decrypted
                channel frame) rather than merely by time — the summary line warns
                when they weren't.
            resolve: Maps a hop hash to a friendly name when known.
            prefix_bytes: The device's path-hash width — how many bytes of an unnamed
                hop's hash stand in for its (unknown) name.
            self_name: Our own node's name (the graph's right endpoint, white).
            summary: The one-line evidence summary shown under the quoted text.
            source: Display name of the message's origin — the sender parsed from a
                channel message, us for an outbound one, the peer for a direct chat
                (``None`` reads as an unknown ``?`` origin).
            type_of: Maps a relay hash to its node type, so the graph marks a repeater
                ``▲`` (etc.) instead of a generic dot; ``None`` keeps the plain dots.
            key_of: Maps the origin's display name back to its node's key, so the
                graph's left endpoint takes its key-derived hue; ``None`` (or an
                unresolvable name) leaves it muted.
        """
        super().__init__()
        self.title = "Message paths"
        self._message = message
        self._arrivals = arrivals
        self._matched = matched
        self._resolve = resolve
        self._prefix_bytes = prefix_bytes
        self._self_name = self_name
        self._summary = summary
        self._source = source
        self._type_of = type_of
        self._key_of = key_of
        self._index = 0
        #: Cells the selected row is shifted left by (reset whenever ↑↓ move), and
        #: how far it *can* shift, measured against the width of the last render.
        self._hshift = 0
        self._hmax = 0
        self._cursor: Optional[int] = None

    # --- input ---------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Row keys while there are rows; a bare close hint otherwise."""
        if self._arrivals:
            return "↑↓ move · ←→ scroll line · Esc close"
        return "Esc close"

    def handle(self, action: str, data: str = "") -> None:
        """Move the selection, shift the selected line sideways, or dismiss."""
        rows = len(self._arrivals)
        if action == "up" and rows:
            self._index = (self._index - 1) % rows
            self._hshift = 0
        elif action == "down" and rows:
            self._index = (self._index + 1) % rows
            self._hshift = 0
        elif action == "pageup" and rows:
            self._index = max(0, self._index - self._page_step)
            self._hshift = 0
        elif action in ("pagedown", "space") and rows:
            self._index = min(rows - 1, self._index + self._page_step)
            self._hshift = 0
        elif action in ("home", "ctrl_home") and rows:
            self._index = 0
            self._hshift = 0
        elif action in ("end", "ctrl_end") and rows:
            self._index = rows - 1
            self._hshift = 0
        elif action == "left":
            self._hshift = max(0, self._hshift - _HSTEP)
        elif action == "right":
            self._hshift = min(self._hmax, self._hshift + _HSTEP)
        elif action in ("escape", "enter"):
            self.resolve(None)

    def cursor_line(self) -> Optional[int]:
        """The selected row, so a tall arrival list scrolls with the selection."""
        return self._cursor

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """The quoted message and summary, the path graph, then the arrival rows."""
        quoted = self._message.text.replace("\n", " ")
        if len(quoted) > 64:
            quoted = quoted[:63] + "…"
        stamp = Text(self._message.created_at.astimezone().strftime("%b %d %H:%M"),
                     style="muted")
        stamp.append("  ·  ", style="muted")
        stamp.append(self._summary, style="muted" if self._matched else "warn")

        lines = [
            render_to_ansi(Text(f"“{quoted}”"), width, no_wrap=True),
            render_to_ansi(stamp, width, no_wrap=True),
        ]
        self._cursor = None
        if not self._arrivals:
            lines.append("")
            note = (
                "Nothing overheard — the radio only logs frames it hears while "
                "MeshTerm is listening."
                if self._matched
                else "No direct-message frames logged in the window."
            )
            lines.append(render_to_ansi(Text(note, style="muted"), width))
            self._scroll_total = len(lines)
            return lines

        # No blank line above the graph: its canvas already opens with air over the
        # topmost lane, so a spacer here would read as two rows of margin.
        lines.extend(self._graph_lines(width))
        caption = Text("origin → you · white = selected path · labels = hash byte",
                       style="faint")
        lines.append(render_to_ansi(caption, width, no_wrap=True))
        lines.append(render_to_ansi(node_type_legend(), width, no_wrap=True))
        lines.append("")
        for i, arrival in enumerate(self._arrivals):
            if i == self._index:
                self._cursor = len(lines)
                lines.append(self._selected_line(arrival, width))
            else:
                row = Text("  ")
                row.append_text(self._path_text(arrival))
                lines.append(render_to_ansi(row, width, no_wrap=True))
            detail = Text(" " * _DETAIL_INDENT)
            detail.append_text(self._detail_text(arrival))
            lines.append(render_to_ansi(detail, width, no_wrap=True))
        self._scroll_total = len(lines)
        return lines

    # -- the rows --

    def _path_text(self, arrival: Arrival) -> Text:
        """One arrival's relay chain — the row's first line, and the one you pick.

        The route gets the row's full width to itself, so no ``via`` introduces it: on a
        lane that holds nothing else there is nothing to tell it apart from. It is THE
        path line, so a route here is drawn exactly as a route anywhere else — chips in
        each node's own hue where the terminal can draw them, arrows where it can't.
        Hops read as names, nothing else: an unnamed hop, having no name to show, stands
        in its own hash at the device's path-hash width (muted grey — colour is the
        "this is a name" signal), and no hop repeats its hash after its name. The hash
        bytes are the graph's job, one row up; what ties the two together is the shared
        node hue, not a second spelling of the hex.
        """
        return path_line(
            arrival.hops, self._resolve,
            prefix_bytes=self._prefix_bytes, self_name=self._self_name,
            empty="direct", hash_as_name=True,
        ).text()

    def _detail_text(self, arrival: Arrival) -> Text:
        """One arrival's reception facts — when it landed, how loudly, which copy.

        The row's second line, hanging muted under the route it describes: the path is
        what distinguishes one arrival from another (and what the graph above draws), so
        it takes the lane, and the frame's own particulars step in beneath it.
        """
        text = Text(arrival.when.astimezone().strftime("%H:%M:%S"), style="muted")
        if arrival.snr is not None:
            text.append("  ")
            text.append(f"{arrival.snr:+.1f} dB", style=snr_style(arrival.snr))
        if arrival.resend:
            text.append(f"  ·  resend #{arrival.resend}", style="muted")
        return text

    def _selected_line(self, arrival: Arrival, width: int) -> str:
        """The highlighted path: ``❯`` pointer, shifted by ``←→`` under edge ``…``.

        The picked line is the route — the one thing here long enough to need scrolling,
        and the one the graph above highlights — and the shift bound is measured here
        against the current width, so a resize can only ever leave the row clamped back
        into range.
        """
        full = self._path_text(arrival)
        avail = max(1, width - 2)
        self._hmax = max(0, full.cell_len - avail)
        self._hshift = min(self._hshift, self._hmax)
        shift = self._hshift
        left_more = 1 if shift > 0 else 0
        right_more = 1 if shift + avail < full.cell_len else 0
        inner = max(1, avail - left_more - right_more)
        line = Text("❯ ", style="brand", no_wrap=True)
        if left_more:
            line.append("…", style="muted")
        line.append_text(crop_cells(full, shift + left_more, inner))
        if right_more:
            line.append("…", style="muted")
        line.style = "brand"
        return render_to_ansi(line, width, no_wrap=True)

    # -- the graph --

    def _paths(self) -> list[tuple[str, ...]]:
        """The distinct relay paths across the arrivals, in first-heard order."""
        seen: list[tuple[str, ...]] = []
        for arrival in self._arrivals:
            if arrival.hops not in seen:
                seen.append(arrival.hops)
        return seen

    def _graph_lines(self, width: int) -> list[str]:
        """Draw every distinct path origin → us, the selected one white over gray.

        The shared route-graph widget does the layout (a left-to-right flow of paths that
        diverge and converge, each route its own lane and each relay drawn once); this just
        maps each distinct path to a
        :class:`~meshterm.ui.pathgraph.PathLayer` — the selected one white on top, the
        rest gray beneath — and hands it the shared route-graph callbacks
        (:func:`~meshterm.ui.widgets.route_graph_style`): endpoints named, relays their
        own map marker where the type is known, each labelled by its first hash byte in
        the node's own hue.

        Layout rank is fixed by first-heard order (the first path heard is always the spine),
        exactly as the node detail's Routes tab fixes it by evidence order, so the fan's
        geometry holds still as ↑↓ walk the arrivals — only the emphasis (which lane draws
        white and on top) follows the pick.
        """
        selected = self._arrivals[self._index].hops
        paths = self._paths()
        layers = [
            PathLayer(
                hops=path,
                color=_EDGE_SELECTED if path == selected else _EDGE_UNUSED,
                priority=len(paths) - i,
                emphasis=1 if path == selected else 0,
            )
            for i, path in enumerate(paths)
        ]
        glyph_of, label_of, label_rgb_of = route_graph_style(
            resolve=self._resolve, self_name=self._self_name, source=self._source,
            type_of=self._type_of, key_of=self._key_of,
        )
        return render_path_graph(
            layers, width,
            glyph_of=glyph_of, label_of=label_of, label_rgb_of=label_rgb_of,
        )
