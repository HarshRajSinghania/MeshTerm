"""The Trophy case: the record-setting walks the trace tools have turned up.

A read-only board over the ``discovered_paths`` table (:meth:`Repository.discoveries`),
opened from the main menu. Every successful trace — a *Trace target* boomerang or a
*Trace path* walk — is scored against all six disciplines and offered to their boards
(see :mod:`~meshterm.services.records`); this screen is where the survivors live.

* the browser groups the six disciplines, each under its heading with a one-line
  description of the game (word-wrapped when it must), then that discipline's records
  ranked best-first — dated, scored in the discipline's own unit, with the walked route
  through THE path widget. A discipline holding records at more than one hash width tags
  each row with its width, since the widths are genuinely different games;
* opening a record floats :class:`RecordDialog` — every stat the walk was measured by,
  the walk drawn on THE route graph (the Message paths dialog's shape, us at both ends),
  the full route and spec, and when/by which app version it was set. From there
  *Trace this path* reopens Trace path with the record's route prefilled, so a claim
  worth re-testing is one Enter from the air again;
* deletion comes in three grains: one record, one discipline (every width), or
  everything (the last two behind a confirm — the wholesale one typed).

Nothing here transmits: it reads the boards the trace tools filled.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, Optional

from rich.text import Text

from ..persistence.repository import DiscoveredPath
from ..services import trace_runner
from ..services.records import CATEGORIES, CATEGORY_BY_ID, Category
from .menus import back_rows, section_heading
from .pathgraph import PathLayer, render_path_graph
from .theme import snr_style
from .tui.render import render_hanging, render_to_ansi
from .tui.screen import Screen
from .widgets import NodeResolver, path_text, route_graph_style

if TYPE_CHECKING:
    from ..context import AppContext

#: The width the discipline descriptions wrap at — comfortably inside 72 columns so the
#: prose reads the same however wide the terminal is.
_DESC_WRAP = 64

#: The colour a record's single walk draws in on the route graph. It is the only path
#: on the canvas, so it needs no colour to set it apart — white just reads as "the walk".
_WALK_EDGE = (255, 255, 255)


class RecordDialog(Screen):
    """One record's full story, floating over the screen beneath.

    Every stat the walk was measured by, the walk drawn on THE route graph (us at both
    ends, the same shape the Message paths dialog draws) over the route in full (wrapped,
    never truncated), and the record's provenance — when it was set and by which app
    version. Two actions besides Back: *Trace this path* reopens Trace path with the
    record's route prefilled (propagation shifts; a record is a claim worth re-testing),
    and *Delete…* removes this one record behind a confirm.
    """

    def __init__(
        self,
        record: DiscoveredPath,
        category: Category,
        rank: int,
        *,
        resolve: NodeResolver,
        device_label: str,
        device_hash: Optional[str],
    ) -> None:
        """Build the dialog for one stored record.

        Args:
            record: The record to show.
            category: Its category (for the score's unit and title).
            rank: Its standing on the board (1 = the record holder).
            resolve: Maps node ids to friendly names.
            device_label: Our node's name, bracketing the route.
            device_hash: Our public key, annotated at the record's width.
        """
        super().__init__()
        self.title = f"Record — {category.title} #{rank}"
        self.footer_hint = "↑↓ move · Enter commit · Esc back"
        self._record = record
        self._category = category
        self._resolve = resolve
        self._device_label = device_label
        self._device_hash = device_hash
        self._actions = ("trace", "delete", "back")
        self._index = 0
        self._cursor: Optional[int] = None

    @property
    def dialog_width(self) -> int:
        """A comfortable reading width; the compositor still caps it to the frame."""
        return 62

    def handle(self, action: str, data: str = "") -> None:
        """Move the cursor, commit the selected action, or dismiss."""
        if action == "up":
            self._index = (self._index - 1) % len(self._actions)
        elif action == "down":
            self._index = (self._index + 1) % len(self._actions)
        elif action == "enter":
            key = self._actions[self._index]
            self.resolve(None if key == "back" else key)
        elif action == "escape":
            self.resolve(None)

    def cursor_line(self) -> Optional[int]:
        """Keep the selected action row visible if the dialog ever scrolls."""
        return self._cursor

    def _lane(self, label: str, value: Text) -> Text:
        """One label/value stat lane (label lane fixed so values align)."""
        row = Text(f"{label:<11}", style="muted")
        row.append_text(value)
        return row

    def _graph_lines(self, width: int) -> list[str]:
        """Draw the walked route as one path on THE route graph — us at both ends.

        A scored walk leaves home and comes back, so it draws us (left) → its relays →
        us (right) as a single white path through the shared fan-lane widget
        (:func:`~meshterm.ui.pathgraph.render_path_graph`), the same shape the Message
        paths dialog draws a delivery over. Nodes the walk passed through more than once
        — a boomerang's mirrored return leg — collapse to their first appearance: the
        depth-ordered graph can only seat a node once, and the route line below still
        carries every hop, revisits and all.
        """
        seen: set[str] = set()
        hops: list[str] = []
        for node in self._record.route:
            if node not in seen:
                seen.add(node)
                hops.append(node)
        glyph_of, label_of, label_rgb_of = route_graph_style(
            resolve=self._resolve,
            self_name=self._device_label,
            source=self._device_label,
        )
        return render_path_graph(
            [PathLayer(hops=tuple(hops), color=_WALK_EDGE, priority=3)],
            width,
            glyph_of=glyph_of, label_of=label_of, label_rgb_of=label_rgb_of,
        )

    def render_body(self, width: int) -> list[str]:
        """Stats lanes, the route graph, the full route, provenance, then the actions."""
        record, category = self._record, self._category
        stats = record.stats
        lines: list[str] = []

        score = category.format_score(record.score)
        if category.id == "long_haul" and not stats.get("km_complete", True):
            score = "≥ " + score
        lines.append(render_to_ansi(
            self._lane("score", Text(score, style="accent bold")), width, no_wrap=True
        ))

        hops = stats.get("hop_count", len(record.route))
        distinct = stats.get("distinct_nodes", len(set(record.route)))
        shape = Text(f"{hops} hop{'s' if hops != 1 else ''}")
        shape.append(f" · {distinct} distinct node{'s' if distinct != 1 else ''}",
                     style="muted")
        if stats.get("repeats"):
            shape.append(" · revisits", style="muted")
        lines.append(render_to_ansi(self._lane("walk", shape), width, no_wrap=True))

        km = stats.get("km_travelled")
        if km:
            value = Text(f"{'≥ ' if not stats.get('km_complete', True) else ''}{km:.1f} km")
            lines.append(render_to_ansi(
                self._lane("distance", value), width, no_wrap=True
            ))
        far = stats.get("far_km")
        if far is not None:
            lines.append(render_to_ansi(
                self._lane("far point", Text(f"{far:.1f} km")), width, no_wrap=True
            ))
        area = stats.get("area_km2")
        if area is not None:
            lines.append(render_to_ansi(
                self._lane("area", Text(f"{area:.1f} km²")), width, no_wrap=True
            ))
        snr = stats.get("min_snr")
        if snr is not None:
            lines.append(render_to_ansi(
                self._lane("weakest", Text(f"{snr:+.1f} dB", style=snr_style(snr))),
                width, no_wrap=True,
            ))
        rtt = stats.get("rtt_ms")
        if rtt is not None:
            lines.append(render_to_ansi(
                self._lane("round trip", Text(f"{rtt:.0f} ms")), width, no_wrap=True
            ))

        lines.append("")
        lines.extend(self._graph_lines(width))
        caption = Text("you → … → you · labels = hash byte", style="faint")
        lines.append(render_to_ansi(caption, width, no_wrap=True))

        lines.append("")
        route = path_text(
            [None, *record.route, None],
            self._resolve,
            self_name=self._device_label,
            show_hash=True,
            hash_bytes=record.width_bytes,
            device_hash=self._device_hash,
        )
        lines.extend(render_hanging(Text("route      ", style="muted"), route, width,
                                    indent=11))
        spec = Text(record.spec, style="brand")
        spec.append(f"  ({record.width_bytes}-byte hops)", style="muted")
        lines.extend(render_hanging(Text("spec       ", style="muted"), spec, width,
                                    indent=11))

        stamp = record.discovered_at.astimezone().strftime("%b %d %Y %H:%M")
        when = Text(stamp)
        when.append(f" · MeshTerm {record.app_version}", style="muted")
        lines.append(render_to_ansi(self._lane("recorded", when), width, no_wrap=True))

        lines.append("")
        self._cursor = None
        for i, key in enumerate(self._actions):
            if key == "back":
                lines.append("")
            selected = i == self._index
            row = Text("❯ " if selected else "  ", style="brand" if selected else "")
            if key == "trace":
                row.append("👣 ", style="accent")
                row.append("Trace this path — reopen in Trace path")
            elif key == "delete":
                row.append("🗑 ", style="err")
                row.append("Delete record…")
            else:
                row.append("Back")
            if selected:
                row.style = "brand"
                self._cursor = len(lines)
            row.no_wrap = True
            row.truncate(width, overflow="ellipsis")
            lines.append(render_to_ansi(row, width))
        self._scroll_total = max(1, len(lines))
        return lines


async def open_records(ctx: "AppContext") -> dict:
    """Open the Trophy case browser and run it until dismissed.

    Wires the browser to the database and the observed contacts: records come straight
    from :meth:`Repository.discoveries`, and hop hashes resolve to friendly names
    through the same resolver the trace screens use. *Trace this path* hands off to the
    live Trace path screen with the record's route prefilled and returns here after.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Returns:
        A summary dict for the tool's run row (the record count shown).

    Raises:
        RuntimeError: If called outside the interactive menu.
    """
    from .surface import TuiUi
    from .trace_screen import open_trace_path
    from .tui import CANCEL, Choice, SelectScreen, Separator

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu caller
        raise RuntimeError("the Trophy case screen is only available in the menu")
    session = ctx.ui.session

    contacts = await ctx.devstate.contacts()
    self_info = await ctx.devstate.self_info()
    resolve = trace_runner.make_node_resolver(contacts, ctx.repo.node_names())
    device_label = str(self_info.get("name") or "us")
    device_hash = str(self_info.get("public_key") or "") or None

    def _as_float(value) -> Optional[float]:  # noqa: ANN001
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    lat, lon = _as_float(self_info.get("adv_lat")), _as_float(self_info.get("adv_lon"))
    self_pos = (lat, lon) if lat is not None and lon is not None and (lat or lon) else None

    def ranked(category: Category) -> list[DiscoveredPath]:
        """One discipline's records, ranked best-first across every hash width."""
        rows = ctx.repo.discoveries(category.id)
        rows.sort(key=lambda r: r.score, reverse=not category.ascending)
        return rows

    def describe(category: Category) -> list:
        """The heading and its word-wrapped description as non-selectable rows."""
        rows: list = [section_heading(f"{category.icon} {category.title}")]
        desc = category.description
        if category.needs_positions and self_pos is None:
            desc += " — needs your location (set it in Config) to score"
        for line in textwrap.wrap(desc, _DESC_WRAP):
            rows.append(Separator(f"   {line}", style="muted"))
        return rows

    def browser_row(
        rank: int, category: Category, record: DiscoveredPath, *, show_width: bool
    ) -> Text:
        """One record row: rank, date, score (width when it disambiguates), route."""
        row = Text(f"#{rank}  ", style="muted")
        row.append(record.discovered_at.astimezone().strftime("%b %d %H:%M"),
                   style="muted")
        row.append("  ")
        score = category.format_score(record.score)
        if category.id == "long_haul" and not record.stats.get("km_complete", True):
            score = "≥ " + score
        row.append(f"{score:<12}", style="accent")
        if show_width:
            row.append(f"{record.width_bytes} B  ", style="muted")
        row.append(" ")
        row.append_text(
            path_text(
                [None, *record.route, None],
                resolve,
                self_name=device_label,
                show_hash=True,
                hash_bytes=record.width_bytes,
                device_hash=device_hash,
            )
        )
        return row

    async def delete_category_flow() -> None:
        """Pick a discipline, confirm, and delete its records (every width)."""
        rows: list = []
        for category in CATEGORIES:
            count = len(ctx.repo.discoveries(category.id))
            rows.append(Choice(
                title=Text.assemble(
                    (f"{category.icon} {category.title}  ", ""),
                    (f"{count} record{'s' if count != 1 else ''}, all widths", "muted"),
                ),
                value=category.id,
            ))
        rows.extend(back_rows("__back__"))
        picked = await session.run_screen(
            SelectScreen(
                "Delete a discipline's records",
                rows,
                footer_hint="↑↓ move · Enter pick · Esc back",
                filterable=False,
                wrap=False,
            )
        )
        if picked in (CANCEL, None, "__back__"):
            return None
        category = CATEGORY_BY_ID[str(picked)]
        sure = await ctx.ui.dialog(
            f"Delete every {category.title} record, at every hash width?",
            [("Cancel", False), ("Delete", True)],
            title="Delete discipline records",
            default=1,
            danger=True,
        )
        if sure:
            ctx.repo.delete_discoveries(category.id)

    while True:
        items: list = []
        for category in CATEGORIES:
            items.extend(describe(category))
            board = ranked(category)
            if not board:
                items.append(Separator("   no records yet", style="muted"))
            show_width = len({r.width_bytes for r in board}) > 1
            for rank, record in enumerate(board, start=1):
                items.append(Choice(
                    title=browser_row(rank, category, record, show_width=show_width),
                    value=("open", category, rank, record),
                ))
        total = len(ctx.repo.discoveries())
        items.append(Separator(" "))
        if total:
            items.append(Choice(
                title=Text.assemble(("🗑 ", "err"), "Delete a discipline's records…"),
                value=("del_cat", None, 0, None),
            ))
            items.append(Choice(
                title=Text.assemble(("🗑 ", "err"), "Delete all records…"),
                value=("del_all", None, 0, None),
            ))
        items.extend(back_rows(("back", None, 0, None)))
        picked = await session.run_screen(
            SelectScreen(
                "Trophy case",
                items,
                footer_hint="↑↓ move · Enter open · Esc back",
                wrap=False,
            )
        )
        if picked is CANCEL or picked is None or picked[0] == "back":
            return {"records": total}
        verb = picked[0]
        if verb == "del_cat":
            await delete_category_flow()
            continue
        if verb == "del_all":
            if not total:
                continue
            if await session.typed_confirm(
                f"This deletes all {total} records — every discipline, every width. "
                "They can only be re-earned by walking them again.",
                "delete",
                title="Delete all records",
            ):
                ctx.repo.delete_discoveries()
            continue
        _verb, category, rank, record = picked
        action = await session.run_screen(RecordDialog(
            record, category, rank,
            resolve=resolve, device_label=device_label, device_hash=device_hash,
        ))
        if action == "trace":
            await open_trace_path(ctx, spec=record.spec)
        elif action == "delete":
            sure = await ctx.ui.dialog(
                f"Delete this {category.title} record?",
                [("Cancel", False), ("Delete", True)],
                title="Delete record",
                default=1,
                danger=True,
            )
            if sure:
                ctx.repo.delete_discovery(record.id)
