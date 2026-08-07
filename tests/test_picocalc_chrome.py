"""P4 chrome contracts: borderless frame, slim header, the F-key lane, dialog gate."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from meshterm.platforms import PICOCALC, REGULAR, set_platform
from meshterm.ui.tui import frame
from meshterm.ui.tui.fkeys import (
    DEFAULT_LANE,
    FPair,
    action_for,
    default_lane,
    lane_text,
)
from meshterm.ui.tui.screen import ScrollScreen

from tests.conftest import plain as _plain


def _screen(lines: int = 40) -> ScrollScreen:
    body = Text("\n".join(f"row {i}" for i in range(lines)))
    screen = ScrollScreen(body, title="Chrome probe", floating=False)
    return screen


def _slot_style(row: Text, index: int) -> str:
    """The style the lane painted onto slot ``index``'s chip (slots are 9 cells + 2 gap)."""
    start = index * 11
    return next(str(span.style) for span in row.spans if span.start <= start < span.end)


def test_borderless_frame_swaps_the_panel_for_a_title_bar() -> None:
    set_platform(PICOCALC)
    composed = frame.compose_base(Text("hdr"), _screen(), "hint", 53, 26).split("\n")
    assert len(composed) == 26
    plain = [_plain(line) for line in composed]
    # Row 1 (under the one-line header) is the title bar: rule + title + clip arrow.
    assert "Chrome probe" in plain[1] and "─" in plain[1]
    assert "↓" in plain[1]  # 40 rows into a 23-row viewport: more below
    # No Panel borders anywhere — the body owns the full width.
    assert not any("│" in line or "╭" in line for line in plain)
    assert all(cell_len(line) <= 53 for line in plain)
    # The body's first row starts at column 0 of row 2 (no panel padding).
    assert plain[2].startswith("row 0")


def test_bordered_frame_is_unchanged_on_regular() -> None:
    set_platform(REGULAR)
    composed = frame.compose_base(Text("hdr"), _screen(), "hint", 72, 24).split("\n")
    plain = [_plain(line) for line in composed]
    assert any("╭" in line or "┌" in line for line in plain), "regular keeps its Panel"


def test_picocalc_header_brands_the_app_not_the_device() -> None:
    """The header_atoms wiring, per JP's spec: picocalc shows ``MeshTerm vX`` (a soldered
    radio's port never changes, so the device segment earns nothing), regular keeps both."""
    from meshterm.ui.menu import _header_segments

    class _Badges:
        def unread_total(self) -> int:
            return 0

        def unacked_count(self) -> int:
            return 0

    class _Ctx:
        mock = True
        chat = _Badges()
        watchtower = _Badges()

    set_platform(PICOCALC)
    text = "".join(seg.plain for seg in _header_segments(_Ctx(), {}))
    assert "MeshTerm" in text
    assert "simulator" not in text  # the device atom is composed out
    assert "pulse" in PICOCALC.header_atoms  # the sparkline takes the remaining room
    set_platform(REGULAR)
    text = "".join(seg.plain for seg in _header_segments(_Ctx(), {}))
    assert "MeshTerm" in text and "simulator" in text


def test_fkey_lane_resolution_and_banks() -> None:
    lane = DEFAULT_LANE
    # F4/F5 (paging — no physical key at all) are plain-key only, no Shift companion.
    assert action_for(lane, 4) == "pageup" and action_for(lane, 9) is None
    assert action_for(lane, 5) == "pagedown" and action_for(lane, 10) is None
    # F1/F2 carry the Fn-layered jump-to-top/end pair, also with no Shift companion.
    assert action_for(lane, 1) == "home" and action_for(lane, 6) is None
    assert action_for(lane, 2) == "end" and action_for(lane, 7) is None
    # F3 is the lone, unassigned slot a screen's own lane fills in.
    assert action_for(lane, 3) is None and action_for(lane, 8) is None
    # Enter/Esc never occupy a slot — both keys are already close at hand.
    assert "enter" not in {action_for(lane, n) for n in range(1, 11)}
    assert "escape" not in {action_for(lane, n) for n in range(1, 11)}


