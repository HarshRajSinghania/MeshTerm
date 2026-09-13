# SPDX-License-Identifier: Apache-2.0
r"""Installing and selecting a console font on Windows, for one user, without admin.

The classic Windows console does no font fallback: a character its configured font lacks
is drawn as a box, and nothing rescues it (see :func:`meshterm.ui.termfont.emoji_support`
for the sibling problem). Its default font is Consolas, which carries 57 of the 122
non-ASCII characters MeshTerm draws and *none* of the 44 braille cells the charts are
made of. Windows PowerShell's classic console is worse still — its default, Lucida
Console, drops even ``●``.

So on that host MeshTerm offers to fix the font. Two Win32 facts make that possible
without an installer and without administrator rights:

* A font installs **for one user** by copying it under ``%LOCALAPPDATA%\Microsoft\
  Windows\Fonts``, registering it under ``HKCU``, and telling the session about it with
  ``AddFontResourceW`` plus a ``WM_FONTCHANGE`` broadcast. No elevation anywhere.
* A console selects a font with ``SetCurrentConsoleFontEx``, which — verified on
  Windows 10 22H2 — accepts a face the console's own properties dialog will not even
  list. That dialog only offers what is registered under the machine-wide
  ``Console\TrueTypeFont`` key (Lucida Console and Consolas here), so without this API
  the user could install the font and still be unable to pick it.

**A font install is one-way inside a session.** Once loaded, Windows locks the file: it
cannot be deleted even by an elevated process, and even after the font cache service is
restarted — it frees at the next logon. So nothing here offers to uninstall one, and the
install is written to be safe to repeat rather than reversible.

Everything is best-effort. A failure at any step leaves the app running with the font it
already had, which is the state it would have been in anyway.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import win32dll

#: Where the bundled font lives, found the way every other asset is (see
#: :mod:`meshterm.ui.about`) so it keeps resolving inside a PyInstaller bundle.
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

#: The font MeshTerm ships and offers to install. Cascadia Mono PL is Microsoft's own
#: console font, SIL OFL (licence beside it), and the ``PL`` build adds the powerline
#: separators the path lines are drawn with — 723KB for 109 of our 122 characters, all 44
#: braille cells and all 3 powerline glyphs, where the 2.4MB Nerd Font build covers no
#: more. Practically every mainstream coder font — Hack, JetBrains Mono, Fira Code, Source
#: Code Pro, even DejaVu Sans *Mono* — carries no braille at all; Cascadia and Iosevka are
#: the exceptions, which is why this is not a matter of taste.
BUNDLED_FONT = FONT_DIR / "CascadiaMonoPL.ttf"

#: Its family name, as the ``name`` table spells it — what ``SetCurrentConsoleFontEx``
#: and the ``HKCU`` registration must both be given, exactly.
BUNDLED_FACE = "Cascadia Mono PL"

_USER_FONTS = "Microsoft/Windows/Fonts"
_FONT_REGISTRY = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"

_HWND_BROADCAST = 0xFFFF
_WM_FONTCHANGE = 0x001D
_SMTO_ABORTIFHUNG = 0x0002

#: ``FF_MODERN | TMPF_VECTOR | TMPF_TRUETYPE`` — what a console expects a TrueType face to
#: declare. Passing 0 here makes the call succeed and quietly keep the old raster font.
_FF_MODERN_TRUETYPE = 54


def user_font_dir() -> Path:
    r"""The per-user font folder — writable without admin, unlike ``C:\Windows\Fonts``."""
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / _USER_FONTS


def install_bundled_font() -> bool:
    """Install the bundled font for this user, and make the session aware of it.

    Safe to repeat: an already-installed copy is left alone rather than overwritten, both
    because the file is locked once loaded and because there is nothing to gain.

    Returns:
        ``True`` when the font is installed and usable (including when it already was).
    """
    if sys.platform != "win32" or not BUNDLED_FONT.is_file():
        return False
    try:
        import ctypes
        import winreg

        target = user_font_dir() / BUNDLED_FONT.name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BUNDLED_FONT, target)

        if not _add_font_resource(target):
            return False

        # The registration is what survives a logout; AddFontResourceW alone lasts only
        # as long as this session. Per-user entries hold the full path, machine-wide ones
        # hold a bare filename — this is the per-user key, so the path goes in.
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _FONT_REGISTRY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, f"{BUNDLED_FACE} (TrueType)", 0, winreg.REG_SZ, str(target))

        # Tell everything already running that the font list changed. Timed out and
        # allowed to abort on a hung window, because a broadcast that waits on every top
        # level window is a good way to stall a startup path.
        user32 = win32dll.user32()
        user32.SendMessageTimeoutW(
            _HWND_BROADCAST,
            _WM_FONTCHANGE,
            0,
            0,
            _SMTO_ABORTIFHUNG,
            1000,
            ctypes.byref(ctypes.c_ulong()),
        )
        return True
    except Exception:  # pragma: no cover - an install that fails leaves the old font
        return False


def _add_font_resource(path: Path) -> bool:
    """Make a font file usable by this process, without installing anything.

    ``AddFontResourceW`` adds to the font table for the running process; the ``HKCU``
    registration beside it is what makes the font permanent, and Windows reads that at the
    *next logon*. Between those two facts sits the case this exists for: a second MeshTerm
    launch in the same session finds its own registration and believes the font is ready,
    when nothing has loaded it into this new process yet.

    Args:
        path: The font file, wherever it is.

    Returns:
        Whether at least one face was added.
    """
    if sys.platform != "win32" or not path.is_file():
        return False
    try:
        import ctypes

        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        return bool(gdi32.AddFontResourceW(ctypes.c_wchar_p(str(path))))
    except Exception:  # pragma: no cover - best effort, like everything else here
        return False


def use(face: str) -> bool:
    """Draw this console with ``face``, loading our own copy first if that is what it is.

    The plain :func:`select` is enough for a font the system installed — the Cascadia that
    comes with Windows 11 or with Windows Terminal. It is *not* enough for the copy
    MeshTerm installed itself in an earlier run of the same session: that one is registered
    but not yet loaded (see :func:`_add_font_resource`), and selecting it silently keeps
    the old font. So a refusal is retried once, after loading it.

    Args:
        face: The family to draw with.

    Returns:
        Whether the console is now drawing with ``face``.
    """
    if select(face):
        return True
    if face == BUNDLED_FACE and _add_font_resource(user_font_dir() / BUNDLED_FONT.name):
        return select(face)
    return False


def current_face() -> str | None:
    """The face this console is drawing with, or ``None`` off a console."""
    return _console_font(None)


def select(face: str) -> bool:
    """Point this console at ``face``, and confirm it took.

    The call reports success even where the console quietly kept the font it had, so the
    answer here is a read-back rather than the return value.

    Args:
        face: The family name, spelled as the font's own ``name`` table spells it.

    Returns:
        Whether the console is now drawing with ``face``.
    """
    return _console_font(face) == face


def _console_font(face: str | None) -> str | None:
    """Read the console's font, first setting it to ``face`` when one is given.

    One function for both because the struct, the handle and the failure modes are the
    same, and because setting without reading back proves nothing.

    Args:
        face: The family to select, or ``None`` to only read.

    Returns:
        The face the console is drawing with afterwards, or ``None`` if it could not
        be asked (no console, not Windows, a failed call).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.ULONG),
                ("nFont", wintypes.DWORD),
                ("dwFontSize", _COORD),
                ("FontFamily", wintypes.UINT),
                ("FontWeight", wintypes.UINT),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        kernel32 = win32dll.kernel32()
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        for name in ("GetCurrentConsoleFontEx", "SetCurrentConsoleFontEx"):
            fn = getattr(kernel32, name)
            fn.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(_CONSOLE_FONT_INFOEX)]
            fn.restype = wintypes.BOOL

        handle = kernel32.GetStdHandle(wintypes.DWORD(-11).value)  # STD_OUTPUT_HANDLE
        info = _CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            return None

        if face is not None:
            # Keep the height the reader already chose and let the width follow the face;
            # a console font is picked for legibility at a size, and changing that out
            # from under someone is not what they agreed to.
            info.FaceName = face[:31]
            info.FontFamily = _FF_MODERN_TRUETYPE
            info.dwFontSize = _COORD(0, info.dwFontSize.Y or 16)
            info.FontWeight = 400
            kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info))
            after = _CONSOLE_FONT_INFOEX()
            after.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
            if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(after)):
                return None
            return after.FaceName or None
        return info.FaceName or None
    except Exception:  # pragma: no cover - a probe that fails is an unknown, not a crash
        return None
