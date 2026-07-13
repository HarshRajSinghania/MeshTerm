"""Session width tests: reclaiming the terminal's final column.

Some terminals (and prompt_toolkit's size probe on them) report the window one column
narrower than it really is, so the frame's right border lands one short and the true last
column sits unused. The session can report one extra column to close that gap. The wrapping
output is pure and the resolver is a small branch, so both are assertable without a terminal.
"""

from __future__ import annotations

from types import SimpleNamespace

from prompt_toolkit.data_structures import Size

import meshterm.ui.tui.session as session_mod
from meshterm.ui.tui.session import TuiSession, _WidthExtendedOutput


def test_width_extended_output_reports_one_more_column() -> None:
    """get_size() gains a column; every other attribute forwards to the wrapped output."""
    inner = SimpleNamespace(
        get_size=lambda: Size(rows=24, columns=80),
        write=lambda s: f"wrote:{s}",
        encoding="utf-8",
    )
    out = _WidthExtendedOutput(inner)
    assert out.get_size() == Size(rows=24, columns=81)  # the reclaimed column
    assert out.write("x") == "wrote:x"  # forwarded method
    assert out.encoding == "utf-8"      # forwarded attribute


def test_session_leaves_a_supplied_output_untouched() -> None:
    """A test-supplied output is never wrapped — headless sizes stay exactly as set."""
    dummy = SimpleNamespace(get_size=lambda: Size(rows=10, columns=40))
    session = TuiSession(output=dummy)
    assert session._resolve_output() is dummy


def test_session_wraps_the_real_terminal_when_enabled(monkeypatch) -> None:
    """No supplied output + gate on → the real terminal output is width-extended."""
    fake = SimpleNamespace(get_size=lambda: Size(rows=30, columns=100))
    monkeypatch.setattr("prompt_toolkit.output.defaults.create_output", lambda: fake)
    monkeypatch.setattr(session_mod, "_RECLAIM_LAST_COLUMN", True)
    out = TuiSession()._resolve_output()
    assert isinstance(out, _WidthExtendedOutput)
    assert out.get_size() == Size(rows=30, columns=101)


def test_session_gate_off_uses_the_bare_terminal(monkeypatch) -> None:
    """Gate off → the real terminal output is used untouched (pt builds its own)."""
    monkeypatch.setattr(session_mod, "_RECLAIM_LAST_COLUMN", False)
    assert TuiSession()._resolve_output() is None
