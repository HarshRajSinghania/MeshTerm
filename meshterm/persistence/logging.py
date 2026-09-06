"""Structured logging setup.

Two sinks are configured: a Rich-formatted console handler for human-readable output,
and a JSON-lines file handler that captures every record for later replay/analysis.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

_LOGGER_NAME = "meshterm"


class JsonlFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a log record to a compact JSON string.

        Args:
            record: The record to format.

        Returns:
            A single-line JSON document.
        """
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(
    console: Console,
    log_dir: Path,
    *,
    level: int = logging.INFO,
    quiet: bool = False,
) -> logging.Logger:
    """Configure and return the application logger.

    Args:
        console: The Rich console the handler should render to.
        log_dir: Directory for the ``meshterm.log.jsonl`` event file.
        level: Console log level.
        quiet: When ``True``, suppress console output (file logging continues).

    Returns:
        The configured ``meshterm`` logger.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    if not quiet:
        rich_handler = RichHandler(
            console=console, rich_tracebacks=True, show_path=False, markup=True
        )
        rich_handler.setLevel(level)
        logger.addHandler(rich_handler)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "meshterm.log.jsonl", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonlFormatter())
    logger.addHandler(file_handler)

    _quiet_library_console(file_handler)

    return logger


#: Third-party loggers that must never write to the terminal — their records would print
#: straight over the full-screen TUI. Routed to the file handler instead so they stay
#: captured for debugging. ``meshcore`` is the notable offender: it calls
#: ``logging.basicConfig(level=INFO)`` on import (e.g. "INFO:meshcore:Serial Connection
#: started"). ``asyncio`` is included so its default exception handler — the one that
#: reports stray background-task errors once prompt_toolkit's screen-dumping handler is
#: disabled — logs to the file instead of the console.
_LIBRARY_LOGGERS: tuple[str, ...] = ("meshcore", "asyncio")


def _quiet_library_console(file_handler: logging.Handler) -> None:
    """Keep noisy third-party libraries off the terminal (they corrupt the TUI).

    The ``meshcore`` client calls ``logging.basicConfig(level=INFO)`` when it is imported,
    which installs a stream handler on the *root* logger that prints its INFO lines straight
    over the screen. Two guards prevent that:

    1. Give the root logger a :class:`~logging.NullHandler` so ``basicConfig`` — which only
       acts when the root has no handlers — becomes a no-op and never adds its stream handler.
    2. Take each known library logger off propagation and attach only ``file_handler``, so
       its records are still captured to the JSON-lines log but never reach the console, even
       if some other code path installs a root stream handler anyway.

    Args:
        file_handler: The application's file handler to also capture library records to.
    """
    root = logging.getLogger()
    if not any(isinstance(h, logging.NullHandler) for h in root.handlers):
        root.addHandler(logging.NullHandler())

    for name in _LIBRARY_LOGGERS:
        lib = logging.getLogger(name)
        lib.handlers.clear()
        lib.setLevel(logging.INFO)
        lib.propagate = False
        lib.addHandler(file_handler)


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the application logger.

    Args:
        name: Optional child name; when ``None`` the root app logger is returned.

    Returns:
        The requested logger.
    """
    if name is None:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
