# SPDX-License-Identifier: Apache-2.0
"""Write an already-composed frame straight to the terminal, one changed row at a time.

MeshTerm composes every frame itself: :func:`~meshterm.ui.tui.frame.compose_base` returns
a finished ANSI string, exactly one line per terminal row, already sliced to the viewport
and already folded to the platform's glyph set. prompt_toolkit's own renderer then takes
that string and does a full round trip with it — parses the ANSI back into style/text
fragments, writes every one of the ~1400 cells into a fresh grid of ``Char`` objects,
diffs that grid against the previous frame's grid, and re-emits ANSI for the cells that
moved.

That round trip is the single largest cost in a keystroke on the PicoCalc, and it is paid
in full on *every* paint — a measured 70-125 ms whether the frame changed by one row, by
all of them, or not at all (the 2 s header tick recomposes an identical frame and still
pays it). Since we already know the frame as rows of text, the same pixels can be reached
by comparing this frame's rows against the last frame's rows and rewriting only the ones
that differ: ~0.1 ms, and a smaller write besides.

The bypass is deliberately narrow. It runs only when the frame is a plain full-screen
composition — one base screen, no floating dialog and no busy overlay — because those are
laid out by prompt_toolkit's float containers rather than by us, and compositing them here
would mean re-implementing that placement. Anything else falls straight through to the
stock renderer, which stays the authority: :meth:`FastRenderer.render` is an accelerator
for the common case, never a second implementation of the whole layout engine.

Three details make the row diff safe rather than merely fast:

* **A row is rewritten whole, from column 0.** Stepping down to a row returns the terminal
  to a true column 0 whatever happened on the row above, so a glyph drawn at a width we did
  not measure can never throw off the rows below it — the same guarantee
  :meth:`~meshterm.ui.tui.session.TuiSession._emit` relies on, reached here for free rather
  than by a scrub pass.
* **Every column of that row is pinned to the grid the app measured**
  (:func:`~meshterm.ui.tui.colsnap.snap_row`), which is what keeps the drift from mattering
  *within* the row either. This is the only place in the app where a row's bytes are still
  ours on their way to the terminal, so it is the only place that correction can be made.
* **Each row is cleared before it is drawn, never after**, so no style leaks in from the row
  above, a row that got shorter leaves no tail behind, and a row that fills the terminal
  exactly keeps its last cell.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from prompt_toolkit.renderer import Renderer

from . import colsnap


def _as_is(row: str) -> str:
    """The unpinned write path: a row goes out exactly as it was composed."""
    return row


def enabled() -> bool:
    """Whether the direct row writer is active.

    On by default — it is 2-3x on every navigation keystroke and its output is verified
    byte-identical against the stock renderer by reading the console back. The escape
    hatch, for a terminal that ever disagrees, is the ``fast_render`` preference;
    ``MESHTERM_FASTRENDER=0``/``=1`` overrules it for one run without editing anything.

    Read at session build, so a change made on the Preferences page takes hold at the
    next launch — which is what that preference's description promises.
    """
    from ...core.preferences import current as current_preferences

    override = os.environ.get("MESHTERM_FASTRENDER")
    if override is not None:
        return override != "0"
    return bool(current_preferences().fast_render)


class FastRenderer(Renderer):
    """A :class:`~prompt_toolkit.renderer.Renderer` that short-circuits simple frames.

    Args:
        frame_source: Returns the composed full-screen frame as one ANSI string (rows
            joined by newlines), or ``None`` when this paint is not a plain base frame
            and must go through prompt_toolkit's layout instead.
    """

    def __init__(self, *args, frame_source: Callable[[], str | None], **kwargs) -> None:
        """Wrap the stock renderer, starting with no remembered frame.

        With nothing to compare against, the first paint is always a full one; the
        ``frame_source`` argument is described in the class docstring above.
        """
        super().__init__(*args, **kwargs)
        self._frame_source = frame_source
        #: How a row is pinned to the app's column grid on its way out — resolved once here,
        #: because the escape hatch is a property of the terminal this session is talking to
        #: and not of any one paint. Identity when it is off, so the write path never branches.
        self._pin: Callable[[str], str] = colsnap.snap_row if colsnap.enabled() else _as_is
        self._prev_rows: list[str] | None = None
        self._prev_size: tuple[int, int] | None = None
        #: Paints answered here vs handed to the stock renderer — read by the bench.
        self.fast_paints = 0
        self.slow_paints = 0

    def reset(self, _scroll: bool = False, leave_alternate_screen: bool = True) -> None:
        """Forget the last frame along with prompt_toolkit's own render state."""
        self._prev_rows = None
        super().reset(_scroll=_scroll, leave_alternate_screen=leave_alternate_screen)

    def erase(self, leave_alternate_screen: bool = True) -> None:
        """The screen is being cleared out from under us — the next paint must be full."""
        self._prev_rows = None
        super().erase(leave_alternate_screen=leave_alternate_screen)

    def render(self, app, layout, is_done: bool = False) -> None:  # noqa: ANN001
        """Paint the frame, taking the row-diff path when this frame is a plain one."""
        text = None if is_done else self._frame_source()
        if text is None:
            # A dialog, the busy overlay, or the closing paint: prompt_toolkit lays those
            # out, so it must also own the diff — its grid is the only record of what is
            # on screen, and it is about to be rebuilt from scratch anyway.
            self._prev_rows = None
            self.slow_paints += 1
            super().render(app, layout, is_done)
            return

        output = self.output
        pending = False  # whether the terminal setup below has queued anything to flush
        if self.full_screen and not self._in_alternate_screen:
            self._in_alternate_screen = True
            output.enter_alternate_screen()
            pending = True
        if not self._bracketed_paste_enabled:
            output.enable_bracketed_paste()
            self._bracketed_paste_enabled = True
            pending = True
        if not self._cursor_key_mode_reset:
            output.reset_cursor_key_mode()
            self._cursor_key_mode_reset = True
            pending = True

        size = output.get_size()
        dims = (size.rows, size.columns)
        rows = text.split("\n")[: size.rows]

        # A resize invalidates both records of the screen: ours and prompt_toolkit's.
        full = self._prev_rows is None or self._prev_size != dims
        if not full and not pending and rows == self._prev_rows:
            # The app repaints on a timer to keep the header's pulse moving, and most of
            # those frames come back identical. Writing nothing at all is not merely cheaper
            # than writing the rows again — it leaves the panel's damage region empty, so
            # the display never flushes and the SPI bus stays quiet.
            self._last_size = size
            self.fast_paints += 1
            return
        if full:
            self._last_screen = None
            output.hide_cursor()
            output.reset_attributes()
            output.erase_screen()
            output.cursor_goto(0, 0)

        write = output.write_raw
        prev = self._prev_rows
        last = -2
        for i, row in enumerate(rows):
            if not full and i < len(prev) and prev[i] == row:
                continue
            # Stepping down a row with CRLF returns the terminal to a true column 0
            # whatever happened on the row above, and costs three bytes less than a
            # cursor address; only a real jump pays for one.
            write("\r\n" if i == last + 1 else f"\x1b[{i + 1};1H")
            # Erase *before* drawing, never after. A row that fills the terminal exactly
            # leaves the cursor in the last column with wrap pending, and an erase-to-end
            # there wipes the very character just written (the row's last cell went
            # missing on the PicoCalc's 53-column console). Clearing first also drops the
            # tail of a row that got shorter, which is what the erase was for.
            write("\x1b[0m\x1b[K")
            # Pinned, not merely written: an emoji in a node's name is drawn at whatever width
            # this font gives it, and the lanes after it belong to the app's grid either way.
            write(self._pin(row))
            last = i
        # Park the cursor out of the text. The app hides it, but a terminal that ignores
        # that should not leave it blinking in the middle of a row.
        write(f"\x1b[{len(rows)};1H")
        output.flush()

        self._prev_rows = rows
        self._prev_size = dims
        self._last_size = size
        # prompt_toolkit's grid no longer describes the screen — if a later paint falls
        # back to it, it must redraw everything rather than diff against a stale record.
        self._last_screen = None
        self.fast_paints += 1
