"""The dual-platform gallery: every screen the interactive menu can open, checked for
width discipline under both flavours this app runs as.

Each entry below builds one full-screen :class:`~meshterm.ui.tui.screen.Screen` against
hand-built (simulator-shaped) data — the same "fake session, real screen" approach every
individual screen's own test file already uses — then renders it, both directly
(``render_body``) and through the real frame compositor (``compose_base``), at REGULAR's
72x24 and at PICOCALC's two live/lux row counts (53x26, the actual on-device floor; 53x40,
the boot-font/6x8-font-B case). No rendered line may exceed its terminal's width.

This is the harness described in .claude/plans/picocalc-platform.md's "Parity" section: a
screen added here without surviving both platforms is meant to fail CI by default, so the
suite polices new work automatically instead of relying on someone remembering to check
by eye. It intentionally starts already covering every screen the interactive menu can
open (not growing empty) — a screen that doesn't yet fit 53 columns is marked ``xfail`` in
:data:`_KNOWN_WIDE` instead of being left out, so that list *is* the width-reduction
worklist P6 works through screen by screen; a screen graduates off it the moment its case
starts reporting XPASS.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pytest
from rich.cells import cell_len
from rich.text import Text

from meshterm.core.courier_store import CourierStore
from meshterm.core.models import (
    ChatMessage,
    Contact,
    Conversation,
    Hop,
    Observation,
    TraceResult,
    TraceStats,
    TxLevelResult,
    utcnow,
)
from meshterm.core.watch_store import WatchStore
from meshterm.persistence.repository import DiscoveredPath
from meshterm.platforms import PICOCALC, REGULAR, Platform, set_platform
from meshterm.ui.fontset import FONT_CODEPOINTS
from meshterm.services.courier import CourierService
from meshterm.services.message_paths import Arrival
from meshterm.services.monitor_service import ACTIVITY_BUCKETS
from meshterm.services.records import CATEGORY_BY_ID
from meshterm.services.topology import MeshTopology, build_topology
from meshterm.ui.chat import ChatScreen
from meshterm.ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
from meshterm.ui.contacts_screen import ContactsScreen
from meshterm.ui.courier_screen import CourierOutboxScreen
from meshterm.ui.dashboard_screen import DashboardScreen
from meshterm.ui.livefeed_screen import LiveFeedScreen
from meshterm.ui.map_render import MapMarker
from meshterm.ui.map_screen import MapScreen
from meshterm.ui.message_paths_screen import MessagePathsScreen
from meshterm.ui.node_detail_screen import NodeDetailScreen, _Action, _RoutesView, _Tab
from meshterm.ui.packet_viewer import PacketEntry, PacketViewer
from meshterm.ui.path_composer import PathComposerScreen
from meshterm.ui.records_screen import RecordDialog
from meshterm.ui.remote_cli import RemoteCliScreen
from meshterm.ui.timemachine_screen import TimeMachineScreen
from meshterm.ui.trace_screen import TraceScreen
from meshterm.ui.tui import Screen, frame
from meshterm.ui.tx_screen import TxSweepScreen
from meshterm.ui.walk_screen import WalkScreen
from meshterm.ui.widgets import ContactsSort, highlighted_hash

from tests.conftest import plain as _plain

_HUB_KEY = "3d63c6429436" + "0" * 52
_FAR_KEY = "f2c24f54551e" + "0" * 52


class _GallerySession:
    """A minimal TuiSession stand-in every gallery screen is happy with (the same shape
    each screen's own test file rolls independently as ``_FakeSession``/``_StubSession``).
    """

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.repaints = 0
        self.stack: list = []
        self._cols, self._rows = cols, rows

    def invalidate(self) -> None:
        self.repaints += 1

    def push(self, screen) -> None:  # noqa: ANN001
        self.stack.append(screen)

    def pop(self, screen=None) -> None:  # noqa: ANN001
        if screen is None:
            self.stack.pop()
        elif screen in self.stack:
            self.stack.remove(screen)

    def base_body_size(self) -> tuple[int, int]:
        return self._cols, self._rows

    async def button_dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001
        return None


@dataclass
class _Entry:
    """One gallery specimen: a name, and how to build it at a given terminal size."""

    name: str
    factory: Callable[[int, int], Screen]
    #: Whether this screen ends in the app's standard exit group (a blank line then the
    #: bare word "Back") — true only for the ``SelectScreen``-family list screens; the rest
    #: exit via Esc (their ``footer_hint`` says so) with no dedicated exit row to find.
    has_exit_group: bool = False


# --- factories: one per screen the interactive menu can open ---------------------------


def _dashboard(cols: int, rows: int) -> Screen:
    return DashboardScreen(
        session=_GallerySession(cols, rows),
        resolve=lambda h: {"a1b2c3d4": "Alice", "3d63c642": "YUL-Cartierville"}.get(h, ""),
        window=[
            Observation(node="a1b2c3d4", name="a1b2c3d4", kind="advert", snr=5.0, rssi=-90.0,
                        observed_at=utcnow()),
            Observation(node="3d63c642", name="3d63c642", kind="packet", snr=-2.0, rssi=-104.0,
                        observed_at=utcnow()),
        ],
        activity=lambda: (2.0,) * ACTIVITY_BUCKETS,
        activity_flags=lambda: (True,) * ACTIVITY_BUCKETS,
        kind_counts=lambda: {"advert": 5, "ack": 2, "packet": 3},
    )


def _contacts(cols: int, rows: int) -> Screen:
    sort = ContactsSort.from_name("name", SORT_COLUMNS, SORT_OPENS_ASCENDING)
    contacts = [
        Contact(name="Alice", public_key="aa" * 32),
        Contact(name="A Rather Long Repeater Name For Width", public_key=_HUB_KEY),
    ]
    return ContactsScreen("Homestead", "cc" * 32, contacts, 1, {"aa" * 6: 7}, sort)


def _node_detail_header() -> Text:
    header = Text("▲ ", style="#a78bfa")
    header.append("YUL-Cartierville", style="accent")
    header.append("   repeater", style="muted")
    return header


def _node_detail(cols: int, rows: int) -> Screen:
    return NodeDetailScreen(
        title="Node — YUL-Cartierville",
        header=_node_detail_header(),
        info_rows=[
            ("key", highlighted_hash(_HUB_KEY, 1)),
            ("heard", Text("5m ago")),
            ("packets", Text("42")),
        ],
        tabs=[_Tab("Info", "info"), _Tab("Routes", "routes")],
        minimap=None,
        map_caption=None,
        routes=_RoutesView(note="no route observed yet — trace to discover one"),
        info_actions=[_Action("timemachine", "⏳", "", "Time machine — 42 receptions")],
        trace_action=_Action("trace", "\U0001f3af", "", "Trace — auto route …"),
        tail_actions=[_Action("back", "", "", "Back")],
    )


class _StubTileSource:
    """An always-offline tile source: the gallery renders markers only, no network."""

    available = False
    max_zoom = 14

    def load_tile(self, z: int, x: int, y: int):  # noqa: ANN001
        return None


def _map(cols: int, rows: int) -> Screen:
    session = _GallerySession(cols, rows)
    markers = [
        MapMarker("Homestead", 45.50, -73.60, is_self=True),
        MapMarker("YUL-Cartierville", 45.40, -73.50, is_repeater=True),
        MapMarker("A Rather Long Node Name For Width", 45.55, -73.65),
    ]
    return MapScreen(session, markers, _StubTileSource(), 14)


def _chat(cols: int, rows: int) -> Screen:
    conv = Conversation(
        label="Alice", is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    messages = [
        ChatMessage(text="on my way, should be there soon", outbound=False, peer="d4e5f6a7"),
        ChatMessage(text="sounds good, see you shortly", outbound=True, peer="d4e5f6a7", acked=True),
    ]
    return ChatScreen(
        conv, messages, send=None, names={"d4e5f6a7": "Alice"}, session=_GallerySession(cols, rows),
    )


def _livefeed(cols: int, rows: int) -> Screen:
    return LiveFeedScreen(
        session=_GallerySession(cols, rows),
        resolve=lambda h: {"a1b2c3d4": "Alice"}.get(h, ""),
        seed=[
            Observation(node="a1b2c3d4", name="a1b2c3d4", kind="advert", snr=5.0, rssi=-90.0,
                        observed_at=utcnow()),
        ],
        hub_active=lambda: True,
    )


def _walk_topo() -> tuple[MeshTopology, dict[str, Contact]]:
    hub = Contact(name="YUL-Cartierville", public_key=_HUB_KEY, key_prefix="3d63c6429436")
    far = Contact(name="Alice", public_key=_FAR_KEY, key_prefix="f2c24f54551e")
    topo = MeshTopology("aa" * 6, contacts=[hub, far])
    hub_id, far_id = topo.canonical(hub.public_key), topo.canonical(far.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, hub_id], snrs=[6.0], when=when, source="trace")
    topo.add_walk([hub_id, far_id], snrs=[-2.0], when=when, source="packet")
    return topo, {hub_id: hub, far_id: far}


def _walk(cols: int, rows: int) -> Screen:
    topo, contacts = _walk_topo()
    return WalkScreen(
        session=_GallerySession(cols, rows), topo=topo, contacts=contacts, self_label="Homestead",
    )


def _timemachine(cols: int, rows: int) -> Screen:
    def build(window, width):  # noqa: ANN001
        return [Text("YUL-Cartierville  5m ago  advert"), Text("Alice  12m ago  packet")]

    return TimeMachineScreen(
        session=_GallerySession(cols, rows), label="YUL-Cartierville", build=build,
    )


def _message_paths(cols: int, rows: int) -> Screen:
    message = ChatMessage(text="on my way, should be there soon", is_channel=True,
                           created_at=utcnow())
    arrivals = [
        Arrival(when=utcnow(), hops=("3d63c6",), snr=4.0),
        Arrival(when=utcnow(), hops=("a1b2c3", "77aabb"), snr=-2.0),
    ]
    return MessagePathsScreen(
        message, arrivals, matched=True, resolve=lambda h: h, prefix_bytes=1,
        self_name="Homestead", summary="heard twice", source="Alice",
    )


def _remote_cli(cols: int, rows: int) -> Screen:
    return RemoteCliScreen(
        node_label="YUL-Cartierville",
        history=["get name", "get name -> YUL-Cartierville"],
        send=lambda c: None, session=_GallerySession(cols, rows),
    )


def _path_composer(cols: int, rows: int) -> Screen:
    hub = Contact(name="YUL-Cartierville", public_key=_HUB_KEY, key_prefix="3d63c6429436")
    far = Contact(name="Alice", public_key=_FAR_KEY, key_prefix="f2c24f54551e")
    topo = build_topology(
        self_id="aaaaaaaaaaaa" + "0" * 52, contacts=[hub, far],
        trace_paths=[], packet_paths=[], neighbour_links=[],
    )
    return PathComposerScreen(
        device_label="Homestead", device_hash="aaaaaaaaaaaa" + "0" * 52, topology=topo,
        width_bytes=1, hops=["3d63c6429436", "f2c24f54551e"],
    )


class _CourierStubDevice:
    async def get_contacts(self) -> list:
        return []


class _CourierStubChat:
    def __init__(self) -> None:
        self.sent: list = []

    async def send_direct(self, contact, text):  # noqa: ANN001
        return None


class _CourierStubContext:
    """The minimal AppContext surface :class:`CourierOutboxScreen` actually reads."""

    def __init__(self, config_dir: Path) -> None:
        self.courier_store = CourierStore(config_dir / "courier.json")
        self.watch_store = WatchStore(config_dir / "watchtower.json")
        self.chat = _CourierStubChat()
        self.is_connected = True
        self.log = logging.getLogger("test.gallery.courier")
        self._device = _CourierStubDevice()

    async def device(self) -> _CourierStubDevice:
        return self._device


def _courier_outbox(cols: int, rows: int) -> Screen:
    config_dir = Path(tempfile.mkdtemp(prefix="meshterm-gallery-courier-"))
    ctx = _CourierStubContext(config_dir)
    ctx.courier = CourierService(ctx)
    ctx.courier_store.queue("aa" * 6, "YUL-Cartierville", "battery reading requested please")
    ctx.courier_store.queue("bb" * 6, "A Rather Long Contact Name For Width", "hello there")
    return CourierOutboxScreen(ctx)


def _record(**over) -> DiscoveredPath:
    defaults = dict(
        id=1, category="grand_tour", width_bytes=1, spec="3d63c6429436,f2c24f54551e",
        route=("3d63c6429436", "f2c24f54551e"), score=2.0,
        stats={
            "hop_count": 2, "distinct_nodes": 2, "repeats": False, "min_snr": 6.0,
            "km_travelled": 3.2, "km_complete": True, "far_km": 1.5, "rtt_ms": 250.0,
        },
        app_version="0.1.0",
        discovered_at=datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc),
    )
    defaults.update(over)
    return DiscoveredPath(**defaults)


def _record_dialog(cols: int, rows: int) -> Screen:
    record = _record()
    return RecordDialog(
        record, CATEGORY_BY_ID[record.category], 1,
        resolve=lambda h: {"3d63c6429436": "YUL-Cartierville", "f2c24f54551e": "Alice"}.get(h, h),
        device_label="Homestead", device_hash=None,
    )


def _packet_viewer(cols: int, rows: int) -> Screen:
    entry = PacketEntry(
        when=utcnow(), kind="packet", node="3d63c6429436", path="3d63c6429436f2c24f54551e",
        raw={"payload_typename": "GRP_TXT", "route_typename": "FLOOD"},
    )
    return PacketViewer(
        [entry], 0, resolve=lambda h: {"3d63c6429436": "YUL-Cartierville"}.get(h, h),
    )


def _trace(cols: int, rows: int) -> Screen:
    async def _noop_flow(current):  # noqa: ANN001
        return current

    async def _trace_call(path_spec, on_trace):  # noqa: ANN001
        pass  # never invoked — the screen is seeded directly via _on_trace below

    screen = TraceScreen(
        "Alice", mode="target", device_label="Homestead", device_hash="aa" * 32,
        resolve=lambda label: label, session=_GallerySession(cols, rows),
        trace=_trace_call, compose_path=_noop_flow, explore=_noop_flow,
        pick_width=_noop_flow, pick_samples=_noop_flow,
        width_bytes=lambda: 2, sample_count=lambda: 1, pace_s=0.0,
        previous=None, auto_spec=lambda: "", auto_source="",
    )
    screen._on_trace(TraceResult(
        target="Alice", success=True,
        hops=[Hop(0, "3d63c6", 5.0), Hop(1, None, 2.0)],
        round_trip_ms=210.0, path_hash_bytes=2,
    ))
    return screen


def _tx_sweep(cols: int, rows: int) -> Screen:
    screen = TxSweepScreen(
        admin_label="YUL-Cartierville", target_label="Alice", device_label="Homestead",
        device_hash="00" * 32, resolve=lambda h: h, session=_GallerySession(cols, rows),
        tx_min=12, tx_max=28, step=3, samples=3,
        run_sweep=lambda: None, apply_winner=lambda: None,
    )
    screen.on_phase("coarse")
    screen.on_level(1, 6, TxLevelResult(
        tx_power=19, samples=3, successes=3, target_snr=8.8, score=8.8,
        stats=TraceStats.from_traces("Alice", []),
    ))
    return screen


_ENTRIES: list[_Entry] = [
    _Entry("dashboard", _dashboard),
    _Entry("contacts", _contacts, has_exit_group=True),
    _Entry("node_detail", _node_detail),
    _Entry("map", _map),
    _Entry("chat", _chat),
    _Entry("livefeed", _livefeed),
    _Entry("walk", _walk),
    _Entry("timemachine", _timemachine),
    _Entry("message_paths", _message_paths),
    _Entry("remote_cli", _remote_cli),
    _Entry("path_composer", _path_composer),
    _Entry("courier_outbox", _courier_outbox, has_exit_group=True),
    _Entry("record_dialog", _record_dialog),
    _Entry("packet_viewer", _packet_viewer),
    _Entry("trace", _trace),
    _Entry("tx_sweep", _tx_sweep),
]

#: (platform, cols, rows) combos every entry above renders under. PicoCalc gets both its
#: live floor (53x26, the on-device measurement — see the plan's P0 appendix) and the lux
#: case (53x40, today's boot fbcon font / a future 6x8 font). Regular is unchanged by this
#: seam's arrival, so it stays the existing 72x24 standard.
_COMBOS: list[tuple[Platform, int, int]] = [
    (REGULAR, REGULAR.readable_cols, REGULAR.readable_rows),
    (PICOCALC, PICOCALC.readable_cols, PICOCALC.readable_rows),
    (PICOCALC, PICOCALC.readable_cols, 40),
]

#: Entries that overflow PICOCALC's 53 columns today (a width overflow doesn't depend on
#: row count, so one entry here covers both picocalc combos). This is P6's worklist, not a
#: TODO comment — pull an entry out the moment its case reports XPASS instead of xfail.
_KNOWN_WIDE: set[str] = {
    "contacts",
    "map",
    "walk",
    "livefeed",
    "remote_cli",
    "path_composer",
}

#: Entries whose footer_hint already overflowed the *existing* 72-column standard before
#: this platform seam existed. Not this phase's to fix: CLAUDE.md's 72-col rule predates
#: the seam, and per standing guidance old chrome that already broke it is left for a
#: dedicated pass rather than retrofitted as a drive-by here (map's footer grows an
#: "offline — no basemap" suffix that was already too long without it).
_PREEXISTING_REGULAR_OVERFLOW: set[str] = {"map"}


def _cases():
    for entry in _ENTRIES:
        for platform, cols, rows in _COMBOS:
            case_id = f"{entry.name}-{platform.name}-{cols}x{rows}"
            marks = []
            if platform.name == "picocalc" and entry.name in _KNOWN_WIDE:
                marks.append(pytest.mark.xfail(
                    reason=(
                        f"{entry.name} overflows picocalc's {PICOCALC.readable_cols} cols "
                        "today -- P6 worklist (.claude/plans/picocalc-platform.md)"
                    ),
                    strict=False,
                ))
            if platform is REGULAR and entry.name in _PREEXISTING_REGULAR_OVERFLOW:
                marks.append(pytest.mark.xfail(
                    reason=(
                        f"{entry.name} already overflows the existing 72-col standard, "
                        "pre-dating the platform seam -- not retrofitted here, see "
                        "_PREEXISTING_REGULAR_OVERFLOW"
                    ),
                    strict=False,
                ))
            yield pytest.param(entry, platform, cols, rows, id=case_id, marks=marks)


def _assert_fits(lines: list[str], cols: int, where: str) -> None:
    """Assert every line of already-rendered output is within ``cols`` display cells."""
    for i, line in enumerate(lines):
        width = cell_len(_plain(line))
        assert width <= cols, f"{where} line {i} is {width} cells, over {cols} allowed: {line!r}"


@pytest.mark.parametrize("entry,platform,cols,rows", list(_cases()))
def test_gallery_screen_fits_its_platform(
    entry: _Entry, platform: Platform, cols: int, rows: int,
) -> None:
    """Every gallery specimen renders within its platform's width, raw and framed alike."""
    set_platform(platform)
    screen = entry.factory(cols, rows)
    screen.note_viewport(max(1, rows - 4))  # mirrors compose_base's own viewport math

    _assert_fits(screen.render_body(cols), cols, "render_body")
    # footer_hint may itself carry Rich markup (e.g. map's "[warn]offline[/warn]" tag), the
    # same way compose_base renders it (`Text.from_markup(f"[muted]{footer_hint}[/muted]")")
    # — so it must be parsed, not just ANSI-stripped, before measuring.
    footer_plain = Text.from_markup(screen.footer_hint).plain
    assert cell_len(footer_plain) <= cols, (
        f"footer_hint renders to {cell_len(footer_plain)} cells, over {cols}: {footer_plain!r}"
    )

    composed = frame.compose_base(Text(""), screen, screen.footer_hint, cols, rows)
    _assert_fits(composed.split("\n"), cols, "compose_base")

    if entry.has_exit_group:
        lines = _plain(screen.render_body(cols)).splitlines()
        assert lines[-1].strip() == "Back", f"{entry.name}: exit row missing, got {lines[-1]!r}"
        assert lines[-2].strip() == "", f"{entry.name}: no blank separator before Back"

    # P3 assertions, on the *rendered ANSI* (the theme/fold contracts, not the config):
    # picocalc output may carry no truecolor or 256-colour SGR (the console has 16 slots,
    # addressed as plain 30-37/90-97/40-47 codes), and no character outside the 512-glyph
    # console font. Together these are the parity gate that catches a stray emoji or hex
    # colour the moment a screen grows one, instead of as tofu found on-device.
    if platform.name == "picocalc":
        for where, ansi_lines in (
            ("render_body", screen.render_body(cols)),
            ("compose_base", composed.split("\n")),
        ):
            for i, line in enumerate(ansi_lines):
                assert "[38;2;" not in line and "[48;2;" not in line, (
                    f"{entry.name} {where} line {i} emits truecolor SGR: {line!r}"
                )
                assert "[38;5;" not in line and "[48;5;" not in line, (
                    f"{entry.name} {where} line {i} emits 256-colour SGR: {line!r}"
                )
                strays = {
                    ch for ch in line
                    if ord(ch) >= 0x20 and ord(ch) not in FONT_CODEPOINTS
                }
                assert not strays, (
                    f"{entry.name} {where} line {i} has characters outside the console "
                    f"font: {sorted(strays)!r} in {line!r}"
                )
