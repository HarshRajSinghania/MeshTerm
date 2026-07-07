"""Structured logging setup.

Two sinks are configured: a Rich-formatted console handler for human-readable output,
and a JSON-lines file handler that captures every record for later replay/analysis.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

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

    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child of the application logger.

    Args:
        name: Optional child name; when ``None`` the root app logger is returned.

    Returns:
        The requested logger.
    """
    if name is None:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
