"""The Message paths dialog: every way one chat message reached this radio.

The interactive face of :mod:`~meshterm.services.message_paths`, opened from the chat
with ``^P`` (or Enter on a picked direct message). The evidence — one logged frame per
arrival, each with the relay path it rode — is laid out twice, so the shape and the
detail read together:

* a **graph** up top draws every distinct path the message took as braille lines:
  the currently selected path white, the unused paths gray beneath it. The origin
  sits at the left, we sit at the right, and every relay in between gets its marker
  plus the first byte of its hash in the marker's colour — the row list carries the
  names, so the graph's labels stay two cells wide and a many-path graph stays
  readable. Distinct depths spread evenly across the width, so forks read clearly
  however lopsided the paths' lengths are, and each label first hunts for a spot
  clear of the drawn lines before settling for one that overprints them.
* the **arrival list** beneath is one row per logged copy — time, reception SNR, and
  the relay chain through the shared compact path widget, each named hop annotated
  with the hash byte it is addressed by (``YUL-Poly (3d)``, the trace presentation).
  ↑↓ move the selection (the graph's highlight follows); a row longer than the
  dialog **scrolls horizontally with ←→**, the whole line shifting under a ``…`` at
  whichever edge continues, and snaps back the moment the selection moves on.

Nothing here transmits; like the service beneath it, this is a read-model over what
the radio already heard.
"""

from __future__ import annotations

from math import ceil
from typing import Optional

from rich.text import Text

from ..core.models import ChatMessage
from ..services.message_paths import Arrival
from .atlas_screen import _UNKNOWN
from .map_render import _NODE, _SELF
from .mapcanvas import MapCanvas, parse_hex
from .theme import snr_style
from .tui.render import crop_cells, render_to_ansi
from .tui.screen import Screen
from .widgets import NodeResolver, path_text

#: Sentinel node ids for the graph's endpoints (NUL never collides with hex hops).
_SRC = "\x00src"
_DST = "\x00dst"

#: Cells one ←/→ press shifts the selected row by.
_HSTEP = 4

#: Vertical dot separation between path lanes, and the graph's height bounds in rows.
_LANE_STEP_DOTS = 10
_GRAPH_MIN_H = 5
_GRAPH_MAX_H = 14

#: Dot rows the graph keeps as breathing room around the lane fan when sizing itself.
_GRAPH_SLACK_DOTS = 18

#: Dot-space margin the endpoint markers keep from the canvas edges.
_GRAPH_PAD_DOTS = 6

