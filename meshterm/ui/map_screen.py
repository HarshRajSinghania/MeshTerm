"""The interactive full-screen map: a pannable, zoomable slippy map in the terminal.

This drives the braille street map inside the TUI. It owns a :class:`~meshterm.core.geo.
Viewport` over the mesh's nodes, fetches the vector tiles covering it in the background (so
the UI never blocks on the network), and redraws via :func:`~meshterm.ui.map_render.
render_map`. Keys:

* the **arrow keys** pan; holding **Shift** pans by a single character cell for fine
  positioning. On the PicoCalc the Shift watcher supplies the modifier the console
  strips: its keymap turns Shift+↑/↓ into PgUp/PgDn (rescued back into fine pans here —
  no physical PgUp exists there, so the code can't mean anything else) and eats
  Shift+←/→ outright — those never reach the app at all until the console keymap maps
  them back to plain arrows,
* ``PgUp`` / ``PgDn`` zoom in / out,
* ``Home`` recenters and refits to the dense core of the nodes — the *region* the mesh
  covers, and the same default view the map opens on,
* ``^U`` (for *you*) recenters on **your own node**, keeping the zoom you chose and
  clearing any find (the lane's ``You`` chip; its Shift half also zooms in close),
* **typing finds nodes**: every letter key feeds a live name filter — matching nodes keep
  bright labels while the rest dim to context, ``Backspace`` edits, ``^Enter`` frames the
  matches (keeping the query), and ``Enter`` or ``Esc`` drops the query where it is
  without moving the view (a second ``Esc`` leaves the map). This is why no plain letters
  are bound to actions here,
* ``Esc`` leaves the map.

With no network (and no cached tiles) the basemap is simply absent and nodes are plotted on a
blank grid — the map still works, it just has no streets.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from typing import TYPE_CHECKING, Callable, Optional

from ..core.geo import DEFAULT_VIEW_FRACTION, EARTH_RADIUS_KM, Viewport, clamp_lat
from ..core.mvt import Layer
from ..platforms import get_platform
from ..services import modifier_watch
from ..services.basemap import BasemapSource
from .map_render import Ghost, MapMarker, render_ground, render_map
from .tui.render import query_line
from .tui.screen import Screen

if TYPE_CHECKING:
    from ..context import AppContext

#: Fraction of the view a single (coarse) pan keypress moves.
_PAN_STEP = 0.30

#: Unit pan direction (east, south) for each directional action / key.
_PAN_DIRS: dict[str, tuple[int, int]] = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

#: How far past the tile source's max zoom the display may go (lower tiles are magnified).
_OVERZOOM = 2


def _loop_running() -> bool:
    """Whether there is an event loop to hand background work to.

    Asked *before* building a coroutine, not after: ``ensure_future`` without a loop
    raises, but by then the coroutine exists and never gets awaited, which Python reports
    as a resource warning on a path that is otherwise perfectly correct (a CLI export or a
    test rendering a map with no loop at all).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True

#: How many screens' worth of decoded tiles to keep resident, as a multiple of what the
#: current view needs. A decoded tile costs ~0.9 MB on the PicoCalc (62 bytes a point,
#: measured) against a device that has ~100 MB in total, so a map panned far enough would
#: otherwise fill memory with ground the user has left behind. Keeping the screen plus one
#: screen of history costs almost nothing to get wrong: an evicted tile comes back from
#: the decoded cache in ~25-50 ms, having already been parsed once.
_TILE_CACHE_SCREENS = 2

#: Floor on that budget, so a zoomed-out view needing one or two tiles still keeps enough
#: history for a pan away and back to be instant.
_MIN_TILE_CACHE = 8

#: The zoom a frame homes in at when the matches set no extent of their own — a
#: single node (or several at one spot) has nothing to frame, so ^Enter zooms to this
#: street-level closeness rather than the fit's neutral default. Capped at the tile
#: source's max so it never over-zooms onto blank tiles.
_FIND_ZOOM = 16


