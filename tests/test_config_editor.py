"""Tests for the overhauled config editor: dialogs, staging, and the location picker.

The pure pieces (the contact share URL, coordinate parsing, the typed-confirmation
dialog, the map picker screen) are exercised directly; the editor's flows run end-to-end
against the :class:`MockDevice` simulator through a scripted fake UI surface that answers
each prompt from a queue.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.persistence.repository import Repository
from meshterm.ui.config_editor import (
    _parse_coords,
    _valid_coords,
    contact_share_url,
    device_actions,
    edit_config,
    send_advert,
)
from meshterm.ui.tui.prompt import TypedConfirmDialog
from meshterm.ui.tui.screen import CANCEL

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(lines: list[str]) -> str:
    """Strip ANSI escapes and join rendered lines for content assertions."""
    return _ANSI.sub("", "\n".join(lines))


class _Fut:
    """A minimal future stand-in so screens can resolve without an event loop."""

    def __init__(self) -> None:
        self.value: Any = None
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value: Any) -> None:
        self.value = value
        self._done = True


# -- contact share URL ----------------------------------------------------------


def test_contact_share_url_matches_meshcore_format() -> None:
    """The contact card URL carries the encoded name, hex key, and node type."""
    url = contact_share_url("Base Camp", "AB" * 32, node_type=2)
    assert url.startswith("meshcore://contact/add?")
    assert "name=Base%20Camp" in url
    assert f"public_key={'ab' * 32}" in url  # key is normalized to lowercase
    assert url.endswith("&type=2")


def test_contact_share_url_defaults_to_companion_type() -> None:
    """With no explicit type the card marks the node as a companion (type 1)."""
    assert contact_share_url("n", "00" * 32).endswith("&type=1")


# -- coordinate parsing ----------------------------------------------------------


def test_parse_coords_accepts_comma_and_space_pairs() -> None:
    """A typed pair parses with a comma, a space, or both."""
    assert _parse_coords("45.5, -73.6") == (45.5, -73.6)
    assert _parse_coords("45.5 -73.6") == (45.5, -73.6)
    assert _parse_coords("  45.5 ,  -73.6 ") == (45.5, -73.6)


def test_parse_coords_rejects_garbage_and_out_of_range() -> None:
    """Wrong shape or out-of-range degrees surface as validation messages."""
    assert _valid_coords("45.5") != True  # noqa: E712 - message, not False
    assert _valid_coords("45.5, -73.6, 7") != True  # noqa: E712
    assert _valid_coords("91.0, 0") != True  # noqa: E712 - latitude out of range
    assert _valid_coords("0, 181") != True  # noqa: E712 - longitude out of range
    assert _valid_coords("here, there") != True  # noqa: E712
    assert _valid_coords("45.5, -73.6") is True


# -- the typed-confirmation dialog ------------------------------------------------


def test_typed_confirm_requires_the_exact_word() -> None:
    """Enter only commits once the confirmation word has been typed."""
    dialog = TypedConfirmDialog("This erases everything.", "RESET")
    dialog.future = _Fut()

    dialog.handle("enter")  # empty field: nudge, no resolve
    assert not dialog.future.done()
    assert "doesn't match" in _plain(dialog.render_body(60))

    for ch in "nope":
        dialog.handle("text", ch)
    dialog.handle("enter")
    assert not dialog.future.done()

    for _ in "nope":
        dialog.handle("backspace")
    for ch in "RESET":
        dialog.handle("text", ch)
    dialog.handle("enter")
    assert dialog.future.done() and dialog.future.value is True


def test_typed_confirm_is_case_insensitive_and_cancellable() -> None:
    """A lowercase match still confirms (the friction is the word); Esc cancels."""
    dialog = TypedConfirmDialog("Danger.", "IMPORT")
    dialog.future = _Fut()
    for ch in "import":
        dialog.handle("text", ch)
    dialog.handle("enter")
    assert dialog.future.value is True

    cancelled = TypedConfirmDialog("Danger.", "IMPORT")
    cancelled.future = _Fut()
    cancelled.handle("escape")
    assert cancelled.future.value is CANCEL


def test_typed_confirm_renders_warning_and_instruction() -> None:
    """The dialog shows the consequence and names the word to type."""
    dialog = TypedConfirmDialog("This erases ALL data.", "RESET", title="⚠ Factory reset")
    body = _plain(dialog.render_body(60))
    assert "This erases ALL data." in body
    assert "Type RESET to confirm" in body
    assert dialog.border_style == "err"


# -- the map location picker -------------------------------------------------------


class _StubSession:
    """Minimal stand-in for :class:`TuiSession` for driving the picker screen."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self._cols, self._rows = cols, rows

    def base_body_size(self) -> tuple[int, int]:
        return self._cols, self._rows

    def invalidate(self) -> None:
        pass


