"""The packet viewer: one floating popup that can open any packet a list shows.

Everywhere MeshTerm lists packets (the dashboard feed today; any future packet list)
the rows share this module's per-kind chrome — a colour and a two-cell icon per packet
class — and every row can open into the same viewer: a centered dialog over the list
that lays the packet out in full, flavoured by kind. An advert shows the node's
identity, type, and location; telemetry shows the node and its reported values; an
RX-logged packet shows the relay path it rode in on; a message shows the sender, the
conversation, and the text; an ack shows its code. Common to all: the timestamp, the
node (name coloured by the app-wide palette, hash in the hash widget), and reception
quality on the shared SNR bar.

When opened over a list the viewer pages through it in place — PgUp/PgDn step to the
newer/older packet, Home/End jump to the newest/oldest — so a burst can be read
packet by packet without bouncing back out to the list. ``↑``/``↓`` scroll a tall
packet inside the dialog; Esc closes it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from rich.console import RenderableType
from rich.table import Table
from rich.text import Text

from ..core.models import NODE_TYPE_LABELS, Observation
from ..services.trace_runner import NodeResolver
from .theme import name_style, snr_style
from .trace_screen import snr_bar
from .tui.render import render_lines
from .tui.screen import Screen
from .widgets import _age_seconds, _format_age, highlighted_hash

#: Display style per packet class, shared by every packet list and the viewer.
KIND_STYLES = {
    "advert": "accent",
    "telemetry": "brand",
    "packet": "muted",
    "message": "ok",
    "ack": "faint",
}

#: The two-cell icon per packet class — every icon is emoji-wide, so a list can swap
#: the textual kind label for the icon alone on a narrow terminal without the lanes
#: shifting. The fallback marks a class this table has never heard of.
KIND_ICONS = {
    "advert": "📢",
    "telemetry": "📊",
    "packet": "📦",
    "message": "💬",
    "ack": "✅",
}
DEFAULT_ICON = "❔"


@dataclass(slots=True)
class PacketEntry:
    """One listed packet, in the shape every packet list stores and the viewer reads.

    A deliberately flat union of what the feed's three event families carry — the
    monitor's observations, chat messages, and delivery acks — so one list type can
    hold them all and the viewer can flavour by :attr:`kind`.

    Attributes:
        when: When the packet was heard.
        kind: Packet class (``advert``/``telemetry``/``packet``/``message``/``ack``).
        node: The transmitting node's stored hash, when identified.
        name: The name the packet itself carried (an advert's node name, a message's
            sender), if any; display falls back to resolving :attr:`node`.
        snr: Reception SNR in dB, if measured (for ``packet`` rows: of the *last relay*).
        rssi: Reception strength in dBm, if measured.
        lat: Advertised latitude, when the packet shared a location.
        lon: Advertised longitude, when the packet shared one.
        node_type: The advertised node type (see ``NODE_TYPE_*``), if carried.
        path: A ``packet`` row's relay path: comma-separated hop hashes in propagation
            order (empty = arrived direct; ``None`` = the class carries no path).
        where: A message's conversation (``ch 3`` / ``direct``) or an ack's code.
        text: A message's body, when it is known.
        raw: The raw event payload, for the values a class-specific layout can't name.
    """

    when: datetime
    kind: str
    node: Optional[str] = None
    name: Optional[str] = None
    snr: Optional[float] = None
    rssi: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    node_type: Optional[int] = None
    path: Optional[str] = None
    where: Optional[str] = None
    text: Optional[str] = None
    raw: Optional[dict] = None

    @classmethod
    def from_observation(cls, obs: Observation) -> "PacketEntry":
        """Capture an overheard observation (the monitor's event family) as an entry."""
        return cls(
            when=obs.observed_at,
            kind=obs.kind,
            node=obs.node,
            name=obs.name,
            snr=obs.snr,
            rssi=obs.rssi,
            lat=obs.lat,
            lon=obs.lon,
            node_type=obs.node_type,
            path=obs.path,
            raw=obs.raw,
        )


def kind_icon(kind: str) -> str:
    """The two-cell icon for a packet class (a fallback for unknown classes)."""
    return KIND_ICONS.get(kind, DEFAULT_ICON)


def node_label(
    entry: PacketEntry, resolve: NodeResolver, self_name: Optional[str] = None
) -> tuple[str, str]:
    """The display name and style for an entry's node — the app-wide naming rule.

    What ``resolve`` knows about the hash wins (the contact list is the canonical
    identity), then the name the packet itself carried; a genuinely nameless node
    shows its hash. Names take their stable palette hue
    (:func:`~meshterm.ui.theme.name_style`) — except our own node, which is always
    the pure-white ``you`` — and a bare hash stays muted, because colour is the
    "this is a name" signal.

    Args:
        entry: The packet whose node to label.
        resolve: Maps a node hash to a friendly name when known.
        self_name: Our own node's name, to spot "us" (``None`` = never matches).

    Returns:
        ``(label, style)``, ready to append to a row.
    """
    name = None
    if entry.node:
        resolved = resolve(entry.node)
        if resolved and resolved != entry.node:
            name = resolved
    if not name:
        name = entry.name
    if not name:
        return (entry.node or "?", "muted")
    if self_name and name == self_name:
        return (name, "you")
    return (name, name_style(name))


class PacketViewer(Screen):
    """The floating packet dialog: one entry laid out in full, pageable over its list.

    A read-only popup. The opener hands it the list *as rendered* (newest first) and
    the index of the row the user opened; PgUp/PgDn then walk toward newer/older
    packets, Home/End jump to the ends, ``↑``/``↓`` scroll a tall body, Esc closes.
    Opened over a single packet (a one-entry list) the paging keys simply do nothing.
    """

    def __init__(
        self,
        entries: list[PacketEntry],
        index: int,
        *,
        resolve: NodeResolver,
        prefix_bytes: int = 0,
        self_name: Optional[str] = None,
    ) -> None:
        """Open the viewer over a packet list.

        Args:
            entries: The list's packets, newest first (a snapshot; live inserts into
                the underlying list don't shift the view).
            index: Which entry to open on.
            resolve: Maps a node hash to a friendly name when known.
            prefix_bytes: Path-hash width to light in displayed hashes (0 = none).
            self_name: Our own node's name, drawn white wherever it appears.
        """
        super().__init__()
        self._entries = entries
        self._index = max(0, min(index, len(entries) - 1))
        self._resolve = resolve
        self._prefix_bytes = prefix_bytes
        self._self_name = self_name
        if len(entries) > 1:
            self.footer_hint = "PgUp/PgDn newer/older · Home/End ends · Esc close"
        else:
            self.footer_hint = "Esc close"
        self._set_title()

    def _set_title(self) -> None:
        entry = self._entries[self._index]
        title = f"{kind_icon(entry.kind)} {entry.kind}"
        if len(self._entries) > 1:
            title += f" · {self._index + 1}/{len(self._entries)}"
        self.title = title

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Page through the list, scroll a tall entry, or dismiss."""
        if action == "pageup":
            self._jump(self._index - 1)
        elif action in ("pagedown", "space"):
            self._jump(self._index + 1)
        elif action in ("home", "ctrl_home"):
            self._jump(0)
        elif action in ("end", "ctrl_end"):
            self._jump(len(self._entries) - 1)
        elif action == "up":
            self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action in ("escape", "enter"):
            self.resolve(None)

    def _jump(self, index: int) -> None:
        """Move to another entry (clamped), resetting the scroll for the new body."""
        index = max(0, min(index, len(self._entries) - 1))
        if index != self._index:
            self._index = index
            self.scroll = 0
            self._set_title()

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Lay the current entry out as labelled rows, flavoured by its kind."""
        entry = self._entries[self._index]
        grid = Table(
            box=None, show_header=False, show_edge=False, pad_edge=False,
            padding=(0, 0), expand=False,
        )
        # The label lane is fixed so wrapped values align with their own block
        # (the app-wide hanging-indent rule), never with column zero.
        grid.add_column(width=10, no_wrap=True)
        grid.add_column(overflow="fold", max_width=max(20, width - 10))

        for label, value in self._rows(entry):
            grid.add_row(Text(label, style="muted"), value)
        lines = render_lines(grid, width)
        self._scroll_total = max(1, len(lines))
        return lines

    def _rows(self, entry: PacketEntry) -> list[tuple[str, RenderableType]]:
        """The labelled rows for one entry: the common core plus its kind's extras."""
        rows: list[tuple[str, RenderableType]] = []

        secs = _age_seconds(entry.when)
        heard = Text(entry.when.astimezone().strftime("%b %d %H:%M:%S"))
        heard.append(f"  ({_format_age(secs)} ago)", style="muted")
        rows.append(("heard", heard))

        if entry.node or entry.name:
            who = Text()
            label, style = node_label(entry, self._resolve, self._self_name)
            who.append(label, style=style)
            if entry.node and label != entry.node:
                who.append("  ")
                who.append_text(highlighted_hash(entry.node, self._prefix_bytes))
            rows.append(("from", who))
        if entry.node_type is not None:
            label = NODE_TYPE_LABELS.get(entry.node_type, f"type {entry.node_type}")
            rows.append(("type", Text(label)))

        if entry.snr is not None or entry.rssi is not None:
            rows.append(("snr", self._reception(entry)))
        if entry.kind == "packet":
            rows.append(("via", self._path_text(entry)))
            rows.append((
                "", Text("reception describes the last relay, not the origin",
                         style="faint"),
            ))
        if entry.lat is not None and entry.lon is not None:
            rows.append(("location", Text(f"{entry.lat:.5f}, {entry.lon:.5f}")))

        if entry.where:
            label = "code" if entry.kind == "ack" else "where"
            rows.append((label, Text(entry.where)))
        if entry.text:
            rows.append(("text", Text(entry.text)))

        rows.extend(self._raw_rows(entry))
        return rows

    def _reception(self, entry: PacketEntry) -> Text:
        """SNR (with the shared quality bar) and RSSI on one line."""
        line = Text()
        if entry.snr is not None:
            line.append(f"{entry.snr:+.1f} dB  ", style=snr_style(entry.snr))
            line.append_text(snr_bar(entry.snr))
        if entry.rssi is not None:
            if entry.snr is not None:
                line.append("  ·  ", style="muted")
            line.append(f"{entry.rssi:.0f} dBm rssi", style="muted")
        return line

    def _path_text(self, entry: PacketEntry) -> Text:
        """A ``packet`` entry's relay path as named hops, or ``direct``."""
        hops = [h for h in (entry.path or "").split(",") if h]
        if not hops:
            return Text("direct — no relays", style="muted")
        text = Text()
        for i, hop in enumerate(hops):
            if i:
                text.append(" → ", style="muted")
            named = self._resolve(hop)
            if named and named != hop:
                style = "you" if self._self_name and named == self._self_name else name_style(named)
                text.append(named, style=style)
                text.append(f" ({hop})", style="muted")
            else:
                text.append(hop, style="muted")
        return text

    def _raw_rows(self, entry: PacketEntry) -> list[tuple[str, RenderableType]]:
        """The raw payload's leftover fields, one labelled row each (telemetry's meat).

        Fields the flavoured layout already presents are skipped; the rest render as
        flat ``key value`` rows so a telemetry frame's channels (or a new firmware
        field the layout doesn't know yet) are still visible without a debugger.
        """
        raw = entry.raw if isinstance(entry.raw, dict) else None
        if not raw:
            return []
        shown = {
            k: v
            for k, v in raw.items()
            if k not in ("node", "name", "kind", "snr", "rssi", "lat", "lon", "path",
                         "node_type", "observed_at", "text")
            and v is not None
        }
        if not shown:
            return []
        rows: list[tuple[str, RenderableType]] = [("", Text())]
        rows.append(("payload", Text(f"{len(shown)} raw fields", style="faint")))
        for key, value in list(shown.items())[:12]:
            body = str(value)
            if len(body) > 200:
                body = body[:199] + "…"
            rows.append((f"  {key[:8]}", Text(body, style="muted")))
        return rows
