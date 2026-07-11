"""Watchtower tests: the store's persistence and the sentinel's rules.

The service is driven synchronously through :meth:`note`/:meth:`evaluate` against a
stub context (the monitor tests' approach), so no timers or hardware are involved.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from meshterm.core.models import Observation, utcnow
from meshterm.core.watch_store import ALERT_CAP, OFF, WatchStore
from meshterm.persistence.repository import Repository
from meshterm.services.watchtower import SNR_WINDOW, WatchtowerService

NODE = "3d" * 6


class _StubContext:
    """Minimal stand-in for :class:`~meshterm.context.AppContext` for rule tests."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = Repository(tmp_path / "wt.db")
        self.watch_store = WatchStore(tmp_path / "watchtower.json")
        self.log = logging.getLogger("test.watchtower")


def _service(tmp_path: Path) -> WatchtowerService:
    ctx = _StubContext(tmp_path)
    service = WatchtowerService(ctx)
    service._known = set()  # what start() seeds from the DB; empty history here
    return service


def _obs(node=NODE, name="YUL", snr=None, age_s=0, kind="advert") -> Observation:
    return Observation(
        node=node, name=name, kind=kind, snr=snr,
        observed_at=utcnow() - timedelta(seconds=age_s),
    )


# --- the store --------------------------------------------------------------------------


def test_store_watch_round_trips_and_persists(tmp_path: Path) -> None:
    """Watched nodes and their rules survive a fresh store instance."""
    path = tmp_path / "watchtower.json"
    store = WatchStore(path)
    seen = utcnow() - timedelta(hours=2)
    store.watch(NODE, "YUL", last_seen=seen)
    store.set_silence(NODE, 24)
    store.set_snr_watch(NODE, False)

    again = WatchStore(path)
    entry = again.watched()[NODE]
    assert entry.name == "YUL" and entry.silence_hours == 24 and not entry.snr_watch
    assert entry.last_heard == seen
    again.unwatch(NODE)
    assert not WatchStore(path).watched()


def test_store_alert_log_caps_acks_and_clears(tmp_path: Path) -> None:
    """Alerts append newest-first, cap, acknowledge, and clear."""
    store = WatchStore(tmp_path / "watchtower.json")
    first = store.add_alert("silence", "YUL", "quiet")
    store.add_alert("snr", "YUL", "sagging")
    assert [a.kind for a in store.alerts()] == ["snr", "silence"]
    assert store.unacked_count() == 2

    store.ack(first.ident)
    assert store.unacked_count() == 1
    store.ack_all()
    assert store.unacked_count() == 0
    store.clear_acked()
    assert store.alerts() == []

    for i in range(ALERT_CAP + 10):
        store.add_alert("new-node", f"n{i}", "hi")
    assert len(store.alerts()) == ALERT_CAP
    assert store.alerts()[0].label == f"n{ALERT_CAP + 9}"  # newest survives the cap


def test_store_note_heard_updates_and_refreshes_name(tmp_path: Path) -> None:
    """Hearing a watched node advances its mark and adopts a fresh name."""
    store = WatchStore(tmp_path / "watchtower.json")
    store.watch(NODE, NODE)
    later = utcnow() + timedelta(minutes=5)
    store.note_heard(NODE, when=later, name="YUL-Cartierville")
    entry = store.watched()[NODE]
    assert entry.last_heard == later and entry.name == "YUL-Cartierville"


# --- the silence rule ---------------------------------------------------------------------


def test_silence_fires_once_then_rearms_on_recovery(tmp_path: Path) -> None:
    """Quiet past the threshold alarms once; hearing again notes the recovery."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "YUL", last_seen=utcnow() - timedelta(hours=13))

    service.evaluate()  # default threshold is 12 h — already past it
    service.evaluate()  # latched: no duplicate
    alerts = store.alerts()
    assert [a.kind for a in alerts] == ["silence"]
    assert "12 h" in alerts[0].message

    service.note(_obs())  # the node returns
    alerts = store.alerts()
    assert [a.kind for a in alerts] == ["recovered", "silence"]
    assert store.watched()[NODE].silent_since is None  # re-armed

    service.evaluate()  # freshly heard: quiet again only after another 12 h
    assert len(store.alerts()) == 2


def test_silence_respects_off_and_unheard(tmp_path: Path) -> None:
    """An OFF rule never alarms, however stale the mark."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "YUL", last_seen=utcnow() - timedelta(days=30))
    store.set_silence(NODE, OFF)
    service.evaluate()
    assert store.alerts() == []


# --- the SNR rule ---------------------------------------------------------------------


def test_snr_sag_alerts_with_cooldown(tmp_path: Path) -> None:
    """A clear drop in median SNR alerts once, not on every subsequent packet."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "YUL")

    for _ in range(SNR_WINDOW):
        service.note(_obs(snr=8.0))
    for _ in range(SNR_WINDOW):
        service.note(_obs(snr=-2.0))
    sags = [a for a in store.alerts() if a.kind == "snr"]
    assert len(sags) == 1
    assert "+8.0" in sags[0].message and "-2.0" in sags[0].message

    service.note(_obs(snr=-2.0))  # still sagging, but inside the cooldown
    assert len([a for a in store.alerts() if a.kind == "snr"]) == 1


def test_snr_ignores_packet_rows_and_disabled_watch(tmp_path: Path) -> None:
    """Relay-measured packet SNR and an off switch both keep the rule quiet."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.watch(NODE, "YUL")
    store.set_snr_watch(NODE, False)
    for snr in (9.0,) * SNR_WINDOW + (-9.0,) * SNR_WINDOW:
        service.note(_obs(snr=snr))
    assert [a for a in store.alerts() if a.kind == "snr"] == []

    store.set_snr_watch(NODE, True)
    for snr in (9.0,) * SNR_WINDOW + (-9.0,) * SNR_WINDOW:
        service.note(_obs(snr=snr, kind="packet"))
    assert [a for a in store.alerts() if a.kind == "snr"] == []


# --- the new-node rule ------------------------------------------------------------------


def test_new_node_announced_once_ever(tmp_path: Path) -> None:
    """A first-ever id alerts once; repeats and restarts stay quiet."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    service.note(_obs(node="f7" * 6, name="Newcomer"))
    service.note(_obs(node="f7" * 6, name="Newcomer"))
    news = [a for a in store.alerts() if a.kind == "new-node"]
    assert len(news) == 1 and news[0].label == "Newcomer"
    assert store.known_contains("f7" * 6)  # persisted: restarts stay quiet too


def test_new_node_rule_can_be_switched_off(tmp_path: Path) -> None:
    """With the toggle off, first sightings are remembered but never announced."""
    service = _service(tmp_path)
    store = service._ctx.watch_store
    store.set_new_node_alerts(False)
    service.note(_obs(node="c9" * 6))
    assert store.alerts() == []
    assert store.known_contains("c9" * 6)