class _StubSource:
    """An always-offline tile source, so the picker renders nodes only (no network)."""

    available = False
    max_zoom = 14

    def load_tile(self, z: int, x: int, y: int):
        return None


def _picker(markers=None, initial=None):
    from meshterm.ui.map_screen import LocationPickScreen

    screen = LocationPickScreen(
        _StubSession(), markers or [], _StubSource(), 14, initial=initial
    )
    screen.future = _Fut()
    return screen


def test_location_picker_opens_on_the_initial_spot_and_commits_the_centre() -> None:
    """The picker centres on the node's current location and Enter resolves it."""
    screen = _picker(initial=(45.5, -73.6))
    screen.render_body(80)
    assert screen._viewport.center_lat == pytest.approx(45.5)
    assert screen._viewport.center_lon == pytest.approx(-73.6)
    assert screen._viewport.zoom == 13

    screen.handle("enter")
    lat, lon = screen.future.value
    assert lat == pytest.approx(45.5) and lon == pytest.approx(-73.6)


def test_location_picker_moves_the_crosshair_and_resets_to_initial() -> None:
    """Panning moves the committed point; ``r`` returns to the starting location."""
    screen = _picker(initial=(45.5, -73.6))
    screen.render_body(80)
    screen.handle("text", "d")  # pan east
    screen.handle("text", "s")  # pan south
    screen.handle("enter")
    lat, lon = screen.future.value
    assert lon > -73.6 and lat < 45.5

    screen.handle("text", "r")  # back to the initial spot
    assert screen._viewport.center_lat == pytest.approx(45.5)
    assert screen._viewport.center_lon == pytest.approx(-73.6)


def test_location_picker_draws_a_live_crosshair_without_keeping_it() -> None:
    """The centre crosshair (with live coordinates) renders each frame but is transient."""
    from meshterm.ui.map_render import MapMarker

    markers = [MapMarker("Repeater", 45.51, -73.61, is_repeater=True)]
    screen = _picker(markers=markers, initial=(45.5, -73.6))
    body = _plain(screen.render_body(80))
    assert "⌖" in body and "45.5" in body  # crosshair + its live coordinates
    assert screen._markers == markers  # not accumulated across frames
    assert "set location" in screen._title(screen._viewport)


def test_location_picker_without_nodes_or_initial_shows_the_world() -> None:
    """With nothing to frame the picker opens on a world view rather than crashing."""
    screen = _picker()
    screen.render_body(80)
    assert screen._viewport.zoom == 2
    screen.handle("text", "r")  # reset with nothing to fit stays on the world view
    assert screen._viewport.zoom == 2
    screen.handle("escape")
    assert screen.future.value is None


# -- the editor's flows against the simulator ---------------------------------------


class _FakeSession:
    """A minimal stand-in for the TUI session behind the editor's persistent menu.

    The editor keeps its main menu *pushed on the session stack* (so sub-prompts float
    over it as modal popups). Here, pushing the menu resolves its future straight from the
    script's next ``select`` answer — the same FIFO the sub-prompts draw from — so the
    modal-backdrop control flow is exercised without a real full-screen session.
    """

    def __init__(self, ui: "_ScriptedUi") -> None:
        self.ui = ui
        self.pushed: list = []

    def push(self, screen: Any) -> None:
        from meshterm.ui.tui.screen import CANCEL

        self.pushed.append(screen)
        value = self.ui._answer("select")
        screen.resolve(CANCEL if value is None else value)

    def pop(self, screen: Any = None) -> None:
        pass


