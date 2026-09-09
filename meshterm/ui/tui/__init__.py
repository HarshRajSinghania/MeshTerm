"""A small, centralized, reusable full-screen text-UI library.

Built on prompt_toolkit (for the terminal, input, and resize handling) and Rich (for all
content rendering), this package gives the interactive app a consistent, bounded, layered
UI: screens never overflow the terminal, long content scrolls, going deeper stacks dialogs
over a dimmed parent, and Esc always dismisses the current layer.

Navigation is a strict stack: one push per screen entered, one pop per Esc, and the screen
object survives the whole visit so its cursor, sort and filter are still there when a
sub-screen closes (:meth:`~meshterm.ui.tui.session.TuiSession.stay`). The one shortcut past
it is ^W, which unwinds every frame at once by raising
:class:`~meshterm.ui.tui.screen.PopToMenu` through them.

Public API:
    - :class:`~meshterm.ui.tui.session.TuiSession` — the running session and prompt helpers.
    - :class:`~meshterm.ui.tui.select.Choice` / :class:`~meshterm.ui.tui.select.Separator`
      — items for ``session.select``.
    - Screen classes for advanced/custom layers.
"""

from __future__ import annotations

from .overlay import BusyOverlay
from .progress import ProgressScreen
from .prompt import (
    CHANNEL_BYTE_LIMIT,
    DM_BYTE_LIMIT,
    AutocompleteScreen,
    ConfirmScreen,
    ReconnectDialog,
    TextScreen,
    TypedConfirmDialog,
    byte_counter,
)
from .screen import CANCEL, BusyDialog, BusyScreen, PopToMenu, Screen, ScrollScreen
from .select import Choice, DeleteRequest, KeyRequest, SelectScreen, Separator
from .session import TuiSession, Visit
from .spinner import Spinner

__all__ = [
    "TuiSession",
    "Screen",
    "ScrollScreen",
    "BusyDialog",
    "BusyScreen",
    "SelectScreen",
    "Choice",
    "DeleteRequest",
    "KeyRequest",
    "Separator",
    "TextScreen",
    "ConfirmScreen",
    "AutocompleteScreen",
    "ReconnectDialog",
    "TypedConfirmDialog",
    "ProgressScreen",
    "Spinner",
    "BusyOverlay",
    "CANCEL",
    "PopToMenu",
    "Visit",
    "DM_BYTE_LIMIT",
    "CHANNEL_BYTE_LIMIT",
    "byte_counter",
]
