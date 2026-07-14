"""Tests for application settings loading."""

from __future__ import annotations

from pathlib import Path

from meshterm.core.config import DIRECT_MESSAGE_MAX_SOFT_RETRIES, Settings


def test_connect_on_start_defaults_true() -> None:
    """Eager connect-at-launch is the default when nothing is configured."""
    assert Settings().connect_on_start is True


def test_connect_on_start_loads_from_toml(tmp_path: Path) -> None:
    """The connect_on_start preference round-trips from the config file."""
    config = tmp_path / "config.toml"
    config.write_text("connect_on_start = false\n", encoding="utf-8")
    assert Settings.load(config).connect_on_start is False


def test_direct_message_soft_retries_defaults_to_two() -> None:
    """Two soft retries (three tries total) is the out-of-the-box direct-message budget."""
    assert Settings().direct_message_soft_retries == 2
    assert DIRECT_MESSAGE_MAX_SOFT_RETRIES == 2


def test_direct_message_soft_retries_loads_from_toml(tmp_path: Path) -> None:
    """The soft-retry budget round-trips from the config file."""
    config = tmp_path / "config.toml"
    config.write_text("direct_message_soft_retries = 1\n", encoding="utf-8")
    assert Settings.load(config).direct_message_soft_retries == 1


def test_direct_message_soft_retries_clamped_to_range(tmp_path: Path) -> None:
    """Out-of-range values are clamped to 0..2 so a DM never floods the mesh (or is dropped)."""
    hot = tmp_path / "hot.toml"
    hot.write_text("direct_message_soft_retries = 9\n", encoding="utf-8")
    assert Settings.load(hot).direct_message_soft_retries == DIRECT_MESSAGE_MAX_SOFT_RETRIES

    cold = tmp_path / "cold.toml"
    cold.write_text("direct_message_soft_retries = -4\n", encoding="utf-8")
    assert Settings.load(cold).direct_message_soft_retries == 0


def test_tcp_profile_inferred_and_loaded(tmp_path: Path) -> None:
    """A profile with a ``host`` loads as a TCP profile and exposes its host:port endpoint."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[profiles.wifi]\n"
        'host = "192.168.1.50"\n'
        "tcp_port = 6000\n",
        encoding="utf-8",
    )
    profile = Settings.load(config).profiles["wifi"]
    assert profile.is_tcp and profile.transport == "tcp"
    assert profile.host == "192.168.1.50" and profile.tcp_port == 6000
    assert profile.tcp_endpoint == "192.168.1.50:6000"


def test_tcp_profile_defaults_port(tmp_path: Path) -> None:
    """A TCP profile naming only a host takes the default port in its endpoint."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[profiles.wifi]\nhost = "meshcore.local"\n', encoding="utf-8"
    )
    profile = Settings.load(config).profiles["wifi"]
    assert profile.is_tcp and profile.tcp_endpoint == "meshcore.local:5000"
