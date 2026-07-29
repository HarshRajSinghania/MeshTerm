"""Repeater-admin tests: the settings catalog, the per-node store, the simulated
remote CLI, and the command-line screen's readline behavior."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import MockDevice
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import Contact
from meshterm.core.remote_config import (
    REPEATER_SETTINGS,
    get_setting,
    known_commands,
    parse_reply_value,
    validate_value,
)
from meshterm.core.remote_store import HISTORY_CAP, RemoteStore
from meshterm.persistence.repository import Repository
from meshterm.ui.remote_cli import RemoteCliScreen
from meshterm.ui.repeater_admin import open_repeater_admin
from meshterm.ui.surface import TuiUi
from meshterm.ui.tui.prompt import TextScreen
from meshterm.ui.tui.screen import CANCEL
from meshterm.ui.tui.select import SelectScreen
from meshterm.ui.tui.session import TuiSession

NODE = Contact(name="Yagi-Repeater", public_key="a1" * 32, key_prefix="a1b2c3d4")


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


# --- the catalog ----------------------------------------------------------------------


def test_catalog_carries_the_repeater_only_knobs() -> None:
    """TX delay and Direct TX delay — the reason this feature exists — are first-class."""
    txdelay = get_setting("txdelay")
    direct = get_setting("direct.txdelay")
    assert txdelay is not None and txdelay.set_command("5") == "set txdelay 5"
    assert direct is not None and direct.get_command == "get direct.txdelay"
    assert any(s.key == "af" for s in REPEATER_SETTINGS)
    assert any(s.key == "advert.interval" for s in REPEATER_SETTINGS)


def test_parse_reply_value_survives_terse_and_verbose_firmware() -> None:
    """Numbers are extracted from any phrasing; errors parse as unknown, not values."""
    tx = get_setting("tx")
    assert parse_reply_value(tx, "20") == "20"
    assert parse_reply_value(tx, "tx: 20") == "20"
    assert parse_reply_value(tx, "TX power = 20 dBm") == "20"
    assert parse_reply_value(tx, "ERR: unknown config: tx") is None
    assert parse_reply_value(tx, None) is None

    name = get_setting("name")
    assert parse_reply_value(name, "> Yagi") == "Yagi"
    assert parse_reply_value(name, "name: Yagi") == "Yagi"

    repeat = get_setting("repeat")
    assert parse_reply_value(repeat, "on") == "on"
    assert parse_reply_value(repeat, "repeat is off") == "off"


def test_validate_value_enforces_kind_and_bounds() -> None:
    """Prompt validation speaks in the setting's own terms."""
    sf = get_setting("sf")
    assert validate_value(sf, "9") is True
    assert "≥ 7" in validate_value(sf, "3")
    assert "number" in validate_value(sf, "fast")
    assert validate_value(get_setting("repeat"), "maybe") == "Enter on or off."
    assert validate_value(get_setting("name"), "  ") == "Enter a value."


def test_known_commands_cover_catalog_and_verbs() -> None:
    """The CLI completions include every catalog spelling plus the fixed verbs."""
    commands = known_commands()
    assert "get txdelay" in commands
    assert "set direct.txdelay " in commands
    assert "reboot" in commands and "ver" in commands
    assert "get guest.password" not in commands  # write-only stays uncompletable


# --- the store ------------------------------------------------------------------------


def test_remote_store_caches_settings_per_node(tmp_path: Path) -> None:
    """Values round-trip with their read stamps, keyed per node."""
    store = RemoteStore(tmp_path / "remote.json")
    store.remember_setting(NODE, "txdelay", "5")
    cached = store.settings(NODE)
    assert cached["txdelay"].value == "5"
    assert cached["txdelay"].read_at is not None
    other = Contact(name="Other", public_key="b2" * 32)
    assert store.settings(other) == {}