def test_fkey_lane_text_fits_and_flips() -> None:
    primary = lane_text(DEFAULT_LANE)
    shifted = lane_text(DEFAULT_LANE, shifted=True)
    assert cell_len(primary.plain) == 53 and cell_len(shifted.plain) == 53
    assert "F1 Top" in primary.plain and "F4 PgUp" in primary.plain and "F5 PgDn" in primary.plain
    # No slot has a Shift companion by default: the whole shifted bank renders unfilled.
    for number in (6, 7, 8, 9):
        assert f"F{number}" in shifted.plain
    assert "10" in shifted.plain
    wide = [FPair("Muchtoolonglabel", "a", "Muchtoolonglabel", "b")] * 5
    assert cell_len(lane_text(wide).plain) == 53  # clipped to the slot budget
    assert cell_len(lane_text(wide, shifted=True).plain) == 53


def test_fkey_slot_dims_when_its_action_is_unavailable() -> None:
    """An unavailable slot keeps its label and drops its fill — it never offers a dead key."""
    lane = [FPair("Paths", "paths", "Retry", "retry", enabled=False)] + [None] * 4
    primary = lane_text(lane)
    shifted = lane_text(lane, shifted=True)

    # The label survives: the lane still says what F1 is for, just not that it acts now.
    assert "F1 Paths" in primary.plain and _slot_style(primary, 0) == "muted"
    # Its Shift companion is untouched — the two banks gate independently.
    assert "F6 Retry" in shifted.plain and _slot_style(shifted, 0) == "fkey.chip.shift"
    # The row is still exactly the lane's width, dim chips and all.
    assert cell_len(primary.plain) == 53 and cell_len(shifted.plain) == 53
    # Dimming is presentational: the key still resolves, and the screen's handler no-ops.
    assert action_for(lane, 1) == "paths"


def test_default_lane_dims_every_nav_slot_when_nothing_moves() -> None:
    """A screen with nothing to move gets the shared lane back inert, not absent."""
    assert default_lane() is DEFAULT_LANE  # the live lane is the constant itself
    dim = default_lane(nav=False)

    assert [pair.label for pair in dim if pair] == ["Top", "End", "PgUp", "PgDn"]
    assert not any(pair.enabled for pair in dim if pair)
    row = lane_text(dim)
    assert cell_len(row.plain) == 53 and "F1 Top" in row.plain and "F5 PgDn" in row.plain
    assert {str(span.style) for span in row.spans} == {"muted"}


def test_lane_is_built_after_the_body_renders() -> None:
    """The frame defers the lane, so its gates read this paint's metrics, not the last one's."""
    set_platform(PICOCALC)
    screen = _screen()  # 40 body rows into a 23-row viewport: it overflows
    seen: dict[str, bool] = {}

    def build() -> Text:
        seen["nav"] = all(pair.enabled for pair in screen.fkey_lane if pair)
        return lane_text(screen.fkey_lane)

    composed = frame.compose_base(Text("hdr"), screen, "hint", 53, 26, footer_lane=build)

    assert seen["nav"] is True  # lit on the very first paint, with no stale frame to lag
    assert "F4 PgUp" in _plain(composed.split("\n")[-1])


def test_scroll_screen_lane_tracks_whether_its_body_overflows() -> None:
    """The result window's nav slots light only once there is something to scroll to."""
    screen = _screen()
    screen.note_metrics(4, 20)  # the whole body fits the viewport
    assert not any(pair.enabled for pair in screen.fkey_lane if pair)
    screen.note_metrics(80, 20)  # taller than the viewport
    assert all(pair.enabled for pair in screen.fkey_lane if pair)


def test_host_battery_reads_the_sysfs_supply(tmp_path, monkeypatch) -> None:
    """The PicoCalc battery path: a true percent and charging flag straight from sysfs."""
    from meshterm.services import battery_service

    (tmp_path / "capacity").write_text("53\n")
    (tmp_path / "status").write_text("Discharging\n")
    (tmp_path / "voltage_now").write_text("4012000\n")
    monkeypatch.setattr(battery_service, "_HOST_SUPPLY", tmp_path)
    service = battery_service.BatteryService(ctx=None)
    service._poll_host()
    reading = service.reading()
    assert reading is not None
    assert (reading.percent, reading.charging, reading.millivolts) == (53, False, 4012)

    (tmp_path / "status").write_text("Charging\n")
    service._poll_host()
    assert service.reading().charging is True

    monkeypatch.setattr(battery_service, "_HOST_SUPPLY", tmp_path / "gone")
    service._poll_host()
    assert service.reading() is None  # unreadable supply = absent, header draws nothing


def test_dialog_gate_shrinks_on_picocalc() -> None:
    from meshterm.ui.surface import _dialog_max_cells

    set_platform(PICOCALC)
    assert _dialog_max_cells() == 43
    set_platform(REGULAR)
    assert _dialog_max_cells() == 76