class MapScreen(Screen):
    """A full-screen, keyboard-driven map of the mesh's located nodes over an OSM basemap."""

    floating = False

    #: Whether printable keys feed the find-as-you-type node filter. The location
    #: picker turns this off: there, typing has no job and a silent filter would
    #: mysteriously dim the context markers.
    find_enabled = True

    @property
    def fkey_lane(self):
        """The PicoCalc lane: three ways to frame the view, then the zoom rocker.

        The map is the one screen that repurposes the nav actions wholesale: PgUp/PgDn
        zoom, Home reframes, and ``end`` is bound to nothing at all — so the shared lane's
        Shift-bank jumps have nothing to jump to here and F4/F5 are lone slots. That is
        also the one exception to the rule that a Home/End verb rides the Shift half of
        its pager (JP, 2026-08-08): ``Region`` is not a vertical move through a body, it
        is a *destination*, and it keeps the prime F1 slot next to the other two.

        All three destinations answer "where should I be looking?", in widening order of
        specificity: the whole **Region** the mesh covers, **You** at the centre of it, or
        just the nodes a find query **Frame**\\ s. Region always acts; You needs our own
        node to be on the map at all; Frame needs a query with matches to frame, so on an
        unfiltered map it dims (and no-ops) rather than standing in for Region.

        Two slots carry a Shift half along their own axis (JP, 2026-08-08). Behind You
        sits **You +**: the same jump home, but zoomed in close — the ``+`` borrowed from
        the zoom rocker's vocabulary, so the pair reads as "you / you, closer". Behind
        Frame sits **Clear**: the find axis's other end, dropping the query the way Frame
        commits it — lit exactly while there is a query to drop. Enter and Esc both do
        that on a keyboard; the chip is how the pair teaches it where there is no hint line.

        The zoom pair keeps the lane's handedness (see
        :data:`~meshterm.ui.tui.fkeys.DEFAULT_LANE`): out on the left, in on the right, so
        F4/F5 read as the ``−``/``+`` rocker they are.
        """
        from .tui.fkeys import FPair

        me = self._self_marker() is not None
        return [
            FPair("Region", "home"),
            FPair("You", "locate", "You +", "locate_zoom", enabled=me, opp_enabled=me),
            FPair(
                "Frame", "frame", "Clear", "clear_find",
                enabled=bool(self._filter) and bool(self._matches()),
                opp_enabled=bool(self._filter),
            ),
            FPair("Zoom -", "pagedown"),
            FPair("Zoom +", "pageup"),
        ]

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        markers: list[MapMarker],
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        saved_view: Optional[tuple[float, float, int]] = None,
        on_view_change: Optional[Callable[[Viewport], None]] = None,
        view_fraction: float = DEFAULT_VIEW_FRACTION,
        find: str = "",
    ) -> None:
        """Create the map screen.

        Args:
            session: The running TUI session (for size + repaint scheduling).
            markers: The located mesh nodes to plot (must be non-empty).
            source: The vector-tile source (already resolved/warmed).
            max_tile_zoom: The source's max zoom, captured off the event loop at open time.
            saved_view: A previously persisted ``(center_lat, center_lon, zoom)`` to reopen
                on, or ``None`` to frame the nodes instead.
            on_view_change: Called with the viewport whenever the centre or zoom changes, so
                the caller can persist it. Deduplicated — only actual changes fire it.
            view_fraction: Fraction of the nodes the default frame (and ``r`` reset) fits —
                the densest that many, so outliers don't dominate. See :meth:`geo.Viewport.fit`.
            find: A find query to open with, exactly as if the user had typed it — the
                matching nodes light and the rest dim (the node-detail page seeds its node's
                name here). Editable and Esc-clearable like any typed find; ``""`` = off.
        """
        super().__init__()
        self.title = "Map"
        self._session = session
        self._markers = markers
        self._source = source
        self._max_tile_zoom = max_tile_zoom
        self._saved_view = saved_view
        self._on_view_change = on_view_change
        self._view_fraction = view_fraction
        # The view last handed to ``on_view_change``; seeded with the restored view so
        # reopening unchanged doesn't rewrite it.
        self._last_saved = saved_view
        #: The live find-as-you-type node filter ("" = off). Every printable key lands
        #: here — the map binds no letters to actions — and rendering highlights the
        #: matching markers while dimming the rest. A caller may seed it (``find``), which
        #: behaves exactly like a query the user had already typed.
        self._filter = find
        self._viewport: Optional[Viewport] = None
        self._size: tuple[int, int] = (0, 0)  # (dot_w, dot_h) the viewport is built for
        # Ask the session to scrub the panel's right edge on the next paint (see
        # :meth:`consume_edge_scrub`). Seeded ``True`` so the first braille frame's edge is
        # cleaned even before the first pan.
        self._needs_scrub = True
        # Decoded tiles keyed by (z, x, y); a stored ``None`` means "fetched, empty/absent".
        # Decoded tiles, least-recently-shown first — see :meth:`_trim_tiles`.
        self._tiles: OrderedDict[tuple[int, int, int], Optional[list[Layer]]] = OrderedDict()
        self._pending: set[tuple[int, int, int]] = set()
        # The last finished ground frame and what it was drawn for — see :meth:`render_body`.
        # Rasterizing a downtown view is ~0.5-1 s of pure Python (tens of thousands of
        # vector features), far too slow to sit on a keystroke, so it happens off the paint
        # path and the paint serves whatever is ready.
        self._frame: Optional[list[str]] = None
        self._frame_key: Optional[tuple] = None
        # That frame's ground, kept so a view that has moved on can stand on it until its
        # own is drawn (see :meth:`_ground`).
        self._ghost: Optional[Ghost] = None
        self._drawing: Optional[tuple] = None  # the key currently being rasterized
        # The next raster to draw: its key plus the whole scene it stands for — viewport,
        # markers, find query — snapshotted at request time (see :meth:`_schedule_ground`).
        self._wanted: Optional[tuple[tuple, Viewport, list[MapMarker], str]] = None

    # --- rendering -----------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Key hints — or the live find query.

        While a find filter is active the hints give way to the query itself with its
        editing keys, so the typed text is always visible somewhere fixed.

        The line spends its whole 72-cell budget, so the two view-jump keys share one atom
        (``Home/^U region/you``) and ``⇧ fine`` — a refinement of a key the line already
        names, and the only atom here that documents a *modifier* rather than a binding —
        is the one that gives way to make room for them. The basemap's state used to hang
        off the end of this line as a suffix; it is a status atom, not a key, so it moved
        to the title where the standards chain those (see :meth:`_title`), which is what
        finally brought the line inside the budget.
        """
        if self._filter:
            return f"find: {self._filter}▏ · ^Enter frame · ⌫ erase · Enter/Esc clear"
        return "↑↓←→ pan · PgUp/PgDn zoom · Home/^U region/you · type to find · Esc back"

    def consume_edge_scrub(self) -> int:
        """Right-edge columns the session should force-repaint on the next paint (0 = none).

        A braille glyph the terminal font lacks is substituted by a *double-width* fallback,
        which shoves the row and smears the panel's right padding and border. The map body
        itself redraws wholesale as it pans, so those cells self-heal — but the static edge
        does not change frame-to-frame, so prompt_toolkit's differential paint never rewrites
        it and the smear lingers there. After a move we ask the session to force just those
        two columns (right padding + border) to repaint, scrubbing the smear without the
        whole-frame flicker a full repaint would cause.
        """
        if not self._needs_scrub:
            return 0
        self._needs_scrub = False
        return 2  # the panel's right padding cell and its right border cell

    def _query_echo(self) -> bool:
        """Whether this paint echoes the find query over the canvas's bottom row.

        Only where the footer isn't drawn (:attr:`~meshterm.platforms.Platform.footer_fkeys`):
        there the hint line carrying the query never reaches the screen, so without this row
        the map would silently filter itself while the reader has no idea what they typed.
        On the desktop the footer already shows it and the canvas keeps the whole body.
        """
        return bool(self._filter) and get_platform().footer_fkeys

    def render_body(self, width: int) -> list[str]:
        """Build (or resize) the viewport, ensure its tiles, and render the frame.

        Where the platform needs a body-line query echo (see :meth:`_query_echo`), it is
        drawn *over* the canvas's last row — directly above the F-key lane — rather than
        stacked above the map: an extra head line used to shift the whole ground down a
        row the moment a find began (JP, 2026-08-08). Overlaying costs a strip of ground
        behind the echo while a query is live, but the viewport itself never resizes, so
        nothing jumps and the pan/zoom geometry holds steady.
        """
        vp = self._ensure_viewport(width)
        self._ensure_tiles(vp)
        self._persist()
        lines = self._ground(vp)
        self.title = self._title(vp)
        if lines and self._query_echo():
            lines[-1] = query_line(self._filter, width)
        return lines

    def _ensure_viewport(self, width: int) -> Viewport:
        """The viewport for a body ``width`` cells wide — built on first paint, else resized.

        Split out of :meth:`render_body` because a subclass may need the view *before* it
        renders anything: the picker's crosshair rides the centre, so it has to know where
        the centre is to build its marker (see :meth:`LocationPickScreen.render_body`).
        """
        _, cell_h = self._session.base_body_size()
        dot_w, dot_h = width * 2, max(1, cell_h) * 4
        if self._viewport is None:
            self._viewport = self._initial_viewport(dot_w, dot_h)
            self._size = (dot_w, dot_h)
        elif self._size != (dot_w, dot_h):
            self._viewport = self._viewport.resized(dot_w, dot_h)
            self._size = (dot_w, dot_h)
        return self._viewport

    def _ground_key(self, vp: Viewport) -> tuple:
        """Everything the rasterized ground is a function of.

        The tile *identities* go in the key rather than their contents: a tile's decoded
        layers never change once loaded, so a tile arriving is the only way the picture can
        gain detail, and that shows up here as a new key.
        """
        loaded = tuple(t for t in vp.tiles(self._max_tile_zoom) if self._tiles.get(t))
        return (vp, loaded, self._filter, len(self._markers))

    def _ground(self, vp: Viewport) -> list[str]:
        """The map picture for ``vp`` — from the last raster if it still applies, else soon.

        Rasterizing a view is 0.5-1 s of pure Python on the PicoCalc (a downtown frame
        projects tens of thousands of vector features), so it cannot happen between a
        keypress and the paint that answers it. Instead the paint always returns
        immediately, with the best picture available *right now*, and a background task
        draws the real one and asks for a repaint when it lands:

        * **Nothing has changed** — the finished raster is exactly this view: serve it.
        * **Only the tiles changed** (a fetch landed, the view did not move) — the previous
          raster is still correctly aligned, just missing some streets. Keep showing it
          rather than blanking a good picture to redraw the same ground.
        * **The view moved** — the old raster is in the wrong place *as a picture*, but the
          ground it drew is still the only ground anyone has: reproject it onto the new
          viewport (:class:`~meshterm.ui.map_render.Ghost`) and draw the nodes over it at
          their real positions. That costs a few milliseconds, so panning still tracks the
          keys exactly, and the streets slide with the view — dimmed, and short of the
          edge you are panning onto — instead of the map blanking to black between every
          keypress and flashing back when the frame lands (JP, 2026-08-09).
        """
        key = self._ground_key(vp)
        if self._frame_key == key and self._frame is not None:
            return list(self._frame)

        self._schedule_ground(key, vp)
        if self._frame is not None and self._frame_key is not None:
            # Everything but the tiles (key[1]) has to match: a frame drawn for a different
            # marker set is not "the same picture missing streets", it is a picture missing
            # a node — the picker's crosshair, say.
            if self._frame_key[0] == key[0] and self._frame_key[2:] == key[2:]:
                return list(self._frame)  # same view, only tiles differ — still aligned
        # The view moved (or nothing has ever been drawn): markers over the last ground.
        return render_map(vp, {}, self._markers, find=self._filter, ghost=self._ghost)

    def _schedule_ground(self, key: tuple, vp: Viewport) -> None:
        """Note that ``key`` wants drawing, and start on it if nothing else is in flight.

        Exactly **one** raster runs at a time, and it is always the newest one asked for.
        A held arrow key hands us a new viewport on every repaint, and a render is most of
        a second: starting one per keypress would pile up a queue of thread-bound work,
        each frame of it already stale on arrival, and the contention would slow the very
        keystrokes this is meant to keep quick. So a request that arrives mid-draw only
        replaces the pending one, and the draw that finishes picks it up.

        The markers and the find query are **snapshotted here**, alongside the viewport, so
        the raster draws the very scene ``key`` stands for. The draw itself happens later,
        on a thread, long after the paint that asked for it returned — and a marker list
        can be per-frame: the picker's crosshair is appended for the duration of one
        ``render_body`` and taken straight back out (see
        :meth:`LocationPickScreen.render_body`). Reading it at draw time would find it
        gone, and the finished basemap would land over the crosshair and erase it.
        """
        if key == self._drawing or key == self._frame_key:
            return
        self._wanted = (key, vp, list(self._markers), self._filter)
        if self._drawing is None:
            self._start_ground()

    def _start_ground(self) -> None:
        """Begin the pending raster, or draw it inline where there is no event loop."""
        if self._wanted is None:
            return
        key, vp, markers, find = self._wanted
        self._wanted = None
        tiles = {t: self._tiles.get(t) for t in vp.tiles(self._max_tile_zoom)}
        if not _loop_running():
            # A static render (the CLI's map export, a test): there is nothing to be
            # responsive *to*, so draw it here and now rather than never.
            self._drawing = None
            self._frame, self._ghost = render_ground(vp, tiles, markers, find=find)
            self._frame_key = key
            return
        self._drawing = key
        asyncio.ensure_future(self._draw_ground(key, vp, tiles, markers, find))

    async def _draw_ground(
        self, key: tuple, vp: Viewport, tiles: dict, markers: list[MapMarker], find: str
    ) -> None:
        """Rasterize one view off the event loop, then repaint and take the next request.

        The work is pure Python, so a thread does not truly run it in parallel — but the
        interpreter still switches between threads every few milliseconds, which is the
        whole point: keystrokes keep being serviced throughout instead of waiting for the
        frame (measured worst-case delay ~50 ms, against the ~1 s of a blocking draw).

        Everything the frame is a function of arrives as an argument (see
        :meth:`_schedule_ground`) — the screen's own state may have moved on by the time
        the thread runs, and the frame is filed under the key of the scene it was asked
        for, so it must *be* that scene.
        """
        try:
            drawn = await asyncio.to_thread(
                render_ground, vp, tiles, markers, find=find
            )
        except Exception:  # noqa: BLE001 - a frame we couldn't draw is one we draw again
            drawn = None
        self._drawing = None
        if drawn is not None:
            lines, self._ghost = drawn
            self._frame, self._frame_key = lines, key
            self._needs_scrub = True
            self._session.invalidate()
        if self._wanted is not None:
            self._start_ground()

    def _initial_viewport(self, dot_w: int, dot_h: int) -> Viewport:
        """Restore the saved view (clamped to sane bounds) or frame the nodes' dense core.

        With no saved view the default frames the half of the nodes nearest the median
        centre, so distant outliers don't zoom the whole mesh out to a useless scale.
        """
        if self._saved_view is not None:
            lat, lon, zoom = self._saved_view
            z = max(2, min(int(zoom), self._max_tile_zoom + _OVERZOOM))
            return Viewport(clamp_lat(lat), lon, z, dot_w, dot_h)
        return Viewport.fit(
            [(m.lat, m.lon) for m in self._markers],
            dot_w,
            dot_h,
            max_zoom=self._max_tile_zoom,
            fraction=self._view_fraction,
        )

    def _persist(self) -> None:
        """Hand the current centre/zoom to ``on_view_change`` if it changed since last time."""
        vp = self._viewport
        if vp is None or self._on_view_change is None:
            return
        view = (vp.center_lat, vp.center_lon, vp.zoom)
        if view == self._last_saved:
            return
        self._last_saved = view
        self._on_view_change(vp)

    def _matches(self) -> list[MapMarker]:
        """The markers the live find filter currently matches (all of them when off)."""
        needle = self._filter.strip().casefold()
        if not needle:
            return self._markers
        return [m for m in self._markers if needle in m.label.casefold()]

    def _title(self, vp: Viewport) -> str:
        """A compact status title: zoom, node count (find matches), and ground scale.

        The basemap's own state — tiles still in flight, or no source at all — is the last
        atom when there is one to report, standing in for the scale rather than joining it.
        Both are answers to "how much ground am I looking at", the status is the more urgent
        of the two while it lasts, and swapping (rather than appending) keeps the title from
        outgrowing a 53-column title bar, which clips rather than wraps.
        """
        # Ground metres per braille dot at the view centre, for a rough sense of scale.
        m_per_dot = (
            2 * math.pi * EARTH_RADIUS_KM * 1000
            * math.cos(math.radians(vp.center_lat))
            / (256 * (2**vp.zoom))
        )
        scale = f"{m_per_dot * vp.dot_w:.0f} m across" if m_per_dot * vp.dot_w < 1000 else \
            f"{m_per_dot * vp.dot_w / 1000:.1f} km across"
        if self._pending:
            scale = f"{len(self._pending)} tiles…"
        elif self._drawing is not None or self._wanted is not None:
            # The ground for this view is still being rasterized off the paint path (see
            # :meth:`_ground`), so what is on screen is the nodes alone, or the last view's
            # streets. Say so, in the same slot the tile fetch reports from.
            scale = "drawing…"
        elif not self._source.available:
            scale = "offline"
        if self._filter:
            nodes = f"{len(self._matches())} of {len(self._markers)} match"
        else:
            nodes = f"{len(self._markers)} nodes"
        return f"Map · z{vp.zoom} · {nodes} · {scale}"

    # --- tiles ---------------------------------------------------------------

    def _ensure_tiles(self, vp: Viewport) -> None:
        """Schedule background fetches for any visible tiles not yet loaded or pending."""
        wanted = vp.tiles(self._max_tile_zoom)
        for t in wanted:
            if t in self._tiles:
                self._tiles.move_to_end(t)  # on screen now, so last in line to be dropped
        self._trim_tiles(len(wanted))
        if not self._source.available:  # offline (resolved at open time) — nodes only
            return
        if not _loop_running():  # nothing to fetch onto — a static render draws what it has
            return
        for t in wanted:
            if t in self._tiles or t in self._pending:
                continue
            self._pending.add(t)
            asyncio.ensure_future(self._load(t))

    def _trim_tiles(self, in_view: int) -> None:
        """Release the least recently shown tiles once the view's budget is exceeded.

        Only tiles holding geometry are counted or dropped. An entry whose value is
        ``None`` is the memory of having *asked* — the tile was absent, or the source
        never answered — and it costs a dict slot rather than a megabyte, so it stays;
        dropping it would only buy a pointless re-request on the next repaint.

        Args:
            in_view: How many tiles the current viewport needs, which sets the budget.
        """
        budget = max(_MIN_TILE_CACHE, in_view * _TILE_CACHE_SCREENS)
        loaded = sum(1 for layers in self._tiles.values() if layers)
        if loaded <= budget:
            return
        # Oldest first; everything on screen was just moved to the end, so it is safe.
        for key in list(self._tiles):
            if loaded <= budget:
                break
            if self._tiles[key]:
                del self._tiles[key]
                loaded -= 1

    async def _load(self, t: tuple[int, int, int]) -> None:
        """Fetch+decode one tile off the event loop, then repaint."""
        try:
            layers = await asyncio.to_thread(self._source.load_tile, *t)
        except Exception:  # noqa: BLE001 - a failed tile is just an absent one
            layers = None
        self._tiles[t] = layers
        self._pending.discard(t)
        self._session.invalidate()

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Pan, zoom, reframe, edit the find filter, or exit.

        Every printable key feeds the find filter — nothing pans or zooms by letter, so
        typing a node name can never fling the view around. Esc peels one layer: an
        active filter first, the map itself only once the filter is clear.

        Three actions reframe the view, and each has both a key and an F-key chip:
        ``home`` the whole region, ``locate`` (^U, for *you*) our own node — ``locate_zoom``
        the Shift-bank variant that also homes in — and ``frame`` (^Enter) the find
        matches. ``clear_find`` drops the query without moving the view, which is also
        what plain **Enter** does.

        Enter and ^Enter are the find's two ways out, and they split along whether the
        *view* moves (JP, 2026-08-09). Typing a query dims everything that doesn't match,
        and the common finish is "yes, that one — now let me look around it": the query
        has done its job and only the dimming is in the way, so Enter drops the query and
        leaves the view exactly where the reader put it. ^Enter is the other finish —
        *take* me to them — and it keeps the query, because a frame is a place to arrive
        and framing tighter from there is one more press, not a re-type.
        """
        vp = self._viewport
        if action == "escape":
            if self._filter:
                self._filter = ""
            else:
                self.resolve(None)
                return
        if vp is None:
            return
        if action in _PAN_DIRS:
            # A shifted arrow the console reports as the bare arrow (a keymap that
            # strips the modifier) still fine-pans: the watcher knows whether Shift is
            # physically held, and it stays False wherever it isn't watching — desktop
            # terminals report shift_up/... themselves, on the branch below.
            self._pan(vp, action, fine=modifier_watch.shift_down())
        elif action.startswith("shift_") and action[len("shift_"):] in _PAN_DIRS:
            self._pan(vp, action[len("shift_"):], fine=True)
        elif action == "pageup":
            if modifier_watch.shift_down():
                # The PicoCalc console's keymap translates Shift+↑ into PgUp (measured
                # on-device, JP 2026-08-09: shifted vertical arrows were zooming). No
                # physical PgUp exists on that keyboard — the pager rides the F-lane —
                # so a raw PgUp with Shift held can only *be* a shifted arrow: fine-pan.
                self._pan(vp, "up", fine=True)
            else:
                self._viewport = vp.zoomed(1, max_zoom=self._max_tile_zoom + _OVERZOOM)
        elif action == "pagedown":
            if modifier_watch.shift_down():
                self._pan(vp, "down", fine=True)  # Shift+↓ arrives as PgDn — see above
            else:
                self._viewport = vp.zoomed(-1)
        elif action in ("home", "ctrl_home"):
            self._reset_view(vp)
        elif action == "locate":
            self._locate(vp)
        elif action == "locate_zoom":
            self._locate(vp, zoom_in=True)
        elif action in ("clear_find", "enter"):
            self._filter = ""
        elif action in ("frame", "ctrl_enter") and self._filter:
            self._frame_matches(vp)
        elif action == "text" and self.find_enabled:
            if not data.isspace() or self._filter:  # never begin the filter with a space
                self._filter += data
        elif action == "space" and self._filter:
            self._filter += " "  # node names carry spaces; only meaningful mid-query
        elif action == "backspace":
            self._filter = self._filter[:-1]
        # Any handled key may have redrawn the body, so clean the right edge next paint.
        self._needs_scrub = True
        self._persist()

    def _self_marker(self) -> Optional[MapMarker]:
        """Our own node among the markers, or ``None`` when the map can't place us.

        A device with no location fix of its own is simply absent from the marker list, so
        ``You`` has nowhere to go and its chip dims (see :attr:`fkey_lane`).
        """
        return next((m for m in self._markers if m.is_self), None)

    def _locate(self, vp: Viewport, *, zoom_in: bool = False) -> None:
        """Recentre on our own node (``^U`` / the ``You`` chip), clearing any find.

        Plain ``You`` keeps the zoom the user chose — pressing it twice does the same
        thing twice, and pairing it with one Zoom + is a single extra press. Its Shift
        half (``You +``) is the one that also homes in, at the same street-level
        closeness a single-match Frame lands on (:data:`_FIND_ZOOM`). Both clear the
        find query (JP, 2026-08-08): jumping home while a filter dims the rest — or
        matches nothing, us included — reads as starting over, and the map should agree.
        """
        me = self._self_marker()
        if me is None:
            return
        self._filter = ""
        zoom = min(_FIND_ZOOM, self._max_tile_zoom) if zoom_in else vp.zoom
        self._viewport = Viewport(clamp_lat(me.lat), me.lon, zoom, vp.dot_w, vp.dot_h)

    def _reset_view(self, vp: Viewport) -> None:
        """Refit the view to the nodes' dense core (the map's opening frame)."""
        self._viewport = Viewport.fit(
            [(m.lat, m.lon) for m in self._markers],
            vp.dot_w,
            vp.dot_h,
            max_zoom=self._max_tile_zoom,
            fraction=self._view_fraction,
        )

    def _frame_matches(self, vp: Viewport) -> None:
        """Refit the view around the find filter's matches (^Enter on an active find).

        All matches are framed (``fraction=1.0`` — the user asked for exactly these
        nodes, so no dense-core trimming). A single match — or several at one spot — has
        no extent to frame, so instead of the fit's neutral default the view homes in
        close on it (:data:`_FIND_ZOOM`, capped at the tile source's max so it never
        over-zooms onto blank tiles). No matches at all leaves the view alone.
        """
        matches = self._matches()
        if not matches:
            return
        self._viewport = Viewport.fit(
            [(m.lat, m.lon) for m in matches],
            vp.dot_w,
            vp.dot_h,
            max_zoom=self._max_tile_zoom,
            default_zoom=min(_FIND_ZOOM, self._max_tile_zoom),
            fraction=1.0,
        )

    def _pan(self, vp: Viewport, direction: str, *, fine: bool) -> None:
        """Pan by one coarse step, or — when ``fine`` — a single character cell.

        A character cell is 2 braille dots wide and 4 tall, so the fine step is that many
        dots expressed as a fraction of the current view.
        """
        dx, dy = _PAN_DIRS[direction]
        if fine:
            self._viewport = vp.panned(dx * 2 / vp.dot_w, dy * 4 / vp.dot_h)
        else:
            self._viewport = vp.panned(dx * _PAN_STEP, dy * _PAN_STEP)


