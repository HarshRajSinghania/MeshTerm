"""The Time Machine: the mesh's recorded history, explorable node by node.

The interactive face of the ``timemachine`` tool. The database has been recording every
overheard packet since the first session, but nothing surfaced that history beyond the
dashboard's two-hour window — this screen is the archaeology dig. A picker offers the
whole mesh or any node ever heard; each subject renders as a scrollable page of braille
charts and stats over a switchable window (``w`` cycles 24 h → 7 d → 30 d → all time):

* a **node** shows its reception volume over the window, its median-SNR band (coloured
  by quality), its hour-of-day rhythm (when does this node talk?), and the roll-up
  stats — first/last heard, medians, extremes;
* the **whole mesh** shows packets per day and nodes per day across the history, the
  arrivals of the window (nodes heard for the first time ever), and the all-time
  totals.

Unlike the dashboard's live chart (newest at the left, sliding), these are *histories*
and read chronologically: oldest at the left, now at the right. Everything is stored
data — no device is needed and nothing transmits.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING, Callable, Optional

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.models import utcnow
from .dashboard_screen import _COL_LEFT, _COL_RIGHT, braille_bars
from .theme import snr_style
from .tui.render import render_lines
from .tui.screen import Screen
from .widgets import _NODE_GLYPHS, _age_seconds, _format_age, _recency_style

if TYPE_CHECKING:
    from ..context import AppContext

#: The switchable history windows (``None`` = everything ever recorded).
_WINDOWS: tuple[tuple[str, Optional[timedelta]], ...] = (
    ("24 h", timedelta(days=1)),
    ("7 d", timedelta(days=7)),
    ("30 d", timedelta(days=30)),
    ("all time", None),
)

#: Picker sentinel for the whole-mesh overview page.
MESH = ("mesh",)

#: Character cells the charts keep clear for their side gutters.
_GUTTER = 4

#: How many braille rows tall the charts draw.
_CHART_ROWS = 2


def bucketize(stamps: list[datetime], start: datetime, end: datetime, buckets: int) -> list[int]:
    """Fold timestamps into ``buckets`` equal slices of ``[start, end]``, oldest first."""
    counts = [0] * max(1, buckets)
    span = max(1.0, (end - start).total_seconds())
    for stamp in stamps:
        index = int((stamp - start).total_seconds() / span * buckets)
        counts[min(buckets - 1, max(0, index))] += 1
    return counts


def bucket_medians(
    pairs: list[tuple[datetime, float]], start: datetime, end: datetime, buckets: int
) -> list[Optional[float]]:
    """Per-bucket medians of timestamped readings (``None`` for empty buckets)."""
    grouped: list[list[float]] = [[] for _ in range(max(1, buckets))]
    span = max(1.0, (end - start).total_seconds())
    for stamp, value in pairs:
        index = int((stamp - start).total_seconds() / span * buckets)
        grouped[min(buckets - 1, max(0, index))].append(value)
    return [median(values) if values else None for values in grouped]


def band_rows(
    values: list[Optional[float]], *, rows: int = _CHART_ROWS
) -> tuple[list[Text], float, float]:
    """Render sparse readings as a braille band scaled between their own extremes.

    The volume charts scale zero-to-peak (:func:`braille_bars`); readings like SNR need
    a *band* — the chart floor is the window's minimum, not zero, so a swing from
    −12 dB to −4 dB still fills the height. Two values share each cell (braille's full
    horizontal resolution), every present value lights at least one dot, empty columns
    keep the faint one-dot floor, and each cell is coloured by its readings' quality
    (the shared SNR palette).

    Args:
        values: Per-bucket readings, oldest first; ``None`` marks an empty bucket.
        rows: How many braille rows tall the band is.

    Returns:
        ``(lines, lo, hi)``: the rendered rows plus the scale's extremes (both 0.0
        when no reading is present at all).
    """
    present = [v for v in values if v is not None]
    lo, hi = (min(present), max(present)) if present else (0.0, 0.0)
    spread = max(1e-9, hi - lo)
    total = rows * 4
    heights = [
        0 if v is None else max(1, 1 + round((v - lo) / spread * (total - 1)))
        for v in values
    ]
    padded = heights + [0] * (len(heights) % 2)
    value_pairs = list(values) + [None] * (len(values) % 2)
    lines: list[Text] = []
    for row in range(rows):
        floor = (rows - 1 - row) * 4
        line = Text()
        for i in range(0, len(padded), 2):
            lf = min(4, max(0, padded[i] - floor))
            rf = min(4, max(0, padded[i + 1] - floor))
            cell = [v for v in value_pairs[i : i + 2] if v is not None]
            if lf or rf:
                mask = _COL_LEFT[lf] | _COL_RIGHT[rf]
                if row == rows - 1:
                    mask |= (0x40 if lf == 0 else 0) | (0x80 if rf == 0 else 0)
                line.append(
                    chr(0x2800 | mask),
                    style=snr_style(sum(cell) / len(cell)) if cell else "brand",
                )
            elif row == rows - 1:
                line.append(chr(0x2800 | 0x40 | 0x80), style="faint")
            else:
                line.append(chr(0x2800))
        lines.append(line)
    return lines, lo, hi


def _chart_block(
    rows: list[Text], left_caption: str, right_caption: str, chars: int
) -> list[RenderableType]:
    """Indent chart rows into the gutter and add the oldest→now caption line."""
    out: list[RenderableType] = []
    for row in rows:
        line = Text("  ")
        line.append_text(row)
        out.append(line)
    caption = Text("  ")
    caption.append(left_caption, style="faint")
    pad = chars - len(left_caption) - len(right_caption)
    caption.append(" " * max(1, pad))
    caption.append(right_caption, style="faint")
    out.append(caption)
    return out


def _heading(title: str, note: str) -> Text:
    """A section heading in the dashboard's voice: accent title, muted note."""
    text = Text(title, style="accent")
    text.append(f"  ·  {note}", style="muted")
    return text


