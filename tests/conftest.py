"""Shared fixtures: pin path rendering to plain arrows for every test.

The powerline verdict is environmental — the suite may run inside VS Code or Windows
Terminal (both chip-capable) or a bare CI shell (not) — and screen assertions must
not change with the developer's glass. ``MESHTERM_POWERLINE=0`` is the supported pin;
the cached verdict is cleared around each test so no ordering leaks it. Powerline-
specific tests pass explicit modes or monkeypatch the widget's own switch.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from meshterm.ui.termfont import powerline_support


@pytest.fixture(autouse=True)
def _plain_path_rendering(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MESHTERM_POWERLINE", "0")
    powerline_support.cache_clear()
    yield
    powerline_support.cache_clear()
