"""Run the real MeshTerm with per-keystroke stage timing, logged to a CSV.

Wraps the stages a keystroke passes through and appends one row per repaint to
$MESHTERM_TRACE (default $HOME/tmp/keytrace.csv):

    t, screen, action, dispatch_ms, handle_ms, header_ms, compose_ms, dialog_ms,
    parse_ms, render_ms, flush_ms, total_ms, out_bytes, rta_calls, rta_ms

``render_ms`` is prompt_toolkit's whole render() — it contains compose/header/parse,
since those run inside it from the layout callbacks. ``total_ms`` is dispatch start to
the end of the render that followed it: the app-side cost of the keystroke.

Usage: python instrument.py [meshterm args...]
"""

from __future__ import annotations

import os
import sys
import time

TRACE = os.environ.get("MESHTERM_TRACE", "$HOME/tmp/keytrace.csv")

_acc: dict[str, float] = {}
_rows: list[str] = []
_state = {"t_dispatch": None, "action": "", "screen": ""}


def _add(key, dt):
    _acc[key] = _acc.get(key, 0.0) + dt


def install():
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.renderer import Renderer

    from meshterm.ui.tui import frame as frame_mod
    from meshterm.ui.tui import render as render_mod
    from meshterm.ui.tui.session import TuiSession

    # --- MeshTerm's own compose stages -------------------------------------------
    orig_compose_base = frame_mod.compose_base
    orig_compose_dialog = frame_mod.compose_dialog
    orig_startup = frame_mod.compose_startup

    def compose_base(header, base, *a, **k):
        t = time.perf_counter()
        try:
            return orig_compose_base(header, base, *a, **k)
        finally:
            _add("compose", time.perf_counter() - t)

    def compose_dialog(*a, **k):
        t = time.perf_counter()
        try:
            return orig_compose_dialog(*a, **k)
        finally:
            _add("dialog", time.perf_counter() - t)

    def compose_startup(*a, **k):
        t = time.perf_counter()
        try:
            return orig_startup(*a, **k)
        finally:
            _add("compose", time.perf_counter() - t)

    frame_mod.compose_base = compose_base
    frame_mod.compose_dialog = compose_dialog
    frame_mod.compose_startup = compose_startup

    # --- the rasterizer, wherever it was imported --------------------------------
    orig_rta = render_mod.render_to_ansi

    def render_to_ansi(renderable, width, *, no_wrap=False):
        t = time.perf_counter()
        try:
            return orig_rta(renderable, width, no_wrap=no_wrap)
        finally:
            _add("rta", time.perf_counter() - t)
            _acc["rta_n"] = _acc.get("rta_n", 0) + 1

    import importlib
    import pkgutil

    import meshterm

    for mod in pkgutil.walk_packages(meshterm.__path__, "meshterm."):
        try:
            importlib.import_module(mod.name)
        except Exception:  # noqa: BLE001
            pass
    for module in list(sys.modules.values()):
        if module is None or not getattr(module, "__name__", "").startswith("meshterm"):
            continue
        if getattr(module, "render_to_ansi", None) is orig_rta:
            module.render_to_ansi = render_to_ansi
    render_mod.render_to_ansi = render_to_ansi

    # --- the session's header callable -------------------------------------------
    orig_init = TuiSession.__init__

    def init(self, header=None, **k):
        if header is not None:
            inner = header

            def timed(cols):
                t = time.perf_counter()
                try:
                    return inner(cols)
                finally:
                    _add("header", time.perf_counter() - t)

            header = timed
        return orig_init(self, header, **k)

    TuiSession.__init__ = init

    # --- input dispatch ----------------------------------------------------------
    orig_dispatch = TuiSession._dispatch

    def dispatch(self, action, data=""):
        _acc.clear()
        _state["t_dispatch"] = time.perf_counter()
        _state["action"] = action if action != "text" else f"text:{data}"
        top = self.top
        _state["screen"] = type(top).__name__ if top is not None else "-"
        t = time.perf_counter()
        try:
            return orig_dispatch(self, action, data)
        finally:
            _add("handle", time.perf_counter() - t)

    TuiSession._dispatch = dispatch

    # --- pt's ANSI parse ---------------------------------------------------------
    orig_ansi = ANSI.__init__

    def ansi_init(self, value):
        t = time.perf_counter()
        try:
            return orig_ansi(self, value)
        finally:
            _add("parse", time.perf_counter() - t)

    ANSI.__init__ = ansi_init

    # --- pt's render + the terminal write ----------------------------------------
    orig_render = Renderer.render

    def render(self, app, layout, is_done=False):
        t = time.perf_counter()
        try:
            return orig_render(self, app, layout, is_done)
        finally:
            try:
                _log_render(time.perf_counter() - t)
            except Exception:  # noqa: BLE001 - never let the meter break the app
                import traceback

                with open(TRACE + ".log", "a") as fh:
                    fh.write(traceback.format_exc() + "\n")

    def _log_render(dt):
        ms = lambda k: _acc.get(k, 0.0) * 1000  # noqa: E731
        t_disp = _state["t_dispatch"]
        if t_disp is None:
            # An idle-tick repaint, not a keystroke — still worth a row: the tick's cost
            # is what the app burns while you are doing nothing at all.
            action, handle, total = "~tick", 0.0, dt * 1000
        else:
            action = _state["action"]
            handle = ms("handle")
            total = (time.perf_counter() - t_disp) * 1000
            _state["t_dispatch"] = None
        _rows.append(
            "%.3f,%s,%s,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%.2f,%d,%.2f"
            % (
                time.time(), _state["screen"] or "-", action,
                handle, ms("header"), ms("compose"), ms("dialog"),
                ms("parse"), dt * 1000, total,
                _acc.get("rta_n", 0), ms("rta"),
            )
        )
        _acc.clear()
        _flush()

    Renderer.render = render


def _flush():
    if not _rows:
        return
    new = not os.path.exists(TRACE)
    with open(TRACE, "a") as fh:
        if new:
            fh.write("t,screen,action,handle_ms,header_ms,compose_ms,dialog_ms,"
                     "parse_ms,render_ms,total_ms,rta_calls,rta_ms\n")
        fh.write("\n".join(_rows) + "\n")
    _rows.clear()


def main():
    with open(TRACE + ".log", "a") as fh:
        fh.write("install start %.3f argv=%r\n" % (time.time(), sys.argv))
    install()
    with open(TRACE + ".log", "a") as fh:
        fh.write("install done %.3f\n" % time.time())
    import atexit

    atexit.register(_flush)
    from meshterm.cli import main as cli_main

    sys.argv = ["meshterm"] + sys.argv[1:]
    try:
        cli_main()
    finally:
        _flush()


if __name__ == "__main__":
    main()
