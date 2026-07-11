"""The trace path composer: build a forced route hop by hop, guided by observed links.

A floating dialog over a live trace screen. The route under construction reads across
the top — ``us → hop → … → us`` — and beneath it sits a suggestion list: the nodes the
topology evidence says the path's current tail can hear, strongest observed link first
(see :meth:`~meshterm.services.topology.MeshTopology.next_hops`). Every link is
bidirectional evidence, so a path that was ever *received* through two nodes proposes
that link in either direction.

The composer serves both trace features, and which one owns it decides how the route's
return half is written (the trace protocol has no separate return-path field — the spec
sent to the device is one walk that ends within our earshot):

* **Target mode** (*Trace target* — a ``target_id`` is pinned): only the outbound leg
  is composed. The target stays pinned as the turning point and the return is the
  outbound hops mirrored — the preview resolves and dims that half, since it isn't
  yours to choose. An *Auto* action hands routing back to the device.
* **Path mode** (*Trace path* — no target at all): the whole walk is yours, hop by
  hop, until the route comes back within earshot of us — only the final landing on
  our own node is filled in (and dimmed). The preview opens as just ``us → us``.
  There is no *Auto* action, because with no destination there is nothing for the
  device to route to.

Interaction, following the reorder screen's cursor-over-rows-and-actions pattern:

* ↑/↓ move over suggestions and the action rows; Enter on a suggestion appends it.
* Typing filters the suggestions by name or hash — and when the typed text is itself
  even-length hex, an *add custom hop* row appears, so a node we have never observed
  (or a bare hash from another tool) can be forced into the route.
* Backspace erases the filter first; with the filter empty it removes the last hop.
* When the path's tail is a repeater we hold admin credentials for, a *fetch
  neighbours* row asks that repeater over the mesh for its own neighbour table —
  fresh second-vantage evidence exactly where composing ran out of it. The dialog
  resolves :class:`FetchNeighbours` and the owning flow fetches, refreshes the
  topology, and reopens the composer mid-thought (hops preserved).
* Enter on **Use this path** commits the composed spec; Esc cancels with no change.

Nodes render exactly as the trace screen's route line does — ``Name (hash)`` with the
name in the brand colour and the hash muted in parentheses — so a node reads the same
wherever it appears.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rich.cells import cell_len
from rich.text import Text

from ..services.topology import MeshTopology, _is_hex, render_custom_spec, render_forced_spec
from .theme import snr_style
from .tui.render import render_lines, render_to_ansi
from .tui.screen import Screen
from .widgets import _age_seconds, _format_age, _shorten_hash

#: Sentinel spec meaning "no forced path — let the device route" (the trace screen's
#: empty-spec convention). Only meaningful in target mode; a path walk has no target
#: for the device to route to, so path mode never resolves it.
AUTO_SPEC = ""

#: How many suggestions to show at most; beyond this the evidence is too weak to matter
#: and the dialog would outgrow the screen.
_MAX_SUGGESTIONS = 12

#: Action-row sentinels (kept distinct from suggestion rows, which carry node ids).
_USE = "use"
_AUTO = "auto"
_CANCEL = "cancel"

#: One-letter tags for the evidence classes backing a link, shown beside each
#: suggestion: T(race), R(oute — the firmware's learned out_path), P(acket log),
#: N(eighbour table fetched from a repeater).
_SOURCE_TAGS = {"trace": "T", "route": "R", "packet": "P", "neighbour": "N"}


@dataclass(frozen=True, slots=True)
class FetchNeighbours:
    """The composer's resolution when the user asks a repeater for its neighbours.

    The dialog itself never touches the radio; it resolves this marker and the owning
    flow performs the login + fetch, refreshes the topology, and reopens the composer
    with :attr:`PathComposerScreen.hops` re-seeded.

    Attributes:
        node: Canonical id of the repeater to query (the path's tail when committed).
    """

    node: str


class PathComposerScreen(Screen):
    """Compose a forced trace path step by step, mirrored at a target or hand-routed.

    Resolves with the finished spec (comma-separated hex — in target mode the outbound
    hops, the target, then the mirrored return; in path mode the composed walk
    verbatim), :data:`AUTO_SPEC` for device routing (target mode only), or
    :data:`~meshterm.ui.tui.screen.CANCEL`.
    """

    footer_hint = "↑↓ · Enter add · type to filter · ⌫ remove · Esc"

    def __init__(
        self,
        *,
        device_label: str,
        topology: MeshTopology,
        width_bytes: int,
        target_id: Optional[str] = None,
        target_hash: Optional[str] = None,
        target_label: Optional[str] = None,
        device_hash: Optional[str] = None,
        hops: Optional[list[str]] = None,
        fetch_nodes: frozenset[str] = frozenset(),
    ) -> None:
        """Build the composer.

        A pinned target (all three ``target_*`` arguments) selects target mode: the
        target is excluded from suggestions (the path implicitly turns at it) and the
        emitted spec appends it plus the mirrored return. With no target the composer
        is in path mode — the whole walk is composed by hand and committed verbatim.

        Args:
            device_label: Our own node's name, opening and closing the route preview.
            topology: The evidence graph suggestions are drawn from.
            width_bytes: Preferred per-hop path-hash width (bytes) for the emitted spec.
            target_id: The pinned target's canonical id, or ``None`` for path mode.
            target_hash: The target's hex hash used as the symmetric spec's turning
                point (its full key prefix; truncated to the spec width at commit).
            target_label: The target's display name for the route preview.
            device_hash: Our own public key, so the preview's endpoints carry a hash
                exactly like the trace screen's route line.
            hops: Canonical ids of already-composed hops (reopening the dialog resumes
                where the user left off).
            fetch_nodes: Canonical ids whose live neighbour table can be fetched
                (repeater contacts with a public key); when the path's tail is one of
                them, the *fetch neighbours* row appears.
        """
        super().__init__()
        self.title = f"compose path · {target_label}" if target_label else "compose path"
        self._target_id = target_id
        self._target_hash = (target_hash or "").lower().removeprefix("0x")
        self._target_label = target_label
        self._device_label = device_label
        self._device_hash = (device_hash or "").lower().removeprefix("0x")
        self._topology = topology
        self._width_bytes = width_bytes
        self._hops: list[str] = list(hops or [])
        self._fetch_nodes = fetch_nodes
        self._entry = ""
        self._index = 0

    @property
    def hops(self) -> list[str]:
        """The composed hops so far (for re-seeding after a fetch)."""
        return list(self._hops)

    # --- state -----------------------------------------------------------------

    @property
    def _mirrored(self) -> bool:
        """Whether the return leg is the pinned target's mirror (target mode)."""
        return self._target_id is not None

    def _tail(self) -> str:
        """The node the path currently ends at (us until a hop is added)."""
        return self._hops[-1] if self._hops else self._topology.self_id

    def _suggestions(self) -> list:
        """The current suggestion rows, filtered by the typed entry.

        Target mode excludes the target (the path implicitly turns at it) and every
        used hop (revisiting one on the outbound leg is never useful — the mirror
        already recrosses it). Path mode only excludes ourselves and the tail: a
        return leg legitimately reuses outbound repeaters.

        Returns:
            The (possibly filtered) :class:`~meshterm.services.topology.HopSuggestion`
            list for the path's tail, capped at :data:`_MAX_SUGGESTIONS`.
        """
        if self._mirrored:
            exclude = frozenset({self._topology.self_id, self._target_id, *self._hops})
        else:
            exclude = frozenset({self._topology.self_id, self._tail()})
        suggestions = self._topology.next_hops(self._tail(), exclude=exclude)
        needle = self._entry.lower()
        if needle:
            suggestions = [
                s
                for s in suggestions
                if s.node.startswith(needle)
                or needle in (self._topology.display_name(s.node) or "").lower()
            ]
        return suggestions[:_MAX_SUGGESTIONS]

    def _custom_hex(self) -> Optional[str]:
        """The typed entry as an addable hex hop, or ``None`` when it isn't one."""
        needle = self._entry.lower().removeprefix("0x")
        return needle if _is_hex(needle) else None

    def _rows(self) -> list[tuple[str, object]]:
        """The cursor-addressable rows: custom hop, suggestions, fetch, then actions."""
        rows: list[tuple[str, object]] = []
        custom = self._custom_hex()
        if custom:
            rows.append(("custom", custom))
        rows.extend(("hop", s) for s in self._suggestions())
        # Standing on a repeater we hold credentials for, its live neighbour table is
        # one keypress away — placed with the suggestions, because that is what it
        # extends: "don't see the node you need? ask the repeater what it hears."
        if self._tail() in self._fetch_nodes:
            rows.append(("fetch", self._tail()))
        rows.append(("action", _USE))
        if self._mirrored:  # a path walk has no target for the device to route to
            rows.append(("action", _AUTO))
        rows.append(("action", _CANCEL))
        return rows

    def _spec(self) -> str:
        """Render the composed route as the spec the trace command will send.

        Target mode appends the target and the mirrored return leg (see
        :func:`~meshterm.services.topology.render_forced_spec`); path mode renders
        the composed walk verbatim (:func:`~meshterm.services.topology.
        render_custom_spec`) — empty until it has at least one hop.
        """
        if self._mirrored:
            return render_forced_spec(
                tuple(self._hops), self._target_hash, self._width_bytes
            )
        return render_custom_spec(tuple(self._hops), self._width_bytes)

    # --- rendering ---------------------------------------------------------------

    def _node_text(self, node: str, *, dim: bool = False) -> Text:
        """One route node, rendered exactly as the trace screen's route line does.

        ``Name (hash)`` when known — name in the brand colour, hash muted in
        parentheses — a bare brand hash when not, and our own device as its accent
        label plus hash. Hashes show at the spec's preferred path-hash width, the
        same width every other trace-feature view truncates to, so a node reads the
        same length everywhere.

        Args:
            node: The node's canonical id (or ``self_id`` for our own device).
            dim: Whether to render in the resolved-return-leg's uniform faint style
                rather than the normal route colours.
        """
        if node == self._topology.self_id:
            text = Text(self._device_label, style="faint" if dim else "accent")
            if self._device_hash:
                shown = _shorten_hash(self._device_hash, self._width_bytes)
                text.append(f" ({shown})", style="faint" if dim else "muted")
            return text
        if node == self._target_id:
            name: Optional[str] = self._target_label
            hash_source = self._target_hash or node
        else:
            name = self._topology.display_name(node)
            hash_source = node
        shown = _shorten_hash(hash_source, self._width_bytes)
        if not name:
            return Text(shown, style="faint" if dim else "brand")
        text = Text(name, style="faint" if dim else "brand")
        text.append(f" ({shown})", style="faint" if dim else "muted")
        return text

    def _route_preview(self) -> Text:
        """The route under construction, endpoints filled in automatically.

        Target mode shows the composed outbound in full colour, then the pinned
        target and the mirrored return resolved and dimmed right alongside it — the
        dimming reads as "this half isn't yours to compose". Path mode shows every
        composed hop in full colour (they are all yours) between our own node at
        both ends, with only the final landing back on us dimmed.
        """
        text = Text()
        text.append_text(self._node_text(self._topology.self_id))
        for hop in self._hops:
            text.append(" → ", style="muted")
            text.append_text(self._node_text(hop))
        if self._mirrored:
            text.append(" → ", style="muted")
            text.append_text(self._node_text(self._target_id))
            for hop in reversed(self._hops):
                text.append(" → ", style="faint")
                text.append_text(self._node_text(hop, dim=True))
        text.append(" → ", style="faint")
        text.append_text(self._node_text(self._topology.self_id, dim=True))
        return text

    def _suggestion_text(self, suggestion) -> Text:  # noqa: ANN001
        """One suggestion row: node, then its link's evidence trail."""
        text = self._node_text(suggestion.node)
        link = suggestion.link
        snr = link.median_snr
        if snr is not None:
            text.append("  ↔ ", style="muted")
            text.append(f"{snr:+.1f} dB", style=snr_style(snr))
        text.append(f"  {link.samples}×", style="muted")
        age = _format_age(_age_seconds(link.last_seen))
        text.append(f" · {age}", style="muted")
        tags = "".join(_SOURCE_TAGS[s] for s in sorted(link.sources & _SOURCE_TAGS.keys()))
        if tags:
            text.append(f" · {tags}", style="faint")
        return text

    def _row_text(self, kind: str, payload: object) -> Text:
        """The display text for one cursor-addressable row."""
        if kind == "custom":
            text = Text("+ add hop ", style="warn")
            text.append(str(payload), style="brand")
            text.append("  (typed hex)", style="muted")
            return text
        if kind == "hop":
            return self._suggestion_text(payload)
        if kind == "fetch":
            name = self._topology.display_name(str(payload)) or str(payload)[:12]
            text = Text("⇣ Fetch neighbours from ", style="")
            text.append(name, style="brand")
            text.append("  (asks the repeater over the mesh)", style="muted")
            return text
        if payload == _USE:
            label = Text.assemble(("✓ ", "ok"), "Use this path")
            spec = self._spec()
            # A path walk with no hops yet has no spec to commit.
            label.append(f"  ({spec})" if spec else "  (add a hop first)", style="muted")
            return label
        if payload == _AUTO:
            return Text("Auto — let the device route")
        return Text.assemble(("✗ ", "err"), "Cancel")

    @property
    def dialog_width(self) -> int:
        """Natural outer width hugging the widest row (compositor still caps it)."""
        widths = [cell_len(self.title), cell_len(self.footer_hint)]
        widths.append(cell_len(self._route_preview().plain))
        for kind, payload in self._rows():
            widths.append(cell_len(self._row_text(kind, payload).plain) + 2)
        return max(widths, default=20) + 8

    def render_body(self, width: int) -> list[str]:
        """Render the route preview, filter/hint line, suggestions, and actions."""
        rows = self._rows()
        self._index = max(0, min(self._index, len(rows) - 1))

        lines = render_lines(self._route_preview(), width)
        lines.append("")
        if self._entry:
            lines.append(render_to_ansi(Text(f"/{self._entry}", style="warn"), width))
        else:
            heading = Text("Next hop from ", style="muted")
            heading.append_text(self._node_text(self._tail()))
            heading.append(" — strongest first", style="muted")
            lines.extend(render_lines(heading, width))

        cursor_at: Optional[int] = None
        for i, (kind, payload) in enumerate(rows):
            if kind == "action" and (i == 0 or rows[i - 1][0] != "action"):
                lines.append("")  # a spacer sets the action group apart
            is_sel = i == self._index
            text = Text("❯ " if is_sel else "  ", style="brand" if is_sel else "")
            text.append_text(self._row_text(kind, payload))
            if is_sel:
                text.style = "brand"
            text.no_wrap = True
            text.truncate(width, overflow="ellipsis")
            if is_sel:
                cursor_at = len(lines)
            lines.append(render_to_ansi(text, width))
        if not any(kind == "hop" for kind, _ in rows):
            if any(kind == "fetch" for kind, _ in rows):
                note = "(no observed links from here — fetch the repeater's neighbours, or type a hex hash)"
            else:
                note = "(no observed links from here — type a hex hash to force a hop)"
            lines.append(render_to_ansi(Text(note, style="muted"), width))

        self._cursor = cursor_at
        self._scroll_total = max(1, len(lines))
        return lines

    def cursor_line(self) -> Optional[int]:
        """The body line of the highlighted row, so the session keeps it visible."""
        return getattr(self, "_cursor", None)

    # --- input ---------------------------------------------------------------------

    def _commit_row(self) -> None:
        """Apply the highlighted row: append a hop or run an action."""
        rows = self._rows()
        if not rows:
            return
        kind, payload = rows[self._index]
        if kind == "custom":
            self._hops.append(str(payload))
            self._entry = ""
            self._index = 0
        elif kind == "hop":
            self._hops.append(payload.node)  # type: ignore[union-attr]
            self._entry = ""
            self._index = 0
        elif kind == "fetch":
            self.resolve(FetchNeighbours(node=str(payload)))
        elif payload == _USE:
            spec = self._spec()
            if spec:  # an empty path walk would masquerade as AUTO_SPEC
                self.resolve(spec)
        elif payload == _AUTO:
            self.resolve(AUTO_SPEC)
        else:
            super().handle("escape")

    def handle(self, action: str, data: str = "") -> None:
        """Move the cursor, edit the entry, add/remove hops, or commit/cancel."""
        rows = self._rows()
        if action == "up" and rows:
            self._index = (self._index - 1) % len(rows)
        elif action == "down" and rows:
            self._index = (self._index + 1) % len(rows)
        elif action in ("home", "ctrl_home"):
            self._index = 0
        elif action in ("end", "ctrl_end"):
            self._index = max(0, len(rows) - 1)
        elif action == "enter":
            self._commit_row()
        elif action == "backspace":
            if self._entry:
                self._entry = self._entry[:-1]
            elif self._hops:
                self._hops.pop()
            self._index = 0
        elif action == "text" and data.isprintable() and data not in ("/",):
            self._entry += data
            self._index = 0
        elif action == "escape":
            super().handle("escape")
