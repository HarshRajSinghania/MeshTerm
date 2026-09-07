"""Tests for the background advert.

The store's schedule arithmetic, the scheduler's one-send-per-pass discipline, and
the editor/executor integration that lets manual adverts and staged cadence changes
share one clock.
"""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.advert_store import (
    DEFAULT_DIRECT_HOURS,
    DEFAULT_FLOOD_HOURS,
    OFF,
    AdvertStore,
    cadence_label,
)
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.advert_scheduler import AdvertScheduler
from meshterm.tools.config import apply_ops

KEY = "00" * 32  # the simulator's public key


# -- the store -----------------------------------------------------------------------


def test_store_defaults_are_on(tmp_path: Path) -> None:
    """An unknown device gets the on-by-default policy: hourly direct, daily flood."""
    policy = AdvertStore(tmp_path / "adverts.json").load(KEY)
    assert policy.direct_hours == DEFAULT_DIRECT_HOURS
    assert policy.flood_hours == DEFAULT_FLOOD_HOURS
    assert policy.last_direct is None and policy.last_flood is None


def test_store_round_trips_cadence_and_marks(tmp_path: Path) -> None:
    """Cadences and last-sent marks persist per device and per advert type."""
    store = AdvertStore(tmp_path / "adverts.json")
    store.set_cadence(KEY, flood=True, hours=48)
    store.set_cadence(KEY, flood=False, hours=OFF)
    when = utcnow()
    store.mark_sent(KEY, flood=True, when=when)

    policy = store.load(KEY)
    assert policy.flood_hours == 48
    assert policy.direct_hours == OFF
    assert policy.last_flood == when
    assert policy.last_direct is None
    # A different device is untouched.
    assert AdvertStore(tmp_path / "adverts.json").load("ff" * 32).flood_hours == 24


def test_never_sent_is_not_due_and_arm_starts_the_clock(tmp_path: Path) -> None:
    """A fresh device is silent until armed; arming never overwrites a real mark."""
    store = AdvertStore(tmp_path / "adverts.json")
    assert not store.load(KEY).due(flood=False)

    armed_at = utcnow() - timedelta(hours=2)
    store.arm(KEY, flood=False, when=armed_at)
    assert store.load(KEY).due(flood=False)  # armed 2 h ago, hourly cadence — due

    store.arm(KEY, flood=False)  # already armed: a no-op, not a reset to "now"
    assert store.load(KEY).last_direct == armed_at


def test_due_respects_off_and_cadence(tmp_path: Path) -> None:
    """The clock honours OFF and only fires once the full cadence has elapsed."""
    store = AdvertStore(tmp_path / "adverts.json")
    store.mark_sent(KEY, flood=True, when=utcnow() - timedelta(hours=25))
    assert store.load(KEY).due(flood=True)  # daily default, 25 h elapsed

    store.mark_sent(KEY, flood=True, when=utcnow() - timedelta(hours=23))
    assert not store.load(KEY).due(flood=True)

    store.set_cadence(KEY, flood=True, hours=OFF)
    assert not store.load(KEY).due(flood=True)  # overdue but switched off


def test_store_tolerates_corrupt_file(tmp_path: Path) -> None:
    """A mangled state file reads as the defaults instead of raising."""
    path = tmp_path / "adverts.json"
    path.write_text("{not json", encoding="utf-8")
    assert AdvertStore(path).load(KEY).direct_hours == DEFAULT_DIRECT_HOURS


def test_cadence_labels() -> None:
    """The display labels read naturally at every offered cadence."""
    assert cadence_label(OFF) == "off"
    assert cadence_label(1) == "every hour"
    assert cadence_label(3) == "every 3 h"
    assert cadence_label(24) == "daily"
    assert cadence_label(168) == "weekly"


# -- the scheduler and executor against the simulator --------------------------------


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context for scheduler/executor tests."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "adv.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


class _NoteUi:
    """A minimal UI surface that only collects notes (all apply_ops needs)."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def note(self, markup: str) -> None:
        self.notes.append(markup)


async def test_scheduler_first_pass_arms_without_sending(ctx: AppContext) -> None:
    """The first look at a fresh device starts the countdown; nothing is transmitted."""
    await ctx.device()
    scheduler = AdvertScheduler(ctx)
    sent: list[bool] = []
    (await ctx.device()).send_advert = lambda flood=False: _record(sent, flood)  # type: ignore[method-assign]

    await scheduler._pass()
    policy = ctx.advert_store.load(KEY)
    assert sent == []
    assert policy.last_direct is not None and policy.last_flood is not None


async def test_scheduler_sends_at_most_one_advert_per_pass(ctx: AppContext) -> None:
    """With both types overdue, one pass sends only the direct; the next sends the flood."""
    await ctx.device()
    store = ctx.advert_store
    store.mark_sent(KEY, flood=False, when=utcnow() - timedelta(hours=2))
    store.mark_sent(KEY, flood=True, when=utcnow() - timedelta(hours=48))

    scheduler = AdvertScheduler(ctx)
    sent: list[bool] = []
    (await ctx.device()).send_advert = lambda flood=False: _record(sent, flood)  # type: ignore[method-assign]

    await scheduler._pass()
    assert sent == [False]  # direct first, flood held for the next pass
    assert not store.load(KEY).due(flood=False)
    assert store.load(KEY).due(flood=True)

    await scheduler._pass()
    assert sent == [False, True]
    assert not store.load(KEY).due(flood=True)


async def test_scheduler_skips_quietly_when_disconnected(ctx: AppContext) -> None:
    """A pass with no device connected does nothing (and records nothing)."""
    scheduler = AdvertScheduler(ctx)
    await scheduler._pass()
    assert ctx.advert_store.load(KEY).last_direct is None


async def test_manual_advert_resets_the_background_clock(ctx: AppContext) -> None:
    """An advert sent through apply_ops (any manual flow) re-marks its type's clock."""
    device = await ctx.device()
    ctx.advert_store.mark_sent(KEY, flood=False, when=utcnow() - timedelta(hours=5))
    assert ctx.advert_store.load(KEY).due(flood=False)

    ctx._ui = _NoteUi()  # type: ignore[assignment]
    snapshot = dict(await device.get_self_info())
    await apply_ops(ctx, device, snapshot, [("advert", False)])
    assert not ctx.advert_store.load(KEY).due(flood=False)


async def test_advert_cadence_op_writes_the_store(ctx: AppContext) -> None:
    """The editor's staged cadence change lands in the store via its op."""
    device = await ctx.device()
    ctx._ui = _NoteUi()  # type: ignore[assignment]
    snapshot = dict(await device.get_self_info())
    changes, _ = await apply_ops(
        ctx, device, snapshot, [("advert_cadence", True, 168), ("advert_cadence", False, OFF)]
    )
    assert changes == 2
    policy = ctx.advert_store.load(KEY)
    assert policy.flood_hours == 168
    assert policy.direct_hours == OFF


async def _record(sent: list[bool], flood: bool) -> None:
    """Stand-in ``send_advert`` recording each transmission's type."""
    sent.append(flood)