class _ScriptedUi:
    """A fake UI surface answering every prompt from a FIFO script.

    Each entry is ``(method, answer)``; the method name is asserted so a flow that asks
    an unexpected question fails loudly instead of silently mis-answering. It exposes a
    :class:`_FakeSession` so the editor's push-a-persistent-menu path works.
    """

    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = list(script)
        self.notes: list[str] = []
        self.views: list[str] = []
        self.presented: list[str] = []
        self.session = _FakeSession(self)

    def _answer(self, method: str) -> Any:
        assert self.script, f"unexpected prompt: {method}"
        expected, value = self.script.pop(0)
        assert expected == method, f"expected {expected} prompt, got {method}"
        return value

    async def select(self, title: str, items: list, **kwargs: Any) -> Any:
        return self._answer("select")

    async def dialog(self, prompt: str, buttons: list, **kwargs: Any) -> Any:
        return self._answer("dialog")

    async def typed_confirm(self, warning: str, word: str, **kwargs: Any) -> bool:
        return self._answer("typed_confirm")

    async def text(self, title: str, **kwargs: Any) -> Optional[str]:
        return self._answer("text")

    async def path(self, title: str, **kwargs: Any) -> Optional[str]:
        return self._answer("path")

    async def autocomplete(self, title: str, choices: list, **kwargs: Any) -> Optional[str]:
        return self._answer("autocomplete")

    async def confirm(self, title: str, **kwargs: Any) -> Optional[bool]:
        return self._answer("confirm")

    async def view(self, renderable: Any, **kwargs: Any) -> None:
        self.views.append(str(kwargs.get("title", "")))

    def note(self, markup: str) -> None:
        self.notes.append(markup)

    def show(self, *renderables: Any) -> None:
        pass

    async def present(self, *, title: str = "") -> None:
        self.presented.append(title)

    def discard(self) -> None:
        pass


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context whose UI is swapped per test."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "cfg.db")
    context = AppContext(
        console=Console(file=__import__("io").StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


def _install(ctx: AppContext, script: list[tuple[str, Any]]) -> _ScriptedUi:
    """Install a scripted UI on the context and return it."""
    ui = _ScriptedUi(script)
    ctx.ui = ui  # type: ignore[assignment]
    return ui


async def test_editor_stages_a_setting_and_returns_ops_on_apply(ctx: AppContext) -> None:
    """Editing a value stages it; Apply returns the set operations for the tool to run."""
    _install(ctx, [
        ("select", "name"),
        ("text", "NewName"),
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ops == [("set", "name", "NewName")]


async def test_editor_restaging_the_current_value_clears_the_stage(ctx: AppContext) -> None:
    """Typing the device's existing value back un-stages the row (nothing to apply)."""
    device = await ctx.device()
    current = (await device.get_self_info())["name"]
    _install(ctx, [
        ("select", "name"),
        ("text", "Changed"),
        ("select", "name"),
        ("text", str(current)),  # back to what the device already has
        ("select", "__cancel__"),  # nothing staged now — closes without a discard dialog
    ])
    assert await edit_config(ctx) is None


async def test_editor_stages_background_advert_cadence(ctx: AppContext) -> None:
    """Picking a cadence stages it under its sentinel; Apply maps it to its own op."""
    _install(ctx, [
        ("select", "__advert_flood__"),
        ("select", 48),  # every 48 h
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ops == [("advert_cadence", True, 48)]


async def test_editor_repicking_the_cadence_in_force_clears_the_stage(ctx: AppContext) -> None:
    """Choosing the cadence already in force un-stages the row (nothing to apply)."""
    _install(ctx, [
        ("select", "__advert_direct__"),
        ("select", 4),
        ("select", "__advert_direct__"),
        ("select", 1),  # back to the default in force
        ("select", "__cancel__"),  # nothing staged now — closes without a discard dialog
    ])
    assert await edit_config(ctx) is None


async def test_editor_asks_before_discarding_staged_changes(ctx: AppContext) -> None:
    """Cancelling with staged changes confirms; Keep editing returns to the menu."""
    _install(ctx, [
        ("select", "name"),
        ("text", "NewName"),
        ("select", "__cancel__"),
        ("dialog", "keep"),  # changed my mind — keep editing
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ops == [("set", "name", "NewName")]


async def test_editor_discards_staged_changes_when_confirmed(ctx: AppContext) -> None:
    """Cancelling with staged changes and confirming the discard applies nothing."""
    _install(ctx, [
        ("select", "name"),
        ("text", "NewName"),
        ("select", None),  # Esc from the main menu
        ("dialog", "discard"),
    ])
    assert await edit_config(ctx) is None


async def test_editor_clean_close_needs_no_confirmation(ctx: AppContext) -> None:
    """With nothing staged, Close leaves immediately — no dialog in the script."""
    _install(ctx, [("select", "__cancel__")])
    assert await edit_config(ctx) is None


async def test_editor_location_typed_coordinates_stage_both_axes(ctx: AppContext) -> None:
    """Typing a coordinate pair stages latitude and longitude together."""
    _install(ctx, [
        ("select", "__location__"),
        ("dialog", "type"),
        ("text", "45.5, -73.6"),
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ("set", "adv_lat", 45.5) in ops
    assert ("set", "adv_lon", -73.6) in ops


async def test_editor_location_map_pick_stages_rounded_coordinates(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking on the map stages the chosen point, rounded to advert precision."""
    async def _fake_pick(_ctx: AppContext, *, initial=None):
        assert initial is None  # the mock device has no fix (0, 0)
        return (45.51234567, -73.65432109)

    monkeypatch.setattr("meshterm.ui.map_screen.pick_location", _fake_pick)
    _install(ctx, [
        ("select", "__location__"),
        ("dialog", "map"),
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ("set", "adv_lat", 45.512346) in ops
    assert ("set", "adv_lon", -73.654321) in ops


async def test_editor_location_clear_stages_the_no_fix_pair(ctx: AppContext) -> None:
    """Clear stages 0, 0 — MeshCore's "no fix" value — for both axes."""
    device = await ctx.device()
    await device.set_coords(45.5, -73.6)  # give the device a position to clear
    _install(ctx, [
        ("select", "__location__"),
        ("dialog", "clear"),
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ("set", "adv_lat", 0.0) in ops
    assert ("set", "adv_lon", 0.0) in ops


async def test_actions_advert_zero_hop_and_flood_run_immediately(ctx: AppContext) -> None:
    """The advert dialog sends immediately: zero-hop by default, flood when chosen."""
    device = await ctx.device()
    sent: list[bool] = []

    async def _record(flood: bool = False) -> None:
        sent.append(flood)

    device.send_advert = _record  # type: ignore[method-assign]
    ui = _install(ctx, [
        ("select", "__advert__"),
        ("select", "zero"),
        ("select", "__advert__"),
        ("select", "flood"),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert sent == [False, True]
    assert any("zero-hop" in n for n in ui.notes)
    assert any("flood" in n for n in ui.notes)


async def test_actions_advert_share_shows_the_contact_card(ctx: AppContext) -> None:
    """The share option opens the QR/URI view built from the node's own identity."""
    ui = _install(ctx, [
        ("select", "__advert__"),
        ("select", "share"),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert any(v.startswith("Share ") for v in ui.views)


async def test_send_advert_standalone_sends_without_the_actions_screen(ctx: AppContext) -> None:
    """The promoted main-menu flow sends an advert directly — no Device actions hop."""
    device = await ctx.device()
    sent: list[bool] = []

    async def _record(flood: bool = False) -> None:
        sent.append(flood)

    device.send_advert = _record  # type: ignore[method-assign]
    ui = _install(ctx, [("select", "flood")])
    await send_advert(ctx)
    assert sent == [True]
    assert any("flood" in n for n in ui.notes)


async def test_send_advert_standalone_shares_the_contact_card(ctx: AppContext) -> None:
    """The standalone flow's share option opens the same QR/URI contact-card view."""
    ui = _install(ctx, [("select", "share")])
    await send_advert(ctx)
    assert any(v.startswith("Share ") for v in ui.views)


async def test_send_advert_standalone_backs_out_cleanly(ctx: AppContext) -> None:
    """Backing out of the advert select sends nothing and returns to the menu."""
    device = await ctx.device()

    async def _boom(flood: bool = False) -> None:  # pragma: no cover - must not run
        raise AssertionError("no advert should be sent on Back")

    device.send_advert = _boom  # type: ignore[method-assign]
    _install(ctx, [("select", None)])
    await send_advert(ctx)


async def test_actions_factory_reset_gates_on_typed_confirmation(ctx: AppContext) -> None:
    """Factory reset only runs behind the typed dialog."""
    device = await ctx.device()
    await device.set_custom_var("mode", "test")

    # Declined: the device is untouched.
    _install(ctx, [
        ("select", "__reset__"),
        ("typed_confirm", False),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert await device.get_custom_vars() == {"mode": "test"}

    # Confirmed: the reset runs and the device comes back empty.
    _install(ctx, [
        ("select", "__reset__"),
        ("typed_confirm", True),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert await device.get_custom_vars() == {}


async def test_actions_import_key_gates_on_typed_confirmation(ctx: AppContext) -> None:
    """Importing an identity key requires the typed IMPORT confirmation."""
    device = await ctx.device()
    new_key = "ab" * 32
    _install(ctx, [
        ("select", "__identity_key__"),
        ("select", "import"),
        ("text", new_key),
        ("typed_confirm", True),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert await device.export_private_key() == new_key


async def test_actions_reboot_on_simulator_stays_on_the_screen(ctx: AppContext) -> None:
    """The simulator has no link to drop, so a confirmed reboot just notes and returns."""
    ui = _install(ctx, [
        ("select", "__reboot__"),
        ("dialog", "reboot"),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert ctx.reboot_in_progress is False
    assert any("rebooting" in n for n in ui.notes)


async def test_actions_sync_clock_corrects_the_device_time(ctx: AppContext) -> None:
    """Sync clock shows the drift, and confirming writes the host time to the device."""
    import time

    device = await ctx.device()
    assert (await device.get_time()) < int(time.time())  # the mock boots with drift
    ui = _install(ctx, [
        ("select", "__sync_clock__"),
        ("dialog", "sync"),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert abs((await device.get_time()) - int(time.time())) <= 2
    assert any("clock set to" in n for n in ui.notes)


async def test_actions_sync_clock_cancel_leaves_the_clock_alone(ctx: AppContext) -> None:
    """Backing out of the sync dialog changes nothing."""
    device = await ctx.device()
    before = await device.get_time()
    _install(ctx, [
        ("select", "__sync_clock__"),
        ("dialog", None),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert abs((await device.get_time()) - before) <= 2  # still ticking on the old drift


async def test_editor_stages_flood_scope(ctx: AppContext) -> None:
    """The flood scope is a first-class staged setting, hashtag-normalized on apply."""
    from meshterm.core.device_config import build_snapshot
    from meshterm.tools.config import apply_ops

    _install(ctx, [
        ("select", "flood_scope"),
        ("text", "alpha"),
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ops == [("set", "flood_scope", "alpha")]

    device = await ctx.device()
    await apply_ops(ctx, device, await build_snapshot(device), ops)
    assert await device.get_default_flood_scope() == "#alpha"


async def test_actions_backup_writes_immediately(ctx: AppContext, tmp_path: Path) -> None:
    """Backup runs at once: the TOML lands on disk before the screen closes."""
    target = tmp_path / "out" / "backup.toml"
    ui = _install(ctx, [
        ("select", "__backup__"),
        ("path", str(target)),
        ("select", "__cancel__"),
    ])
    await device_actions(ctx)
    assert target.exists()
    assert any("wrote" in n for n in ui.notes)


async def test_editor_bool_prompt_uses_the_button_dialog(ctx: AppContext) -> None:
    """A boolean setting is toggled through the On/Off dialog and staged."""
    _install(ctx, [
        ("select", "manual_add_contacts"),
        ("dialog", True),
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ops == [("set", "manual_add_contacts", True)]


async def test_editor_custom_var_suggests_known_names(ctx: AppContext) -> None:
    """With existing custom vars the name prompt offers them as suggestions."""
    device = await ctx.device()
    await device.set_custom_var("existing", "1")
    _install(ctx, [
        ("select", "__custom__"),
        ("autocomplete", "existing"),
        ("text", "2"),
        ("select", "__apply__"),
    ])
    ops = await edit_config(ctx)
    assert ops == [("set_custom", "existing", "2")]