#: The widest a node label may render on the graph before it is ellipsized.
_GRAPH_LABEL_W = 12

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
    ) -> None:
        """Build the dialog over one message's matched arrivals.

        Args:
            message: The chat message whose delivery evidence is shown.
            arrivals: Its logged arrivals, oldest first (may be empty).
            matched: Whether the arrivals were matched by content (a decrypted
                channel frame) rather than merely by time — the summary line warns
                when they weren't.
            resolve: Maps a hop hash to a friendly name when known.
            prefix_bytes: Path-hash width to light in unnamed hops' hashes.
            self_name: Our own node's name (the graph's right endpoint, white).
            summary: The one-line evidence summary shown under the quoted text.
            source: Display name of the message's origin — the sender parsed from a
                channel message, us for an outbound one, the peer for a direct chat
                (``None`` reads as an unknown ``?`` origin).
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

        lines.append("")
        lines.extend(self._graph_lines(width))
        caption = Text("origin → you · white = selected path · labels = hash byte",
                       style="faint")
        lines.append(render_to_ansi(caption, width, no_wrap=True))
        lines.append("")
        for i, arrival in enumerate(self._arrivals):
            if i == self._index:
                self._cursor = len(lines)
                lines.append(self._selected_line(arrival, width))
            else:
                row = Text("  ")
                row.append_text(self._row_text(arrival))
                lines.append(render_to_ansi(row, width, no_wrap=True))
        self._scroll_total = len(lines)
        return lines

    # -- the rows --

    def _row_text(self, arrival: Arrival) -> Text:
        """One arrival, unabridged: time, reception SNR, and its relay path.

        Hops carry the trace presentation at the graph's grain: each named hop is
        annotated with the hash byte it is addressed by (``YUL-Poly (3d)``), unnamed
        hops show that byte bare, so the rows and the graph's labels cross-reference.
        """
        row = Text()
        row.append(arrival.when.astimezone().strftime("%H:%M:%S"), style="muted")
        row.append("  ")
        if arrival.snr is not None:
            row.append(f"{arrival.snr:+5.1f} dB", style=snr_style(arrival.snr))
        else:
            row.append(" " * 8)
        row.append("  ")
        if arrival.hops:
            row.append("via ", style="muted")
        row.append_text(
            path_text(
                arrival.hops, self._resolve,
                prefix_bytes=self._prefix_bytes, self_name=self._self_name,
                empty="direct", show_hash=True, hash_bytes=1,
            )
        )
        if arrival.resend:
            row.append(f"  (resend #{arrival.resend})", style="muted")
        return row

    def _selected_line(self, arrival: Arrival, width: int) -> str:
        """The highlighted row: ``❯`` pointer, shifted by ``←→`` under edge ``…``.

        The whole line scrolls — timestamp and SNR included — and the shift bound
        is measured here against the current width, so a resize can only ever leave
        the row clamped back into range.
        """
        full = self._row_text(arrival)
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

        Lanes fan out from the vertical centre (first path on it, the next below,
        the next above, …); a node shared between paths averages its lanes, so a
        common relay pulls the paths together where they actually met. Columns are
        the distinct depths, spread evenly across the width, so forks stay clear
        however lopsided the paths' lengths are.
        """
        paths = self._paths()
        selected = self._arrivals[self._index].hops
        # Lanes go to the paths that have relays (a direct path rides the centre
        # line anyway), fanning out from the middle: 0, -1, 1, -2, 2…
        relayed = [path for path in paths if path]
        lane_of = {path: (-1) ** k * ((k + 1) // 2) for k, path in enumerate(relayed)}

        # Positions: a node's depth is its mean relative slot along the paths
        # through it; the distinct depths then map to evenly spaced columns
        # (endpoints pinned to the margins). y is the mean lane, centre-pinned
        # for the endpoints — a shared relay averages toward the middle.
        rel: dict[str, list[float]] = {}
        lanes: dict[str, list[int]] = {}
        seqs = [(_SRC, *path, _DST) for path in paths]
        for path, seq in zip(paths, seqs):
            hops = len(seq) - 1
            for i, node in enumerate(seq):
                rel.setdefault(node, []).append(i / hops)
                lanes.setdefault(node, []).append(lane_of.get(path, 0))
        depth = {node: sum(r) / len(r) for node, r in rel.items()}
        columns = sorted(set(depth.values()))
        slot = {d: i / max(1, len(columns) - 1) for i, d in enumerate(columns)}
        lane_y = {
            node: sum(l) / len(l)
            for node, l in lanes.items()
            if node not in (_SRC, _DST)
        }

        # Height follows where the relays actually land after lane-averaging, so
        # the box spends its rows on the fan, not on empty lanes.
        step = _LANE_STEP_DOTS
        reach = max((abs(y) for y in lane_y.values()), default=0.0)
        rows = max(_GRAPH_MIN_H, ceil((2 * reach * step + _GRAPH_SLACK_DOTS) / 4))
        if rows > _GRAPH_MAX_H:
            rows = _GRAPH_MAX_H
            step = max(4, int((rows * 4 - _GRAPH_SLACK_DOTS) / (2 * reach)))
        canvas = MapCanvas(width, rows)
        dot_w, dot_h = width * 2, rows * 4
        cy = dot_h // 2
        span = dot_w - 2 * _GRAPH_PAD_DOTS

        def pos(node: str) -> tuple[int, int]:
            x = _GRAPH_PAD_DOTS + round(slot[depth[node]] * span)
            if node in (_SRC, _DST):
                return x, cy
            return x, cy + round(lane_y[node] * step)

        # Edges: the selected path white and drawn last so shared cells go to it;
        # the unused paths sit gray beneath.
        ordered = sorted(zip(paths, seqs), key=lambda ps: ps[0] == selected)
        for path, seq in ordered:
            color = _EDGE_SELECTED if path == selected else _EDGE_UNUSED
            priority = 3 if path == selected else 2
            for i in range(len(seq) - 1):
                canvas.draw_line([pos(seq[i]), pos(seq[i + 1])], color, priority)

        # Markers for every node, each labelled — endpoints by name, relays by the
        # first byte of their hash in the marker's colour. Endpoints then the
        # selected path go first so they win collisions; every label first sweeps
        # for a spot clear of the drawn edges, then settles for any free cell.
        for node in rel:
            glyph, color_hex = self._node_glyph(node)
            canvas.marker(*pos(node), glyph, parse_hex(color_hex))
        labelled = [_DST, _SRC]
        for hop in (*selected, *(h for path in paths for h in path)):
            if hop not in labelled:
                labelled.append(hop)
        for node in labelled:
            label = self._node_label(node)
            if len(label) > _GRAPH_LABEL_W:
                label = label[: _GRAPH_LABEL_W - 1] + "…"
            rgb = (
                (255, 255, 255)
                if node == _DST
                else parse_hex(self._node_glyph(node)[1])
            )
            x, y = pos(node)
            spots = [(x, y + dy) for dy in (0, 4, -4)]
            placed = False
            for sx, sy in spots:
                if canvas.marker_label(sx, sy, label, rgb, avoid_dots=True):
                    placed = True
                    break
            if not placed:
                for sx, sy in spots:
                    if canvas.marker_label(sx, sy, label, rgb):
                        break
        return canvas.to_ansi_lines()

    def _node_glyph(self, node: str) -> tuple[str, str]:
        """The graph marker for a node: us a star, named nodes dots, unknowns rings."""
        if node == _DST:
            return _SELF
        if node == _SRC:
            if self._source and self._self_name and self._source == self._self_name:
                return _SELF
            return _NODE if self._source else _UNKNOWN
        named = self._resolve(node)
        return _NODE if named and named != node else _UNKNOWN

    def _node_label(self, node: str) -> str:
        """A node's graph label: endpoints by name, relays by their first hash byte."""
        if node == _DST:
            return self._self_name or "you"
        if node == _SRC:
            return self._source or "?"
        return node[:2]