def test_remote_store_history_dedupes_and_caps(tmp_path: Path) -> None:
    """History appends in order, skips adjacent repeats, and stays capped."""
    store = RemoteStore(tmp_path / "remote.json")
    store.append_history(NODE, "get tx")
    store.append_history(NODE, "get tx")  # adjacent repeat — no stutter on recall
    store.append_history(NODE, "set tx 20")
    assert store.history(NODE) == ["get tx", "set tx 20"]
    for i in range(HISTORY_CAP + 20):
        store.append_history(NODE, f"cmd {i}")
    history = store.history(NODE)
    assert len(history) == HISTORY_CAP and history[-1] == f"cmd {HISTORY_CAP + 19}"


# --- the simulated remote CLI ----------------------------------------------------------


async def test_mock_remote_cli_requires_login() -> None:
    """A stranger's command reads as a timeout (None), exactly like hardware."""
    device = MockDevice()
    await device.connect()
    assert await device.send_remote_command(NODE, "get tx") is None
    await device.disconnect()


async def test_mock_remote_cli_round_trips_settings() -> None:
    """get/set work for catalog keys; unknown keys answer with an error string."""
    device = MockDevice()
    await device.connect()
    assert await device.admin_login(NODE, "admin")
    assert await device.send_remote_command(NODE, "get txdelay") == "> 0"
    assert await device.send_remote_command(NODE, "set txdelay 5") == "OK"
    assert await device.send_remote_command(NODE, "get txdelay") == "> 5"
    assert await device.send_remote_command(NODE, "set tx 22") == "OK"
    assert await device.get_remote_tx_power(NODE) == 22  # one shared TX state
    reply = await device.send_remote_command(NODE, "get nonsense")
    assert reply is not None and "unknown" in reply.lower()
    assert "simulator" in (await device.send_remote_command(NODE, "ver"))
    await device.disconnect()


# --- the login flow: the password floats over the node picker, never a blank frame ----


@pytest.fixture()
def tui_ctx(tmp_path: Path) -> AppContext:
    """A mock-backed context wired to a headless TUI session (no prompt_toolkit app)."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "admin.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    ctx.ui = TuiUi(TuiSession())
    yield ctx
    ctx.repo.close()


async def _step_until(predicate, *, limit: int = 200):
    """Yield to the event loop until ``predicate()`` is truthy; return it (or its last value)."""
    value = predicate()
    for _ in range(limit):
        if value:
            return value
        await asyncio.sleep(0)
        value = predicate()
    return value


async def test_login_password_floats_over_the_node_picker(tui_ctx) -> None:
    """The admin-login password prompt floats over the picker, not an erased background.

    Regression: ``_login`` ran on an empty stack (the picker was popped when it returned),
    so ``session.text`` pushed a *blank* base and the password box floated over an erased
    frame. ``open_repeater_admin`` now redraws the node picker as a static backdrop and keeps
    it pushed across the login, so the base under the floating prompt is the picker — the
    picked node still highlighted — never a blank frame.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    pick_title = "Repeater admin — node to manage"

    task = asyncio.ensure_future(open_repeater_admin(ctx))
    try:
        picker = await _step_until(
            lambda: session.top
            if isinstance(session.top, SelectScreen) and session.top.title == pick_title
            else None
        )
        assert picker is not None, "the node picker never opened"

        picker.resolve("Yagi-Repeater")  # pick the repeater to administer
        prompt = await _step_until(
            lambda: session._float_layers()[0] if session._has_float() else None
        )
        assert isinstance(prompt, TextScreen)  # the password box floats

        base = session._base_screen()
        assert isinstance(base, SelectScreen) and base.title == pick_title  # the picker backdrop
        assert base is not picker  # a fresh redraw kept as the backdrop, not the popped picker
        current = base._current_choice()  # the picked node stays highlighted behind the prompt
        assert current is not None and current.value == "Yagi-Repeater"

        prompt.resolve(CANCEL)  # Esc — abandon the login
        result = await task
    finally:
        if not task.done():
            task.cancel()

    assert result is None  # cancelling the password bows the flow out


