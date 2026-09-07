"""Tests for the platform seam.

Resolution order, the active-platform singleton, and the two platform specs' own
sanity — PicoCalc being strictly the narrower, plainer flavour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import meshterm.platforms as platforms_mod
from meshterm.platforms import PICOCALC, REGULAR, get_platform, resolve, set_platform


def test_regular_is_todays_behaviour() -> None:
    """REGULAR is the desktop terminal exactly as it behaves without this seam."""
    assert REGULAR.name == "regular"
    assert REGULAR.width_reclaim is True
    assert REGULAR.emoji is True
    assert REGULAR.truecolor is True
    assert REGULAR.frame_border is True
    assert REGULAR.readable_cols == 72


def test_picocalc_is_the_narrower_plainer_flavour() -> None:
    """PICOCALC never claims a capability the console can't back up."""
    assert PICOCALC.name == "picocalc"
    assert PICOCALC.readable_cols < REGULAR.readable_cols
    assert PICOCALC.width_reclaim is False  # a phantom column would tear the exact-width frame
    assert PICOCALC.emoji is False
    assert PICOCALC.truecolor is False


def test_get_platform_defaults_to_regular() -> None:
    """Every test starts on REGULAR (see the autouse fixture in conftest.py)."""
    assert get_platform() is REGULAR


def test_set_platform_is_visible_through_get_platform() -> None:
    """set_platform takes effect immediately for any caller reading via get_platform()."""
    set_platform(PICOCALC)
    assert get_platform() is PICOCALC
    set_platform(REGULAR)
    assert get_platform() is REGULAR


def test_set_platform_reaches_a_dotted_import_too() -> None:
    """A consumer that imports the module (not the name) sees the change immediately.

    This is the exact footgun the module docstring warns about: ``from meshterm.platforms
    import PLATFORM`` at a module's top level would NOT see this, but module-qualified
    access (``platforms_mod.PLATFORM``) — and every real binding point in this codebase,
    which reads through :func:`get_platform` — does.
    """
    set_platform(PICOCALC)
    assert platforms_mod.PLATFORM is PICOCALC


def test_resolve_defaults_to_regular_off_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """No flag, no env, no device tree (every desktop/CI machine) → REGULAR, source 'default'."""
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)
    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", Path("/nonexistent/model"))
    r = resolve()
    assert r.platform is REGULAR
    assert r.source == "default"
    assert r.flag is None and r.env is None and r.detected_model is None


def test_resolve_flag_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit --platform overrides both the env var and auto-detection."""
    monkeypatch.setenv("MESHTERM_PLATFORM", "picocalc")
    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", Path("/nonexistent/model"))
    r = resolve("regular")
    assert r.platform is REGULAR
    assert r.source == "--platform flag"
    assert r.env == "picocalc"  # recorded even though it didn't decide the outcome


def test_resolve_env_wins_when_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """MESHTERM_PLATFORM decides when --platform wasn't passed."""
    monkeypatch.setenv("MESHTERM_PLATFORM", "picocalc")
    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", Path("/nonexistent/model"))
    r = resolve(None)
    assert r.platform is PICOCALC
    assert r.source == "MESHTERM_PLATFORM env var"


def test_resolve_matching_is_case_insensitive_and_trims_space() -> None:
    """A flag/env value matches regardless of case or stray whitespace."""
    assert resolve(" PicoCalc ").platform is PICOCALC
    assert resolve("REGULAR").platform is REGULAR


def test_resolve_unknown_flag_raises_with_the_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd --platform is a clear ValueError, not a silent fallback to REGULAR."""
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)
    with pytest.raises(ValueError) as exc:
        resolve("laptop")
    message = str(exc.value)
    assert "laptop" in message and "--platform" in message
    assert "picocalc" in message and "regular" in message


def test_resolve_unknown_env_raises_only_when_it_would_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad env var is only an error when nothing else (the flag) overrides it."""
    monkeypatch.setenv("MESHTERM_PLATFORM", "laptop")
    # A valid flag wins outright — the bad env var is recorded but never validated.
    assert resolve("regular").platform is REGULAR
    with pytest.raises(ValueError) as exc:
        resolve(None)
    assert "MESHTERM_PLATFORM" in str(exc.value)


def test_resolve_auto_detects_the_lyra_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact on-device string (P0, 2026-08-01) — trailing space and NUL included."""
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)

    class _FakePath:
        def read_bytes(self) -> bytes:
            return b"Luckfox Lyra \x00"

    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", _FakePath())
    r = resolve()
    assert r.platform is PICOCALC
    assert r.source == "auto-detect"
    assert r.detected_model == "Luckfox Lyra"  # trailing space/NUL stripped


def test_resolve_auto_detect_is_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device tree that exists but names different hardware never guesses picocalc."""
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)

    class _FakePath:
        def read_bytes(self) -> bytes:
            return b"Raspberry Pi 4 Model B\x00"

    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", _FakePath())
    r = resolve()
    assert r.platform is REGULAR
    assert r.detected_model == "Raspberry Pi 4 Model B"