class LocationPickScreen(MapScreen):
    """The map, repurposed as a coordinate picker: pan the crosshair, Enter to choose.

    Used by the config editor to set the node's advertised location by *pointing at the
    map* instead of typing degrees. It is a :class:`MapScreen` with three changes: a
    crosshair marker rides the view centre (labelled with the live coordinates, so the
    user always sees exactly what they're about to pick), Enter resolves with the
    centre's ``(lat, lon)`` instead of framing find matches (find is off here — see
    :attr:`MapScreen.find_enabled`), and ``Home`` recentres on the *initial* location
    rather than refitting the node cloud. The surrounding mesh nodes are still drawn, so
    placing yourself relative to a known repeater is easy. Esc cancels (resolves CANCEL,
    surfaced as ``None`` by the caller).
    """

    find_enabled = False

    @property
    def fkey_lane(self):  # type: ignore[override]
        """The map's lane minus the two verbs a picker has no use for.

        ``Frame`` goes because find is off here — nothing can ever be typed to frame, so
        the slot is *empty*, not dim. ``You`` goes because the whole screen is about
        choosing where "you" will be: the crosshair at the centre already is that answer,
        and a chip that jumped to wherever the node currently claims to be would compete
        with the pick rather than help it. ``Home`` keeps F1, but reframed: here it returns
        to the view the picker **opened** on, so a pan that went wrong is one key to undo.
        """
        from .tui.fkeys import FPair

        return [FPair("Start", "home"), None, None, FPair("Zoom -", "pagedown"),
                FPair("Zoom +", "pageup")]

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        markers: list[MapMarker],
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        initial: Optional[tuple[float, float]] = None,
        zoom: int = 13,
    ) -> None:
        """Create the picker.

        Args:
            session: The running TUI session (for size + repaint scheduling).
            markers: Located mesh nodes to draw for context (may be empty).
            source: The vector-tile source (already resolved/warmed).
            max_tile_zoom: The source's max zoom, captured off the event loop at open time.
            initial: The location to open centred on (the node's current position), or
                ``None`` to frame the mesh instead (falling back to a world view when no
                nodes are located either).
            zoom: The zoom to open at when ``initial`` is given.
        """
        saved = (initial[0], initial[1], zoom) if initial is not None else None
        super().__init__(session, markers, source, max_tile_zoom, saved_view=saved)
        self._initial = initial
        self._pick_zoom = zoom

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Key hints for picking, plus the live tile-loading indicator."""
        base = "↑↓←→ pan · ⇧ fine · PgUp/PgDn zoom · Enter set location · Esc cancel"
        if self._pending:
            return f"{base} · [muted]loading {len(self._pending)} tiles…[/muted]"
        if not self._source.available:
            return f"{base} · [warn]offline — no basemap[/warn]"
        return base

    def _initial_viewport(self, dot_w: int, dot_h: int) -> Viewport:
        """Open on the initial location, else frame the nodes, else a world view."""
        if self._saved_view is None and not self._markers:
            return Viewport(20.0, 0.0, 2, dot_w, dot_h)  # nothing to frame — the world
        return super()._initial_viewport(dot_w, dot_h)

    def render_body(self, width: int) -> list[str]:
        """Render the map with the crosshair marker pinned to the view centre.

        The crosshair is a transient marker appended for just this frame (never stored in
        :attr:`_markers`), drawn in the "self" style so it reads as *your* position-to-be
        and labelled with the live coordinates it would commit. The viewport is settled
        first (:meth:`~MapScreen._ensure_viewport`) so the crosshair rides the centre from
        the very first frame, and rides the *resized* centre when the window changes.

        Because the marker only exists for the duration of this call, every raster it
        should appear in has to be requested from inside it — which is why the background
        draw snapshots the scene rather than reading it back later (see
        :meth:`~MapScreen._schedule_ground`); otherwise the finished basemap would land
        over the crosshair and the picker would lose sight of what it is picking.
        """
        real = self._markers
        vp = self._ensure_viewport(width)
        cross = MapMarker(
            label=f"⌖ {vp.center_lat:.5f}, {vp.center_lon:.5f}",
            lat=vp.center_lat,
            lon=vp.center_lon,
            is_self=True,
        )
        self._markers = real + [cross]
        try:
            return super().render_body(width)
        finally:
            self._markers = real

    def _title(self, vp: Viewport) -> str:
        """A live status title: the coordinates under the crosshair and the scale."""
        base = super()._title(vp)
        scale = base.rsplit("·", 1)[-1].strip()
        return f"Set location · {vp.center_lat:.5f}, {vp.center_lon:.5f} · z{vp.zoom} · {scale}"

    def handle(self, action: str, data: str = "") -> None:
        """Commit the centre on Enter; ``Home`` returns to the initial spot; else map keys."""
        if action == "enter":
            vp = self._viewport
            if vp is not None:
                self.resolve((vp.center_lat, vp.center_lon))
            return
        if action in ("home", "ctrl_home") and self._viewport is not None:
            # Reset returns to the *starting* view — the initial location when one was
            # given, else the node frame — rather than refitting a cloud that now includes
            # nowhere in particular. With neither, fall back to the world view.
            vp = self._viewport
            if self._initial is not None:
                lat, lon = self._initial
                self._viewport = Viewport(
                    clamp_lat(lat), lon, self._pick_zoom, vp.dot_w, vp.dot_h
                )
            elif not self._markers:
                self._viewport = Viewport(20.0, 0.0, 2, vp.dot_w, vp.dot_h)
            else:
                super().handle(action, data)
                return
            self._needs_scrub = True
            return
        super().handle(action, data)


async def pick_location(
    ctx: "AppContext", *, initial: Optional[tuple[float, float]] = None
) -> Optional[tuple[float, float]]:
    """Open the full-screen map as a coordinate picker; return ``(lat, lon)`` or ``None``.

    Gathers the mesh's located nodes for context (best-effort — an unreachable radio just
    means a barer map), warms the tile source off the event loop, then runs a
    :class:`LocationPickScreen` until the user commits a spot with Enter or backs out
    with Esc.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        initial: The location to open centred on (e.g. the node's current coordinates),
            or ``None`` to frame the mesh.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    import asyncio

    from ..tools.map import gather_markers
    from .surface import TuiUi
    from .tui.screen import CANCEL

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive map is only available in the menu")
    session = ctx.ui.session
    try:
        markers = await gather_markers(ctx)
    except Exception:  # noqa: BLE001 - context markers are a nicety, never a requirement
        markers = []
    source = basemap_source(ctx)
    max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
    screen = LocationPickScreen(
        session, markers, source, max_zoom, initial=initial
    )
    try:
        result = await session.run_screen(screen)
    finally:
        # Same clean-slate repaint as open_map: the braille may have smeared the terminal.
        session.request_full_repaint()
    return None if result is CANCEL or result is None else result


def basemap_source(ctx: "AppContext") -> BasemapSource:
    """The session's shared vector-tile source (see :attr:`AppContext.basemap_source`).

    Memoized on the context, so the one-off TileJSON resolve is paid once for the whole
    session instead of on every map open or Node-detail location preview.
    """
    return ctx.basemap_source


async def open_map(
    ctx: "AppContext",
    markers: list[MapMarker],
    *,
    focus: Optional[tuple[float, float]] = None,
    find: Optional[str] = None,
    fraction: float = DEFAULT_VIEW_FRACTION,
) -> None:
    """Open the interactive full-screen map over ``markers`` and run until dismissed.

    Warms the tile source off the event loop (so the first paint doesn't block on the
    network), then pushes the :class:`MapScreen` and awaits its dismissal.

    Args:
        ctx: Shared application context (must be in the interactive menu).
        markers: The located mesh nodes to plot (non-empty).
        focus: A ``(lat, lon)`` to open centred on — the node you opened the map from,
            say — instead of the persisted "where you left the map" view. A focused open
            is a transient peek: it deliberately wires no ``on_view_change``, so panning
            around it never overwrites that saved view and the Map tool still reopens
            where the user last left it.
        find: A find query to open with — the focused node's name, so it lights among the
            rest exactly as if the user had typed it. ``None`` opens with the find off.
        fraction: Fraction of the nodes the default view frames (see :class:`MapScreen`).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive map is only available in the menu")
    session = ctx.ui.session
    source = basemap_source(ctx)
    # Resolve the tile template/zoom in a worker thread so the UI thread never blocks.
    max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
    if focus is not None:
        # Open on the focused node — matching the detail preview's centre and zoom
        # (``min(13, max_zoom)``) so the full map is visibly the same place, larger. No
        # ``on_view_change``: a focused peek must leave the persisted global view alone.
        lat, lon = focus
        screen = MapScreen(
            session,
            markers,
            source,
            max_zoom,
            saved_view=(clamp_lat(lat), lon, min(13, max_zoom)),
            view_fraction=fraction,
            find=find or "",
        )
    else:
        screen = MapScreen(
            session,
            markers,
            source,
            max_zoom,
            saved_view=ctx.repo.get_map_view(),
            on_view_change=lambda vp: ctx.repo.set_map_view(
                vp.center_lat, vp.center_lon, vp.zoom
            ),
            view_fraction=fraction,
        )
    try:
        await session.run_screen(screen)
    finally:
        # The map's braille may have smeared the terminal via double-width fallback glyphs
        # that prompt_toolkit's diff can't see; force one full repaint so the menu drawn
        # underneath starts from a clean slate rather than inheriting that garbage.
        session.request_full_repaint()
