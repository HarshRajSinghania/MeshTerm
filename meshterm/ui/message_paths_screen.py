"""The Message paths dialog: every way one chat message reached this radio.

The interactive face of :mod:`~meshterm.services.message_paths`, opened from the chat
with ``^P`` (or Enter on a picked direct message). The evidence — one logged frame per
arrival, each with the relay path it rode — is laid out twice, so the shape and the
detail read together:

* a **graph** up top draws every distinct path the message took, in the atlas's edge
  style: braille lines, the final relay → us link coloured by that path's reception
  SNR (green → amber → red), earlier links slate (their SNR was never ours to
  measure). The origin sits at the left, we sit at the right, every relay in between
  gets a marker — but only the nodes on the *currently selected* path are labelled,
  so a many-path graph stays readable. The selected path draws bright; the rest dim.
* the **arrival list** beneath is one row per logged copy — time, reception SNR, and
  the relay chain through the shared compact path widget. ↑↓ move the selection (the
  graph's highlight follows); a row longer than the dialog **scrolls horizontally
  with ←→**, the whole line shifting under a ``…`` at whichever edge continues, and
  snaps back the moment the selection moves on.

Nothing here transmits; like the service beneath it, this is a read-model over what
the radio already heard.
"""

from __future__ import annotations

from statistics import median
from typing import Optional

from rich.text import Text

from ..core.models import ChatMessage
from ..services.message_paths import Arrival
from .atlas_screen import _NO_READING, _UNKNOWN, _scaled, _snr_rgb
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
_LANE_STEP_DOTS = 8
_GRAPH_MIN_H = 3
_GRAPH_MAX_H = 10

#: Dot-space margin the endpoint markers keep from the canvas edges.
_GRAPH_PAD_DOTS = 6

#: The widest a node label may render on the graph before it is ellipsized.
_GRAPH_LABEL_W = 12


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
        caption = Text("origin → you · edge = last-hop SNR · bright = selected path",
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
        """One arrival, unabridged: time, reception SNR, and its relay path."""
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
                empty="direct",
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

    def _path_snr(self, path: tuple[str, ...]) -> Optional[float]:
        """The median reception SNR across the arrivals that rode ``path``."""
        readings = [
            a.snr for a in self._arrivals if a.hops == path and a.snr is not None
        ]
        return median(readings) if readings else None

    def _graph_lines(self, width: int) -> list[str]:
        """Draw every distinct path origin → us, the selected one bright and labelled.

        Lanes fan out from the vertical centre (first path on it, the next below,
        the next above, …); a node shared between paths averages its lanes, so a
        common relay pulls the paths together where they actually met.
        """
        paths = self._paths()
        selected = self._arrivals[self._index].hops
        offsets = [(-1) ** k * ((k + 1) // 2) for k in range(len(paths))]  # 0 -1 1 -2 2…
        max_off = max(abs(o) for o in offsets)
        step = _LANE_STEP_DOTS
        rows = max(_GRAPH_MIN_H, (2 * max_off * step + 14) // 4)
        if rows > _GRAPH_MAX_H:
            rows = _GRAPH_MAX_H
            step = max(4, (rows * 4 - 14) // (2 * max_off))
        canvas = MapCanvas(width, rows)
        dot_w, dot_h = width * 2, rows * 4
        cy = dot_h // 2
        span = dot_w - 2 * _GRAPH_PAD_DOTS

        # Positions: x by mean relative slot along the paths through a node, y by
        # mean lane — endpoints pinned to the margins and the centre line.
        rel: dict[str, list[float]] = {}
        lanes: dict[str, list[int]] = {}
        seqs = [(_SRC, *path, _DST) for path in paths]
        for off, seq in zip(offsets, seqs):
            hops = len(seq) - 1
            for i, node in enumerate(seq):
                rel.setdefault(node, []).append(i / hops)
                lanes.setdefault(node, []).append(off)

        def pos(node: str) -> tuple[int, int]:
            x = _GRAPH_PAD_DOTS + round(
                sum(rel[node]) / len(rel[node]) * span
            )
            if node in (_SRC, _DST):
                return x, cy
            return x, cy + round(sum(lanes[node]) / len(lanes[node]) * step)

        # Edges, the selected path drawn last and bright so shared cells go to it.
        ordered = sorted(zip(paths, seqs), key=lambda ps: ps[0] == selected)
        for path, seq in ordered:
            bright = 1.0 if path == selected else 0.55
            priority = 3 if path == selected else 2
            snr_rgb = _snr_rgb(self._path_snr(path))
            for i in range(len(seq) - 1):
                last = i == len(seq) - 2  # the final relay → us link owns the SNR
                color = _scaled(snr_rgb if last else _NO_READING, bright)
                canvas.draw_line([pos(seq[i]), pos(seq[i + 1])], color, priority)

        # Markers for every node; labels only along the selected path (endpoints
        # first, so they win label collisions against mid-path relays).
        for node in rel:
            glyph, color_hex = self._node_glyph(node)
            canvas.marker(*pos(node), glyph, parse_hex(color_hex))
        for node in (_DST, _SRC, *selected):
            label = self._node_label(node)
            if len(label) > _GRAPH_LABEL_W:
                label = label[: _GRAPH_LABEL_W - 1] + "…"
            rgb = (
                (255, 255, 255)
                if node == _DST
                else parse_hex(self._node_glyph(node)[1])
            )
            x, y = pos(node)
            for dy in (0, 4, -4):
                if canvas.marker_label(x, y + dy, label, rgb):
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
        """A node's graph label: its name when known, else its short hash."""
        if node == _DST:
            return self._self_name or "you"
        if node == _SRC:
            return self._source or "?"
        named = self._resolve(node)
        return named if named and named != node else node[:8]
