"""P4 chrome contracts: borderless frame, slim header, the F-key lane, dialog gate."""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from meshterm.platforms import PICOCALC, REGULAR, set_platform
from meshterm.ui.tui import frame
from meshterm.ui.tui.fkeys import DEFAULT_LANE, FPair, action_for, lane_text
from meshterm.ui.tui.screen import ScrollScreen

from tests.conftest import plain as _plain


def _screen(lines: int = 40) -> ScrollScreen:
    body = Text("\n".join(f"row {i}" for i in range(lines)))
    screen = ScrollScreen(body, title="Chrome probe", floating=False)
    return screen


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


def test_picocalc_header_drops_version_and_pulse() -> None:
    """The header_atoms wiring: no brand/version mark, no pulse row under picocalc."""
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
    segments = _header_segments(_Ctx(), {})
    text = "".join(seg.plain for seg in segments)
    assert "MeshTerm" not in text and "v" not in text.split()[0]
    assert "simulator" in text
    set_platform(REGULAR)
    segments = _header_segments(_Ctx(), {})
    assert "MeshTerm" in "".join(seg.plain for seg in segments)


def test_fkey_lane_resolution_and_banks() -> None:
    lane = DEFAULT_LANE
    assert action_for(lane, 1) == "home" and action_for(lane, 6) == "end"
    assert action_for(lane, 2) == "pageup" and action_for(lane, 7) == "pagedown"
    assert action_for(lane, 3) is None and action_for(lane, 8) is None
    assert action_for(lane, 5) == "enter" and action_for(lane, 10) == "escape"


def test_fkey_lane_text_fits_and_flips() -> None:
    primary = lane_text(DEFAULT_LANE)
    shifted = lane_text(DEFAULT_LANE, shifted=True)
    assert cell_len(primary.plain) <= 53 and cell_len(shifted.plain) <= 53
    assert "1 Top" in primary.plain and "5 OK" in primary.plain
    assert "6 End" in shifted.plain and "10 Back" in shifted.plain
    wide = [FPair("Muchtoolonglabel", "a", "Muchtoolonglabel", "b")] * 5
    assert cell_len(lane_text(wide).plain) <= 53  # clipped to the slot budget
    assert cell_len(lane_text(wide, shifted=True).plain) <= 53


def test_dialog_gate_shrinks_on_picocalc() -> None:
    from meshterm.ui.surface import _dialog_max_cells

    set_platform(PICOCALC)
    assert _dialog_max_cells() == 43
    set_platform(REGULAR)
    assert _dialog_max_cells() == 76
