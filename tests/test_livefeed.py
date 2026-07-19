"""Live feed tests: the promoted feed screen's state, rows, and windowing.

The screen is driven headless against fake sessions and canned observations — the
same approach as the dashboard tests, whose feed panel this screen grew out of.
"""

from __future__ import annotations

import re
from datetime import timedelta

from meshterm.core.events import MeshEvent
from meshterm.core.models import Ack, Message, Observation, utcnow
from meshterm.ui.livefeed_screen import LiveFeedScreen


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


def test_livefeed_packet_rows_show_their_relay_path() -> None:
    """An RX-log packet reads as its relay path, resolved to names where known.

    The path renders through the shared compact path widget, so each name carries its
    own hue — assert on the stripped text, not the raw ANSI.
    """
    screen = _screen()
    screen.on_event(
        MeshEvent.observation_event(_obs(kind="packet", path="3d63,a1b2", snr=1.0))
    )
    assert "via YUL → Alice" in "\n".join(_stripped(screen.render_body(100)))


def test_livefeed_names_a_relayed_packet_by_its_payload_class() -> None:
    """A relayed packet naming no origin reads as its payload class, never a bare '?'."""
    screen = _screen()
    raw = {"payload_typename": "TRACE", "route_typename": "FLOOD"}
    screen.on_event(
        MeshEvent.observation_event(
            _obs(node="", kind="packet", snr=1.0, path="3d63,a1b2", raw=raw)
        )
    )
    body = _plain(screen.render_body(100))
    assert "trace" in body  # the payload-class gloss stands in for the missing identity
    assert "?" not in body  # …instead of the useless placeholder


def test_livefeed_names_a_channel_message_by_its_sender() -> None:
    """A channel message's ``Name:`` prefix names the node lane, not the bare channel."""
    screen = _screen()
    screen.on_event(
        MeshEvent.message_event(Message(text="Alice: hi all", channel=3, is_channel=True))
    )
    body = _plain(screen.render_body(100))
    assert "Alice" in body          # the parsed sender leads the row
    assert "ch 3" in body           # …with the channel kept as the trailing context


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


def test_livefeed_without_a_device_reads_as_waiting() -> None:
    """With the hub idle the heading says so instead of pretending to be live."""
    screen = _screen(active=False)
    assert "○ waiting for a device" in _plain(screen.render_body(100))