class TimeMachineScreen(Screen):
    """One subject's history page: scrollable sections, ``w`` cycles the window."""

    floating = False
    footer_hint = "w window · ↑↓ PgUp/PgDn scroll · Esc back"

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        label: str,
        build: Callable[[Optional[timedelta], int], list[RenderableType]],
    ) -> None:
        """Create the page over its section builder.

        Args:
            session: The running TUI session (for repaints on window switch).
            label: The subject's display name (titles the screen).
            build: Renders the sections for ``(window, width)``; called once per
                window/width combination and cached — the data is stored history,
                so nothing needs re-querying per repaint.
        """
        super().__init__()
        self._session = session
        self._label = label
        self._build = build
        self._window_index = 1  # open on 7 d: enough depth to see shape, still fast
        self._cache: dict[tuple[int, int], list[str]] = {}
        self._set_title()

    def _set_title(self) -> None:
        name, _delta = _WINDOWS[self._window_index]
        self.title = f"⏳ {self._label} · {name}"

    def handle(self, action: str, data: str = "") -> None:
        """Scroll, cycle the window, or dismiss."""
        if action == "up":
            self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self.scroll_to_bottom()
        elif action == "text" and data.lower() == "w":
            self._window_index = (self._window_index + 1) % len(_WINDOWS)
            self._set_title()
            self.scroll_to_top()
            self._session.invalidate()
        elif action == "escape":
            self.resolve(None)

    def render_body(self, width: int) -> list[str]:
        """Render (or reuse) the current window's sections."""
        key = (self._window_index, width)
        lines = self._cache.get(key)
        if lines is None:
            _name, delta = _WINDOWS[self._window_index]
            lines = render_lines(Group(*self._build(delta, width)), width)
            self._cache[key] = lines
        self._scroll_total = max(1, len(lines))
        return lines


# --- the node page -----------------------------------------------------------------------


def _when_label(when: datetime) -> str:
    """A compact local timestamp for chart captions: ``Jul 04 18:30``."""
    return when.astimezone().strftime("%b %d %H:%M")


def _node_sections(
    ctx: "AppContext", node_id: str, label: str, window: Optional[timedelta], width: int
) -> list[RenderableType]:
    """Build one node's history page: volume, SNR band, rhythm, and the roll-up."""
    now = utcnow()
    since = now - window if window is not None else None
    observations = ctx.repo.node_observations(node_id, since=since)
    if not observations:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]
    start = since or observations[0].observed_at
    chars = max(20, width - 2 * _GUTTER)
    buckets = chars * 2

    out: list[RenderableType] = []
    stamps = [o.observed_at for o in observations]
    out.append(
        _heading("Volume", f"{len(observations)} receptions · oldest left")
    )
    out.extend(
        _chart_block(
            braille_bars(bucketize(stamps, start, now, buckets)),
            _when_label(start), "now", chars,
        )
    )

    snr_pairs = [
        (o.observed_at, float(o.snr)) for o in observations if o.snr is not None
    ]
    if snr_pairs:
        rows, lo, hi = band_rows(bucket_medians(snr_pairs, start, now, buckets))
        out.append(Text())
        out.append(
            _heading("SNR", f"median per slice · scale {lo:+.1f} → {hi:+.1f} dB")
        )
        out.extend(_chart_block(rows, _when_label(start), "now", chars))

    hours = [0] * 24
    for stamp in stamps:
        hours[stamp.astimezone().hour] += 1
    out.append(Text())
    out.append(_heading("Rhythm", "receptions by local hour of day"))
    out.extend(_chart_block(braille_bars(hours), "0 h", "23 h", 12))

    out.append(Text())
    out.append(_heading("Record", "this window"))
    first, last = observations[0].observed_at, observations[-1].observed_at
    line = Text("heard    ", style="muted")
    line.append(f"first {_when_label(first)} · last {_when_label(last)}")
    line.append(f"  ({_format_age(_age_seconds(last))} ago)", style="muted")
    out.append(line)
    snrs = [p[1] for p in snr_pairs]
    if snrs:
        med = median(snrs)
        line = Text("snr      ", style="muted")
        line.append(f"{med:+.1f} dB median", style=snr_style(med))
        line.append(f"  ·  worst {min(snrs):+.1f} · best {max(snrs):+.1f}", style="muted")
        out.append(line)
    rssis = [o.rssi for o in observations if o.rssi is not None]
    if rssis:
        out.append(
            Text.assemble(
                ("rssi     ", "muted"), (f"{median(rssis):.0f} dBm median", ""),
                (f"  ·  weakest {min(rssis):.0f} · strongest {max(rssis):.0f}", "muted"),
            )
        )
    kinds: dict[str, int] = {}
    for o in observations:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1
    parts = " · ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
    out.append(Text.assemble(("kinds    ", "muted"), (parts, "")))
    return out


