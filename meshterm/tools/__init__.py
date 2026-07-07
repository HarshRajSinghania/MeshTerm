"""Pluggable tools (menu options / CLI subcommands).

Importing this package eagerly imports every tool module so that each one's ``@register``
decorator populates the shared registry. New features only need to add a module here.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import Tool, all_tools, get_tool, register

__all__ = ["Tool", "all_tools", "get_tool", "register", "load_all_tools"]


def load_all_tools() -> None:
    """Import every sibling module so its tools self-register.

    Modules whose names start with an underscore (and ``base``) are skipped.
    """
    package = __name__
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        importlib.import_module(f"{package}.{mod.name}")


# Populate the registry on import so the CLI and menu see every tool.
load_all_tools()
