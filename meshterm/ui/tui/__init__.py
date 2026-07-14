"""A small, centralized, reusable full-screen text-UI library.

Built on prompt_toolkit (for the terminal, input, and resize handling) and Rich (for all
content rendering), this package gives the interactive app a consistent, bounded, layered
UI: screens never overflow the terminal, long content scrolls, going deeper stacks dialogs
over a dimmed parent, and Esc always dismisses the current layer.

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
    AutocompleteScreen,
    ConfirmScreen,
    ReconnectDialog,
    TextScreen,
    TypedConfirmDialog,
)
from .screen import CANCEL, BusyScreen, Screen, ScrollScreen
from .select import Choice, DeleteRequest, SelectScreen, Separator
from .session import TuiSession
from .spinner import Spinner

__all__ = [
    "TuiSession",
    "Screen",
    "ScrollScreen",
    "BusyScreen",
    "SelectScreen",
    "Choice",
    "DeleteRequest",
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
]
