"""Live feed tests: the promoted feed screen's state, rows, and windowing.

The screen is driven headless against fake sessions and canned observations — the
same approach as the dashboard tests, whose feed panel this screen grew out of.
"""

from __future__ import annotations

import re
from datetime import timedelta

from rich.cells import cell_len

from meshterm.core.events import MeshEvent
from meshterm.core.models import Ack, Message, Observation, utcnow
from meshterm.ui.livefeed_screen import _ICON_LANE, LiveFeedScreen


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _screen(seed=None, active=True) -> LiveFeedScreen:
    screen = LiveFeedScreen(
        session=_FakeSession(),
        resolve=lambda h: {"a1b2": "Alice", "3d63": "YUL"}.get(h, ""),
        seed=list(seed or []),
        hub_active=lambda: active,
    )
    screen.note_viewport(48)  # the frame records this before every real paint
    return screen


def _obs(node="a1b2", kind="advert", snr=5.0, rssi=-90.0, age_s=0, **extra) -> Observation:
    return Observation(
        node=node, name=node, kind=kind, snr=snr, rssi=rssi,
        observed_at=utcnow() - timedelta(seconds=age_s), **extra,
    )


def _plain(lines: list[str]) -> str:
    return "\n".join(lines)


def _stripped(lines: list[str]) -> list[str]:
    """The rendered lines with ANSI escapes removed, for structural assertions."""
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _rows(screen: LiveFeedScreen, width: int) -> list[str]:
    """Just the packet rows — the body past its status line and its column header."""
    return _stripped(screen.render_body(width))[2:]


def _col(line: str, needle: str) -> int:
    """The display column (cells, not characters) ``needle`` starts at within ``line``."""
    return cell_len(line[: line.index(needle)])


def test_livefeed_tool_registers_under_the_dashboard() -> None:
    """The livefeed tool lands in the Mesh section, right below the dashboard."""
    from meshterm.tools import load_all_tools
    from meshterm.tools.base import all_tools

    load_all_tools()
    tools = {t.name: t for t in all_tools()}
    tool = tools["livefeed"]
    assert tool.title == "Live feed" and tool.category == "Mesh"
    assert tools["dashboard"].order < tool.order < tools["contacts"].order


def test_livefeed_live_events_land_in_the_feed() -> None:
    """Observations, messages, and acks each fold into the feed as they arrive."""
    screen = _screen()
    screen.on_event(MeshEvent.observation_event(_obs(snr=7.5)))
    screen.on_event(MeshEvent.message_event(Message(text="hi", sender="a1b2")))
    screen.on_event(MeshEvent.ack_event(Ack(code="01c3")))
    body = _plain(screen.render_body(100))
    assert "+7.5 dB" in body
    assert "message" in body and "ack" in body
    # Newest first: the ack row sits above the message row, which sits above the advert.
    assert body.index("ack") < body.index("message") < body.index("advert")


def test_livefeed_rows_leave_the_relay_path_to_the_viewer() -> None:
    """A row says what arrived and how well it was heard; the route is Enter's job.

    Whatever the fixed lanes left a route was never enough to draw one in — it arrived
    elided to a stub — so the feed carries none, and the whole row fits a 72-column
    screen with its class label intact.
    """
    screen = _screen()
    screen.on_event(
        MeshEvent.observation_event(_obs(kind="packet", path="3d63,a1b2", snr=1.0))
    )
    row = _rows(screen, 72)[0]
    assert "via" not in row and "YUL" not in row  # no route, not even a stub of one
    assert "📦 packet" in row                      # the class still reads in words at 72
    assert "+1.0 dB" in row and "-90 dBm" in row   # …as does the reception it was heard at
    assert len(row.rstrip()) <= 72


def test_livefeed_class_lane_names_the_payload_class_once() -> None:
    """A raw frame is filed under what it *is*, and the node lane doesn't repeat it.

    ``packet`` names only the event family the frame arrived in, so the class lane
    reads its parsed payload class — the very label the viewer's card headlines. The
    node lane is then free to be about the node, and an origin-less flood says so with
    a dash rather than standing the class in a second time.
    """
    screen = _screen()
    raw = {"payload_typename": "TRACE", "route_typename": "FLOOD"}
    screen.on_event(
        MeshEvent.observation_event(
            _obs(node="", kind="packet", snr=1.0, path="3d63,a1b2", raw=raw)
        )
    )
    row = _rows(screen, 100)[0]
    assert "🎯 trace" in row       # the class lane, under the payload class's own icon
    assert row.count("trace") == 1  # said once, not once per lane
    assert "packet" not in row      # …and never as the generic event family
    assert "—" in row               # the node lane: nobody identified themselves
    assert "?" not in row           # …but never the useless placeholder


def test_livefeed_names_a_channel_message_by_its_sender() -> None:
    """A channel message's ``Name:`` prefix names the node lane, not the bare channel."""
    screen = _screen()
    screen.on_event(
        MeshEvent.message_event(Message(text="Alice: hi all", channel=3, is_channel=True))
    )
    body = _plain(screen.render_body(100))
    assert "Alice" in body          # the parsed sender leads the row
    assert "ch 3" in body           # …with the channel kept as the trailing context


