"""The P2 performance contracts: what must stay cheap, and what must stay off the boot path.

These are behaviour tests, not benchmarks — a wall-clock assertion would be flaky on CI and
would say nothing about *why* a regression happened. Each test instead pins the structural
property the optimisation rests on: the database opens in the mode that makes small writes
cheap, the frame skips the passes that cannot change its pixels, and the startup import graph
stays clear of the three subtrees that were dragging a fifth of a second each onto every run.

The numbers behind these choices were measured on the PicoCalc (Luckfox Lyra, ~1 GHz
Cortex-A7, SD card) and are recorded in the plan's Measurements appendix.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from rich.text import Text

from meshterm.persistence import db
from meshterm.platforms import PICOCALC, REGULAR, set_platform
from meshterm.ui.tui.frame import compose_base
from meshterm.ui.tui.screen import Screen
from meshterm.ui.tui.session import _has_wide_glyph
from meshterm.ui.tui.spinner import Spinner


# --- the database's write path ------------------------------------------------------


def test_connect_opens_in_wal_mode(tmp_path: Path) -> None:
    """WAL is what turns each tiny insert into an append instead of a journal rewrite."""
    conn = db.connect(tmp_path / "wal.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_relaxed_sync_is_only_taken_together_with_wal(tmp_path: Path) -> None:
    """``synchronous=NORMAL`` is safe under WAL and corrupting without it — never split.

    Under WAL, NORMAL risks losing the last transactions to a power cut; under the rollback
    journal it risks the file itself. The PicoCalc runs on a battery pack, so the two
    settings travel together or not at all.
    """
    conn = db.connect(tmp_path / "sync.db")
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    if journal == "wal":
        assert synchronous == 1  # NORMAL
    else:  # pragma: no cover - only on a filesystem that refuses WAL
        assert synchronous == 2  # FULL, the safe default left untouched


def test_temp_store_is_memory(tmp_path: Path) -> None:
    """Sorts and temporary b-trees stay in RAM rather than landing on the SD card."""
    conn = db.connect(tmp_path / "temp.db")
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY


def test_tuning_survives_a_reopen(tmp_path: Path) -> None:
    """WAL is persistent, so a second session inherits it — and with it the relaxed sync."""
    path = tmp_path / "reopen.db"
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


# --- what must not be imported at startup -------------------------------------------

#: Subtrees that cost a fifth of a second *each* to import on the Lyra and are needed by no
#: startup path: PyCryptodome probes the CPU's crypto features (reached only through
#: ``core.channels``, whose constants the boot path wanted), and the map screen drags the
#: basemap's tile fetcher — ``urllib.request`` → ``http.client`` → ``ssl`` — behind it.
_MUST_STAY_LAZY = ("Crypto", "urllib.request", "http.client", "meshterm.ui.map_screen")


def test_startup_does_not_import_the_heavy_optional_subtrees() -> None:
    """``import meshterm.cli`` stays clear of crypto, the network stack, and the map.

    Run in a subprocess because the assertion is about a *fresh* interpreter's import graph:
    by the time this suite is running, half the codebase is already in ``sys.modules``.
    """
    probe = (
        "import sys, meshterm.cli;"
        "print(','.join(sorted(m for m in sys.modules"
        f" if any(m == h or m.startswith(h + '.') for h in {_MUST_STAY_LAZY!r}))))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"these should not load at startup: {out}"


def test_the_deferred_crypto_still_decrypts() -> None:
    """Deferring the import must not have cost the function its names."""
    from meshterm.core.channels import decrypt_channel_text, identify_channel

    # No known channel matches, but reaching the "no match" answer means HMAC/AES resolved.
    assert identify_channel("ab", "cdef", "00" * 16, [("#public", b"\x01" * 16)]) is None
    assert decrypt_channel_text("ab", "cdef", "00" * 16, [("#public", b"\x01" * 16)]) is None


def test_map_default_fraction_is_the_same_constant_from_either_module() -> None:
    """The map screen re-exports the geo constant, so existing importers still see one value."""
    from meshterm.core.geo import DEFAULT_VIEW_FRACTION as from_geo
    from meshterm.ui.map_screen import DEFAULT_VIEW_FRACTION as from_screen

    assert from_geo == from_screen


def test_widgets_does_not_drag_the_spatial_modules() -> None:
    """``ui.widgets`` reads its mark constants from ``ui.marks``, not the heavy hosts.

    The P2 sweep measured ~60 ms of Lyra import time in ``widgets`` pulling
    ``map_render``/``mapcanvas``/``pathgraph`` for constants alone; the constants moved
    to the dependency-free ``ui.marks`` in P3. Same subprocess trick as the startup
    guard: the claim is about a fresh interpreter's import graph.
    """
    heavy = ("meshterm.ui.map_render", "meshterm.ui.mapcanvas", "meshterm.ui.pathgraph")
    probe = (
        "import sys, meshterm.ui.widgets;"
        f"print(','.join(sorted(m for m in sys.modules if m in {heavy!r})))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "", f"widgets should not drag these anymore: {out}"


# --- per-frame work the platform can skip -------------------------------------------


class _Body(Screen):
    """A minimal screen, just enough for ``compose_base`` to frame something."""

    title = "Perf"

    def render_body(self, width: int) -> list[str]:  # noqa: D102 - inherited docstring
        return ["body"]


def _glow_calls(cols: int = 60, rows: int = 20) -> int:
    """Compose one frame, counting how many times the corner-glow pass ran."""
    import meshterm.ui.tui.frame as frame_mod

    original = frame_mod.apply_corner_glow
    calls = 0

    def counting(lines: list[str]) -> list[str]:
        nonlocal calls
        calls += 1
        return original(lines)

    frame_mod.apply_corner_glow = counting
    try:
        compose_base(Text("hdr"), _Body(), "Esc back", cols, rows)
    finally:
        frame_mod.apply_corner_glow = original
    return calls


def test_glow_runs_on_regular() -> None:
    """The desktop frame keeps its lit corners."""
    assert _glow_calls() == 1


def test_glow_is_skipped_where_effects_are_off() -> None:
    """The pass only recolours truecolor foregrounds, so on PicoCalc it is pure scan cost."""
    set_platform(PICOCALC)
    assert _glow_calls() == 0


def test_wide_glyph_scan_short_circuits_without_emoji() -> None:
    """A no-emoji console font has no wide glyph, so the per-frame scan can be skipped."""
    waving = "hi \U0001f44b"
    assert _has_wide_glyph(waving) is True
    set_platform(PICOCALC)
    assert _has_wide_glyph(waving) is False


def test_spinner_cycle_follows_the_platform() -> None:
    """Braille where effects are on, the plain LINE cycle where they aren't."""
    assert Spinner().frames == Spinner.BRAILLE
    set_platform(PICOCALC)
    assert Spinner().frames == Spinner.LINE


