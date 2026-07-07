"""Tests for application settings loading."""

from __future__ import annotations

from pathlib import Path

from meshterm.core.config import Settings


def test_connect_on_start_defaults_true() -> None:
    """Eager connect-at-launch is the default when nothing is configured."""
    assert Settings().connect_on_start is True


def test_connect_on_start_loads_from_toml(tmp_path: Path) -> None:
    """The connect_on_start preference round-trips from the config file."""
    config = tmp_path / "config.toml"
    config.write_text("connect_on_start = false\n", encoding="utf-8")
    assert Settings.load(config).connect_on_start is False
