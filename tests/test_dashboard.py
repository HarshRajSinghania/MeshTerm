"""Dashboard tests: the braille bar chart, the live screen's state, and its data feeds.

The screen is driven headless against fake sessions and canned observations, the same
approach as the live trace/TX screen tests.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from meshterm.core.events import MeshEvent
from meshterm.core.models import Ack, Message, Observation, utcnow
from meshterm.persistence.repository import ACTIVITY_BUCKETS, Repository
from meshterm.ui.dashboard_screen import DashboardScreen, braille_bars


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _screen(window=None, histogram=None, kinds=None, active=True) -> DashboardScreen:
    return DashboardScreen(
        session=_FakeSession(),
        resolve=lambda h: {"a1b2": "Alice", "3d63": "YUL"}.get(h, ""),
        window=list(window or []),
        activity=lambda: tuple(histogram or (0,) * ACTIVITY_BUCKETS),
        kind_counts=lambda: dict(kinds or {}),
        hub_active=lambda: active,
    )


def _obs(node="a1b2", kind="advert", snr=5.0, rssi=-90.0, age_s=0, **extra) -> Observation:
    return Observation(
        node=node, name=node, kind=kind, snr=snr, rssi=rssi,
        observed_at=utcnow() - timedelta(seconds=age_s), **extra,
    )


def _plain(lines: list[str]) -> str:
    return "\n".join(lines)


# --- braille_bars --------------------------------------------------------------------


def test_braille_bars_scales_the_peak_to_full_height() -> None:
    """The window's peak fills the whole chart; a zero column draws the flatline."""
    rows = braille_bars([0, 6, 12], rows=3)
    assert len(rows) == 3
    assert all(len(r.plain) == 3 for r in rows)
    # The peak column is fully lit on the top row; the zero column is blank there.
    assert rows[0].plain[2] == chr(0x2800 | 0x47 | 0xB8)
    assert rows[0].plain[0] == chr(0x2800)
    # The zero column keeps the faint two-dot floor on the bottom row.
    assert rows[2].plain[0] == chr(0x2800 | 0x40 | 0x80)


def test_braille_bars_never_hides_a_lone_packet() -> None:
    """A tiny non-zero value still lights (in the lit style, not the faint floor's)."""
    rows = braille_bars([1, 1000], rows=3)
    first_span = rows[2].spans[0]
    assert first_span.start == 0 and first_span.style == "ok"


def test_braille_bars_all_silent_is_a_flatline() -> None:
    """With nothing heard the chart is a floor line, not a divide-by-zero."""
    rows = braille_bars([0, 0, 0], rows=2)
    assert rows[1].plain == chr(0x2800 | 0x40 | 0x80) * 3


# --- the screen ----------------------------------------------------------------------


def test_dashboard_renders_all_four_sections() -> None:
    """Activity, Traffic, RF health, and Feed all render from a seeded window."""
    screen = _screen(
        window=[_obs(), _obs(node="3d63", node_type=2, snr=-2.0)],
        histogram=[3] + [0] * (ACTIVITY_BUCKETS - 1),
        kinds={"advert": 5, "ack": 1},
    )
    body = _plain(screen.render_body(100))
    assert "Activity" in body and "Traffic" in body
    assert "RF health" in body and "Feed" in body
    assert "● live" in body
    assert "nodes heard" in body and "(1 repeater)" in body
    assert "advert" in body and "Alice" in body and "YUL" in body


def test_dashboard_live_events_land_in_the_feed_and_window() -> None:
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


def test_dashboard_packet_rows_show_their_relay_path() -> None:
    """An RX-log packet reads as its relay path, resolved to names where known."""
    screen = _screen()
    screen.on_event(
        MeshEvent.observation_event(_obs(kind="packet", path="3d63,a1b2", snr=1.0))
    )
    assert "via YUL → Alice" in _plain(screen.render_body(100))


def test_dashboard_prunes_the_window_but_keeps_the_feed() -> None:
    """Observations older than the window drop out of the statistics."""
    stale = _obs(age_s=3 * 3600, snr=-12.0)
    screen = _screen(window=[stale])
    body = _plain(screen.render_body(100))
    assert "no receptions in the window yet" in body  # stats pruned the stale row


def test_dashboard_without_a_device_reads_as_waiting() -> None:
    """With the hub idle the feed heading says so instead of pretending to be live."""
    screen = _screen(active=False)
    assert "○ waiting for a device" in _plain(screen.render_body(100))


def test_dashboard_radio_rows_render_device_stats() -> None:
    """The Device-info dynamic numbers (noise floor, airtime, battery) fold in."""
    screen = _screen(window=[_obs()])
    screen.stats = {
        "noise_floor": -104, "last_rssi": -95, "last_snr": 5.5,
        "tx_air_secs": 12, "rx_air_secs": 340,
    }
    screen.battery = {"level": 4010}
    body = _plain(screen.render_body(100))
    assert "-104 dBm noise floor" in body
    assert "TX 12 s · RX 340 s" in body
    assert "4.01 V" in body


# --- the repository window -----------------------------------------------------------


def test_recent_observations_windows_and_orders(tmp_path: Path) -> None:
    """The seed query returns only the window, oldest first, hydrated with kinds."""
    repo = Repository(tmp_path / "dash.db")
    run = repo.start_run("monitor", {}, None)
    repo.record_observation(run, _obs(node="old", age_s=3 * 3600))
    repo.record_observation(run, _obs(node="mid", kind="packet", age_s=600, path="3d63"))
    repo.record_observation(run, _obs(node="new", age_s=5))

    window = repo.recent_observations(since=utcnow() - timedelta(hours=2))
    assert [o.node for o in window] == ["mid", "new"]
    assert window[0].kind == "packet" and window[0].path == "3d63"
    repo.close()