# --- the command-line screen -----------------------------------------------------------


def _cli(history=None, sent=None) -> RemoteCliScreen:
    return RemoteCliScreen(
        node_label="Yagi-Repeater",
        history=list(history or []),
        send=(sent.append if sent is not None else lambda c: None),
        session=_FakeSession(),
    )


def _type(screen: RemoteCliScreen, text: str) -> None:
    for ch in text:
        screen.handle("text", ch)


def test_cli_screen_sends_on_enter_and_parks_while_waiting() -> None:
    """Enter commits the buffer once; a command in flight blocks the next send."""
    sent: list[str] = []
    screen = _cli(sent=sent)
    _type(screen, "get tx")
    screen.handle("enter")
    assert sent == ["get tx"]
    screen.sent("get tx")  # the owner echoes and parks the prompt
    _type(screen, "ver")
    screen.handle("enter")
    assert sent == ["get tx"]  # parked: nothing new goes out until the reply lands
    screen.reply("20")
    screen.handle("enter")  # the typed-while-parked buffer is still there to send
    assert sent == ["get tx", "ver"]


def test_cli_screen_history_recall_keeps_the_draft() -> None:
    """↑ walks back through history; ↓ past the newest restores the unsent draft."""
    screen = _cli(history=["get tx", "set tx 20"])
    _type(screen, "dra")
    screen.handle("up")
    assert screen._editor.text == "set tx 20"
    screen.handle("up")
    assert screen._editor.text == "get tx"
    screen.handle("down")
    screen.handle("down")
    assert screen._editor.text == "dra"  # the draft came back


def test_cli_screen_tab_completes_known_commands() -> None:
    """Tab adopts the first known command extending the typed prefix."""
    screen = _cli()
    _type(screen, "get txd")
    screen.handle("tab")
    assert screen._editor.text == "get txdelay"


def test_cli_screen_transcript_shows_exchange() -> None:
    """The transcript keeps the echoed command and its reply, prompt at the bottom."""
    screen = _cli()
    screen.sent("get txdelay")
    screen.reply("> 5")
    body = "\n".join(screen.render_body(80))
    assert "get txdelay" in body and "> 5" in body
    assert screen.cursor_line() == screen._prompt_line  # the view follows the prompt


def test_cli_screen_timeout_note_frees_the_prompt() -> None:
    """A timeout lands as a muted note and the prompt accepts input again."""
    screen = _cli()
    screen.sent("advert")
    assert screen.busy
    screen.failed("no reply within 10 s")
    assert not screen.busy
    assert "no reply within 10 s" in "\n".join(screen.render_body(80))


# --- the editor menu's column header --------------------------------------------------


def test_admin_menu_pins_the_column_header_over_the_category() -> None:
    """Scrolled deep, the lane names stay overhead with the category heading under them."""
    import re

    from meshterm.ui.repeater_admin import _menu_items
    from meshterm.ui.tui import frame

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    title, items = _menu_items(NODE, {}, {})
    screen = SelectScreen(title, items, wrap=False)
    for _ in range(18):  # down past the first categories
        screen.handle("down")
    visible, above, _below = frame._visible_slice(screen, screen.render_body(72), 10)
    top = [ansi.sub("", row).strip() for row in visible[:2]]
    assert top[0].startswith("SETTING") and top[0].endswith("DESCRIPTION")
    assert top[1].startswith("──")  # the category the highlighted row sits in
    assert above is True


def test_admin_menu_header_abbreviates_rather_than_wrapping() -> None:
    """Too narrow for the whole line, the last label shortens — the header stays one row."""
    from rich.cells import cell_len

    from meshterm.ui.repeater_admin import _menu_items

    _title, items = _menu_items(NODE, {}, {})
    header = items[0]
    assert header.pinned
    full = header.text(100)
    assert full.endswith("DESCRIPTION")
    narrow = header.text(cell_len(full) - 2)
    assert narrow.endswith("DESC") and "\n" not in narrow