def test_livefeed_column_header_sits_over_the_lanes_it_names() -> None:
    """The header names each lane, and every label lands on the values beneath it."""
    screen = _screen(seed=[_obs(node="3d63", kind="telemetry", snr=12.8, rssi=-105.0)])
    header, row = _stripped(screen.render_body(72))[1:3]
    assert header.split() == ["TIME", "CLASS", "NODE", "SNR", "RSSI"]
    # Measured in display cells, not characters — the class icon is one character wide
    # but two cells, so a character index would report every later lane one column early.
    assert _col(header, "TIME") == _col(row, "03:")
    assert _col(header, "CLASS") == _col(row, "telemetry") - _ICON_LANE
    assert _col(header, "NODE") == _col(row, "YUL")
    # The two readings right-align their number, so their labels end where the digits do.
    assert _col(header, "SNR") + 3 == _col(row, "+12.8") + 5
    assert _col(header, "RSSI") + 4 == _col(row, "-105") + 4


def test_livefeed_column_header_is_pinned_and_carries_no_sort_cue() -> None:
    """The header never scrolls away, and it advertises no sort — the feed has one order."""
    screen = _screen(seed=[_obs(node=f"n{i}", age_s=i) for i in range(40)])
    screen.note_viewport(12)
    lines = _stripped(screen.render_body(100))
    assert "TIME" in lines[1]
    assert not any(mark in lines[1] for mark in ("▲", "▼"))  # nothing here sorts
    screen.handle("end")  # walk to the oldest packet — the window scrolls under the header
    after = _stripped(screen.render_body(100))
    assert after[1] == lines[1]  # …and the header is exactly where it was
    assert "↑" in after[2] and "more" in after[2]  # the rows really did travel


def test_livefeed_highlight_uses_the_app_wide_cursor() -> None:
    """The feed marks its row like every other list: a ``❯`` pointer over a brand row."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    rows = _rows(screen, 100)
    assert rows[0].startswith("❯ ") and rows[1].startswith("  ")
    assert "▸" not in "\n".join(rows)  # the old odd-one-out mark is gone
    raw = screen.render_body(100)[2]
    assert raw.startswith("\x1b[")  # the pointer carries the brand style, not bare text


def test_livefeed_page_keys_move_the_feed_selection() -> None:
    """With a feed row highlighted, PgUp/PgDn walk the selection a windowful at a time."""
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(20)]
    screen = _screen(seed=seed)
    screen._feed_window.page = 5  # as if the last paint settled a five-row window
    assert screen._selected == 0  # the newest packet is highlighted from the start
    screen.handle("pagedown")
    assert screen._selected == 5  # the selection travelled down a page, not just the view
    screen.handle("pageup")
    assert screen._selected == 0

    # With nothing highlighted, the page keys slide the feed window instead.
    screen._selected = None
    screen.handle("pagedown")
    assert screen._feed_window.top > 0 and screen._selected is None


def test_livefeed_windows_inside_the_fixed_screen() -> None:
    """The heading stays pinned: the body fits the viewport and the feed rows window."""
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(40)]
    screen = _screen(seed=seed)
    screen.note_viewport(24)
    lines = screen.render_body(100)
    assert len(lines) <= 24  # heading + feed window == the viewport, never more
    body = _plain(_stripped(lines))
    assert "newest first" in body  # the pinned status line is still there
    assert "↓" in body and "more" in body  # hidden feed rows are counted below


#: A terminal too narrow to hold the feed's fixed lanes — the only thing that makes a
#: row overflow now that no route rides in it (see ``_FEED_LABEL_MIN_WIDTH``).
_CRAMPED = 44


def test_livefeed_the_whole_row_fits_a_72_column_screen() -> None:
    """No row overflows at 72 — which is why ←→ has nothing to advertise there."""
    screen = _screen(seed=[_obs(node="n0", kind="telemetry")])
    for line in _stripped(screen.render_body(72))[1:]:
        assert len(line.rstrip()) <= 72
    assert screen._hmax == 0
    assert "←→" not in screen.footer_hint


def test_livefeed_highlighted_row_scrolls_sideways_to_its_tail() -> None:
    """On a terminal too narrow for the lanes, ←→ slide the highlighted row to its end."""
    screen = _screen(seed=[_obs()])
    opening = _rows(screen, _CRAMPED)[0]
    assert screen._hmax > 0  # the row does run past the right edge
    assert not opening.rstrip().endswith("dBm")  # …so its reception tail is off-screen

    for _ in range(40):  # → saturates at the row's own end, never past it
        screen.handle("right")
    scrolled = _rows(screen, _CRAMPED)[0]
    assert screen._hshift == screen._hmax
    assert scrolled.startswith("❯ ")  # the pointer lane stays pinned while the row slides
    assert scrolled.rstrip().endswith("-90 dBm")  # the tail is now readable
    assert opening[2:10] not in scrolled  # …at the cost of the time lane, slid off left

    for _ in range(40):
        screen.handle("left")
    assert screen._hshift == 0
    assert _rows(screen, _CRAMPED)[0] == opening  # back where it started


def test_livefeed_moving_the_selection_abandons_the_rows_scroll() -> None:
    """Each row scrolls on its own: landing on another packet starts it at its beginning."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    screen.render_body(_CRAMPED)
    screen.handle("right")
    assert screen._hshift > 0
    screen.handle("down")
    assert screen._hshift == 0


def test_livefeed_advertises_line_scroll_only_where_it_acts() -> None:
    """←→ earns its footer atom exactly while the highlighted row overflows."""
    screen = _screen(seed=[_obs()])
    screen.render_body(100)
    assert "←→" not in screen.footer_hint  # a row with room to spare scrolls nowhere

    screen.render_body(_CRAMPED)
    assert "←→ scroll line" in screen.footer_hint
    assert len(screen.footer_hint) <= 72  # the screens-at-72 rule, fullest state


def test_livefeed_without_a_device_reads_as_waiting() -> None:
    """With the hub idle the heading says so instead of pretending to be live."""
    screen = _screen(active=False)
    assert "○ waiting for a device" in _plain(screen.render_body(100))