def test_an_explicit_spinner_cycle_still_wins() -> None:
    """The platform supplies a *default*; a caller that names its frames keeps them."""
    set_platform(PICOCALC)
    assert Spinner("ab").frames == "ab"


def test_battery_gauge_holds_still_where_effects_are_off() -> None:
    """No charging sweep and no low-battery blink: the gauge draws its resting frame."""
    from meshterm.services.battery_service import BatteryReading
    from meshterm.ui.menu import _battery_segment

    class _Ctx:
        class battery:  # noqa: N801 - a stand-in for the service, not a real class name
            @staticmethod
            def reading() -> BatteryReading:
                return BatteryReading(millivolts=3300, percent=5, charging=True)

    ctx = _Ctx()
    set_platform(PICOCALC)
    # Frame 0 of a charging sweep is the empty cell; any later frame differs. Two calls at
    # different wall-clock instants must agree, which they only can if the clock isn't read.
    first = _battery_segment(ctx).plain  # type: ignore[arg-type]
    second = _battery_segment(ctx).plain  # type: ignore[arg-type]
    assert first == second
    set_platform(REGULAR)
    assert _battery_segment(ctx).plain.endswith("5%")  # type: ignore[arg-type]


def test_idle_and_spin_cadences_are_slower_where_a_frame_is_dear() -> None:
    """PicoCalc composes a frame in ~74 ms, so both animation clocks have to give.

    At the desktop's rates a 1 Hz idle tick alone would spend 7-11% of the core doing
    nothing, and a 0.12 s spinner would ask for more frames per second than the hardware
    can physically compose. Neither is a free-floating preference: both must stay strictly
    slower than regular's.
    """
    assert REGULAR.tick_s == 1.0
    assert REGULAR.spinner_tick_s == 0.12
    assert PICOCALC.tick_s > REGULAR.tick_s
    # A frame costs ~110 ms at the taller 53x40 geometry; a spin cadence under that would
    # queue repaints faster than they can finish.
    assert PICOCALC.spinner_tick_s > 0.12


def test_spinner_interval_reads_the_active_platform() -> None:
    """One source for the spin cadence, re-read per tick so set_platform is never missed."""
    from meshterm.ui.tui.spinner import spinner_interval

    assert spinner_interval() == REGULAR.spinner_tick_s
    set_platform(PICOCALC)
    assert spinner_interval() == PICOCALC.spinner_tick_s
