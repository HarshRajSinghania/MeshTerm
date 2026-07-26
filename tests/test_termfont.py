"""Terminal-font detection tests: the recommended list, the ladder, the verdict.

Everything runs against injected environments and temp settings files — no test
touches the real registry, the real Win32 console, or the machine's actual fonts —
so the suite decides the same verdicts on any box.
"""

from __future__ import annotations

import json
from pathlib import Path

from meshterm.ui.termfont import (
    CORE,
    FULL,
    NONE,
    UNKNOWN,
    NERD_FONT_GENERIC,
    RecommendedFont,
    _powerline_support,
    _read_jsonc,
    _vscode_default_face,
    _vscode_face,
    _windows_terminal_face,
    detect_terminal_font,
    installed_recommended,
    match_recommended,
    normalize_face,
    primary_family,
)


# --- name matching ----------------------------------------------------------------


def test_primary_family_takes_the_first_of_a_css_list() -> None:
    """VS Code stores comma lists with optional quotes; the head family is judged."""
    assert primary_family("'Hack Nerd Font Mono', monospace") == "Hack Nerd Font Mono"
    assert primary_family('"Fira Code", Consolas') == "Fira Code"
    assert primary_family("Cascadia Mono") == "Cascadia Mono"


def test_normalize_face_folds_case_quotes_and_spacing() -> None:
    """Matching is done on a lower-cased, single-spaced, unquoted form."""
    assert normalize_face("  'Hack  Nerd Font' ") == "hack nerd font"
    assert normalize_face('"MesloLGS NF"') == "meslolgs nf"


def test_match_recommended_knows_the_nerd_font_spellings() -> None:
    """The long name and the v3 short suffix both land on the same full entry."""
    assert match_recommended("Hack Nerd Font Mono").name == "Hack Nerd Font"
    assert match_recommended("Hack NFM").name == "Hack Nerd Font"
    assert match_recommended("MesloLGS NF").coverage == FULL  # powerlevel10k's install


def test_match_recommended_orders_full_patches_before_core_families() -> None:
    """``JetBrainsMono Nerd Font`` must hit its full entry, never the plain
    ``JetBrains Mono`` core prefix — first match wins, so full entries lead."""
    assert match_recommended("JetBrainsMono Nerd Font Mono").coverage == FULL
    assert match_recommended("JetBrains Mono").coverage == CORE


def test_match_recommended_core_natives_and_strangers() -> None:
    """Stock coder fonts with native triangles read core; anything else, nothing."""
    assert match_recommended("Fira Code").coverage == CORE
    assert match_recommended("Source Code Variable").coverage == CORE
    assert match_recommended("Consolas") is None
    assert match_recommended(None) is None


def test_match_recommended_generic_nerd_rule_catches_unlisted_patches() -> None:
    """Any family carrying the Nerd Font branding has the full block patched in."""
    assert match_recommended("Comic Shanns Mono Nerd Font") is NERD_FONT_GENERIC
    assert match_recommended("Terminess NF") is NERD_FONT_GENERIC


# --- settings-file reading --------------------------------------------------------


def test_read_jsonc_survives_comments_and_trailing_commas(tmp_path: Path) -> None:
    """Both WT and VS Code write JSON-with-comments; the reader shrugs it off."""
    path = tmp_path / "settings.json"
    path.write_text(
        '{\n'
        '  // the font\n'
        '  "editor.fontFamily": "Hack NFM", /* inline */\n'
        '  "url": "https://example.org//not-a-comment",\n'
        '  "list": [1, 2,],\n'
        '}\n',
        encoding="utf-8",
    )
    data = _read_jsonc(path)
    assert data == {
        "editor.fontFamily": "Hack NFM",
        "url": "https://example.org//not-a-comment",
        "list": [1, 2],
    }
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    assert _read_jsonc(broken) is None


def _wt_env(tmp_path: Path, settings: dict, guid: str = "{abc-123}") -> dict:
    """A fake Windows Terminal environment around a written settings file."""
    folder = tmp_path / "Microsoft" / "Windows Terminal"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return {"LOCALAPPDATA": str(tmp_path), "WT_SESSION": "s", "WT_PROFILE_ID": guid}


def test_windows_terminal_face_resolves_profile_then_defaults(tmp_path: Path) -> None:
    """The session profile's ``font.face`` wins; ``profiles.defaults`` backs it up."""
    env = _wt_env(tmp_path, {
        "profiles": {
            "defaults": {"font": {"face": "Cascadia Code PL"}},
            "list": [{"guid": "{ABC-123}", "font": {"face": "Hack Nerd Font Mono"}}],
        },
    })
    assert _windows_terminal_face(env) == "Hack Nerd Font Mono"  # guid case-folded
    env2 = _wt_env(tmp_path, {
        "profiles": {
            "defaults": {"font": {"face": "Cascadia Code PL"}},
            "list": [{"guid": "{other}"}],
        },
    })
    assert _windows_terminal_face(env2) == "Cascadia Code PL"


