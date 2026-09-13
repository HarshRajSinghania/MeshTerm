# SPDX-License-Identifier: Apache-2.0
# PyInstaller spec for the downloadable builds. Run from the repository root:
#
#     pyinstaller packaging/meshterm.spec
#
# One file, console mode. Console is not a detail: MeshTerm *is* a terminal program, and
# a windowed build would start with nowhere to draw and exit immediately.
#
# Built on the machine it targets. There is no cross-compiling here — the Windows build
# comes off a Windows runner, the macOS one off macOS. That is what the workflow is for.

from PyInstaller.utils.hooks import collect_submodules

# The assets have to land at `meshterm/assets`, because `ui/about.py` finds them with
# `Path(__file__).parent.parent / "assets"` and that path has to keep resolving inside
# the bundle exactly as it does in a checkout.
datas = [
    ("../meshterm/assets", "meshterm/assets"),
]

hiddenimports = [
    # Every tool module. `meshterm.tools` finds these with `pkgutil.iter_modules` at
    # import time, so nothing static ever names one, and a frozen build without this line
    # starts with an empty registry: no menu entries but Quit, and no CLI subcommands.
    # The app launches and looks like it works, which is the worst way for this to fail.
    *collect_submodules("meshterm.tools"),
    # bleak picks its backend at runtime by platform, so static analysis never sees the
    # one that actually gets used. Collect all of them and let the unused ones sit.
    *collect_submodules("bleak.backends"),
    # Same story for the companion library's transports.
    *collect_submodules("meshcore"),
    # Rich and prompt_toolkit both reach for things dynamically.
    "rich.console",
    "prompt_toolkit.output.win32",
    "prompt_toolkit.output.vt100",
]

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here is used, and each one drags in a GUI toolkit or a test framework.
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "numpy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="meshterm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off deliberately: it saves a few megabytes and gets the result flagged by
    # antivirus often enough that it is not a trade worth making for a first release.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
