"""Tests for machine-setup loading (where things live, and which device to talk to).

MeshTerm's *behaviour* settings are preferences and live in ``tests/test_preferences.py``;
this file is only what ``config.toml`` still answers for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshterm.core.config import CONFIG_DIR_ENV, Settings, default_config_dir


def test_connect_on_start_defaults_true() -> None:
    """Eager connect-at-launch is the default when nothing is configured."""
    assert Settings().connect_on_start is True


def test_connect_on_start_loads_from_toml(tmp_path: Path) -> None:
    """Whether the session opens the radio link at launch round-trips from the config file."""
    config = tmp_path / "config.toml"
    config.write_text("connect_on_start = false\n", encoding="utf-8")
    assert Settings.load(config).connect_on_start is False


def test_tcp_profile_inferred_and_loaded(tmp_path: Path) -> None:
    """A profile with a ``host`` loads as a TCP profile and exposes its host:port endpoint."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[profiles.wifi]\nhost = "192.168.1.50"\ntcp_port = 6000\n',
        encoding="utf-8",
    )
    profile = Settings.load(config).profiles["wifi"]
    assert profile.is_tcp and profile.transport == "tcp"
    assert profile.host == "192.168.1.50" and profile.tcp_port == 6000
    assert profile.tcp_endpoint == "192.168.1.50:6000"


def test_tcp_profile_defaults_port(tmp_path: Path) -> None:
    """A TCP profile naming only a host takes the default port in its endpoint."""
    config = tmp_path / "config.toml"
    config.write_text('[profiles.wifi]\nhost = "meshcore.local"\n', encoding="utf-8")
    profile = Settings.load(config).profiles["wifi"]
    assert profile.is_tcp and profile.tcp_endpoint == "meshcore.local:5000"


def test_config_dir_defaults_to_the_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override set, config and data live in ``.meshterm`` under the user's home."""
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    assert default_config_dir() == Path.home() / ".meshterm"


def test_config_dir_follows_the_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``$MESHTERM_HOME`` moves the whole directory, so a build can run beside a real one.

    This is what keeps a downloaded binary from opening the same database as the checkout
    it was built from — the history is the valuable half of an install, and there was no
    way to point a second copy somewhere else.
    """
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "elsewhere"))
    assert default_config_dir() == tmp_path / "elsewhere"


def test_config_dir_ignores_an_empty_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset-but-present or whitespace value falls back rather than resolving to nowhere.

    A shell that exports the variable empty is common enough that treating "" as "put the
    data in the current directory" would be a nasty way to scatter someone's history.
    """
    monkeypatch.setenv(CONFIG_DIR_ENV, "   ")
    assert default_config_dir() == Path.home() / ".meshterm"