# --- the mesh page -----------------------------------------------------------------------


def _mesh_sections(
    ctx: "AppContext", window: Optional[timedelta], width: int
) -> list[RenderableType]:
    """Build the whole-mesh overview: days, arrivals, and the all-time ledger."""
    now = utcnow()
    since = now - window if window is not None else None
    days = ctx.repo.daily_activity()
    if since is not None:
        floor = since.strftime("%Y-%m-%d")
        days = [d for d in days if d[0] >= floor]
    if not days:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]

    # One dot column per day; the width caps how many trailing days fit.
    chars = max(20, width - 2 * _GUTTER)
    shown = days[-chars * 2 :]
    out: list[RenderableType] = []
    packets = [d[1] for d in shown]
    out.append(
        _heading("Packets per day", f"UTC days · peak {max(packets)} · oldest left")
    )
    out.extend(
        _chart_block(braille_bars(packets), shown[0][0], shown[-1][0], chars)
    )

    nodes = [d[2] for d in shown]
    out.append(Text())
    out.append(_heading("Nodes per day", f"distinct nodes heard · peak {max(nodes)}"))
    out.extend(
        _chart_block(braille_bars(nodes), shown[0][0], shown[-1][0], chars)
    )

    arrivals = ctx.repo.first_seen(since=since)
    out.append(Text())
    out.append(_heading("Arrivals", "nodes heard for the first time ever"))
    if not arrivals:
        out.append(Text("none in this window", style="muted"))
    for node, name, first in arrivals[:12]:
        line = Text("  ")
        secs = _age_seconds(first)
        line.append(name or node, style=_recency_style(secs))
        line.append(f"  first heard {_when_label(first)}", style="muted")
        line.append(f"  ({_format_age(secs)} ago)", style="muted")
        out.append(line)

    all_days = ctx.repo.daily_activity()
    total_nodes = len(ctx.repo.first_seen())
    busiest = max(all_days, key=lambda d: d[1])
    out.append(Text())
    out.append(_heading("Ledger", "everything ever recorded"))
    out.append(
        Text.assemble(
            ("history  ", "muted"),
            (f"{ctx.repo.observation_count()} observations", ""),
            (f" across {len(all_days)} active days · {total_nodes} nodes", "muted"),
        )
    )
    out.append(
        Text.assemble(
            ("busiest  ", "muted"), (busiest[0], "brand"),
            (f"  {busiest[1]} packets", "muted"),
        )
    )
    return out


# --- the picker loop ---------------------------------------------------------------------


async def open_timemachine(ctx: "AppContext") -> None:
    """Run the Time Machine: pick a subject, explore its page, repeat until Esc.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi
    from .tui import Choice, Separator
    from .widgets import _DEFAULT_GLYPH

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session

    while True:
        heard = ctx.repo.heard_nodes()
        if not heard:
            await session.message_dialog(
                Text(
                    "History is empty — the recorder fills it passively while "
                    "MeshTerm listens. Come back after the mesh has talked a while.",
                    style="muted",
                ),
                title="⏳ Time machine",
            )
            return
        items: list = [
            Choice("🌐 The whole mesh — days, arrivals, the ledger", MESH),
            Separator(""),
            Separator("Nodes, most recently heard first", style="accent"),
        ]
        for node in heard:
            if not node.node:
                continue
            glyph, glyph_style = _NODE_GLYPHS.get(node.node_type, _DEFAULT_GLYPH)
            secs = _age_seconds(node.last_seen)
            row = Text()
            row.append(glyph, style=glyph_style)
            row.append(" ")
            row.append(node.name or node.node, style=_recency_style(secs))
            row.append(
                f"   {node.count}× · heard {_format_age(secs)}", style="muted"
            )
            items.append(Choice(row, (node.node, node.name or node.node)))
        picked = await session.select(
            "⏳ Time machine — pick a subject",
            items,
            prompt="Everything the recorder ever heard, explorable:",
        )
        if picked is None:
            return
        if picked == MESH:
            label = "the whole mesh"
            build = lambda window, width: _mesh_sections(ctx, window, width)  # noqa: E731
        else:
            node_id, label = picked
            build = (  # noqa: E731 - a tiny binding closure reads better than a def here
                lambda window, width, _id=node_id, _lb=label: _node_sections(
                    ctx, _id, _lb, window, width
                )
            )
        screen = TimeMachineScreen(session=session, label=label, build=build)
        await session.run_screen(screen)