def test_windows_terminal_face_reads_legacy_and_defaults_to_cascadia(tmp_path: Path) -> None:
    """The old flat ``fontFace`` key still counts; nothing set means the built-in."""
    env = _wt_env(tmp_path, {
        "profiles": {"list": [{"guid": "{abc-123}", "fontFace": "MesloLGS NF"}]},
    })
    assert _windows_terminal_face(env) == "MesloLGS NF"
    env2 = _wt_env(tmp_path, {"profiles": {"list": [{"guid": "{abc-123}"}]}})
    assert _windows_terminal_face(env2) == "Cascadia Mono"
    assert _windows_terminal_face({"LOCALAPPDATA": str(tmp_path / "nowhere")}) \
        == "Cascadia Mono"


def test_vscode_face_precedence_terminal_over_editor_workspace_over_user(
    tmp_path: Path,
) -> None:
    """``terminal.integrated.fontFamily`` beats ``editor.fontFamily``; within a key
    the workspace file beats the user file; the value's head family is judged."""
    workspace = tmp_path / "repo"
    (workspace / ".vscode").mkdir(parents=True)
    (workspace / ".vscode" / "settings.json").write_text(
        '{"editor.fontFamily": "Consolas"}', encoding="utf-8"
    )
    appdata = tmp_path / "appdata"
    user = appdata / "Code" / "User"
    user.mkdir(parents=True)
    (user / "settings.json").write_text(
        '{"terminal.integrated.fontFamily": "\'Hack Nerd Font Mono\', monospace"}',
        encoding="utf-8",
    )
    env = {"APPDATA": str(appdata)}
    assert _vscode_face(env, workspace) == "Hack Nerd Font Mono"  # terminal.* wins
    (user / "settings.json").write_text("{}", encoding="utf-8")
    assert _vscode_face(env, workspace) == "Consolas"  # workspace editor fallback
    (workspace / ".vscode" / "settings.json").write_text("{}", encoding="utf-8")
    assert _vscode_face(env, workspace) == _vscode_default_face()


# --- the ladder -------------------------------------------------------------------


def test_detect_terminal_font_ladder(tmp_path: Path) -> None:
    """WT markers first, then VS Code, then a genuine conhost; TERM disqualifies
    the conhost probe (some other emulator is hosting the console)."""
    wt = detect_terminal_font(_wt_env(tmp_path, {"profiles": {"list": []}}))
    assert wt is not None and wt.source == "windows-terminal"
    code = detect_terminal_font(
        {"TERM_PROGRAM": "vscode", "APPDATA": str(tmp_path)}, cwd=tmp_path
    )
    assert code is not None and code.source == "vscode"
    con = detect_terminal_font({}, conhost_probe=lambda: "Consolas")
    assert con is not None and (con.face, con.source) == ("Consolas", "conhost")
    assert detect_terminal_font({"TERM": "xterm"}, conhost_probe=lambda: "Consolas") is None


def test_powerline_support_env_override_always_wins() -> None:
    """``MESHTERM_POWERLINE`` is the user's word: off, on/full, or core."""
    assert _powerline_support({"MESHTERM_POWERLINE": "0"}).level == NONE
    assert _powerline_support({"MESHTERM_POWERLINE": "off"}).level == NONE
    on = _powerline_support({"MESHTERM_POWERLINE": "1", "WT_SESSION": "s"})
    assert (on.level, on.source) == (FULL, "env")
    assert _powerline_support({"MESHTERM_POWERLINE": "core"}).level == CORE


def test_powerline_support_matched_font_sets_the_level(tmp_path: Path) -> None:
    """A recommended face read from the terminal's own config decides coverage."""
    env = _wt_env(tmp_path, {
        "profiles": {"list": [{"guid": "{abc-123}",
                               "font": {"face": "Hack Nerd Font Mono"}}]},
    })
    verdict = _powerline_support(env)
    assert verdict.level == FULL
    assert verdict.source == "font:windows-terminal"
    assert verdict.matched is not None and verdict.matched.name == "Hack Nerd Font"


def test_powerline_support_renderer_fallback_and_honest_none(tmp_path: Path) -> None:
    """An unmatched face on WT still earns core (the bundled-symbol fallback); the
    same face on a bare conhost is an honest ``none``."""
    env = _wt_env(tmp_path, {"profiles": {"list": [{"guid": "{abc-123}"}]}})
    wt = _powerline_support(env)  # face resolves to Cascadia Mono → no match
    assert (wt.level, wt.source) == (CORE, "renderer:windows-terminal")
    con = _powerline_support({}, conhost_probe=lambda: "Consolas")
    assert (con.level, con.source, con.face) == (NONE, "font:conhost", "Consolas")


def test_powerline_support_kitty_ssh_and_unknown() -> None:
    """Glyph-drawing terminals earn core by marker; ssh and strangers stay unknown
    (the font lives on glass we cannot see)."""
    kitty = _powerline_support({"TERM": "xterm-kitty"})
    assert (kitty.level, kitty.source) == (CORE, "renderer:kitty")
    ssh = _powerline_support({"TERM": "xterm", "SSH_CONNECTION": "1.2.3.4"})
    assert (ssh.level, ssh.source) == (UNKNOWN, "ssh")
    lost = _powerline_support({"TERM": "xterm-256color"})
    assert (lost.level, lost.source) == (UNKNOWN, "unknown")


def test_installed_recommended_never_raises() -> None:
    """The machine scan is best-effort context for a future nudge screen — whatever
    the platform answers, it is a RecommendedFont or None, never an exception."""
    result = installed_recommended()
    assert result is None or isinstance(result, RecommendedFont)
