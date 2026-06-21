"""The tool plugin contract and registry.

A *tool* is one menu option / CLI subcommand. Subclass :class:`Tool`, decorate it with
:func:`register`, and implement :meth:`Tool.run`. The same registry drives both the
interactive menu and the Typer CLI, and :meth:`Tool.execute` wraps every invocation in a
logged ``runs`` row, so new tools get persistence and logging for free.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # avoid importing typer/context at module load for fast startup
    import typer

    from ..context import AppContext


@dataclass(slots=True)
class ToolResult:
    """The outcome of a tool execution.

    Attributes:
        summary: JSON-serializable summary persisted to the run record.
        message: Optional human-readable closing message.
        artifacts: Paths to any files produced (e.g. generated visualizations).
    """

    summary: dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None
    artifacts: list[str] = field(default_factory=list)


class Tool(ABC):
    """Base class for all MeshTools features.

    Class Attributes:
        name: CLI/menu identifier (kebab-case), unique across tools.
        help: One-line description shown in the menu and ``--help``.
        category: Grouping label used to organize the interactive menu.
        order: Sort key within a category (lower sorts first).
    """

    name: str = ""
    help: str = ""
    category: str = "General"
    order: int = 100

    @abstractmethod
    async def run(self, ctx: "AppContext", params: dict[str, Any]) -> ToolResult:
        """Execute the tool's work.

        Args:
            ctx: Shared application context (console, device, repository, ...).
            params: Validated parameters for this invocation.

        Returns:
            A :class:`ToolResult` describing the outcome.
        """

    async def prompt_params(self, ctx: "AppContext") -> dict[str, Any]:
        """Interactively gather parameters for the menu.

        The default implementation requires no parameters. Tools override this to ask
        the user via ``questionary``.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict compatible with :meth:`run`.
        """
        return {}

    def register_cli(self, app: "typer.Typer") -> None:
        """Register this tool as a Typer subcommand.

        The default registers a no-argument command. Tools with parameters override this
        to declare typed options, then delegate to :meth:`execute`.

        Args:
            app: The Typer application to add the command to.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _command() -> None:
            run_tool_command(self, {})

    async def execute(self, ctx: "AppContext", params: dict[str, Any]) -> ToolResult:
        """Run the tool wrapped in run-logging and error capture.

        Opens a ``runs`` row before execution and closes it with the final status and
        summary afterward, regardless of success.

        Args:
            ctx: Shared application context.
            params: Parameters for this invocation.

        Returns:
            The :class:`ToolResult` from :meth:`run`.

        Raises:
            Exception: Re-raises any error from :meth:`run` after recording it.
        """
        from ..core.connection import DeviceCommandError
        from ..core.device_config import DeviceConfigError
        from ..core.selection import DeviceSelectionError

        run_id = ctx.repo.start_run(self.name, params, ctx.profile_name)
        ctx.log.debug("run %s start: tool=%s params=%s", run_id, self.name, params)
        try:
            result = await self.run(ctx, {**params, "_run_id": run_id})
        except (DeviceSelectionError, DeviceConfigError, DeviceCommandError) as exc:
            # Expected user-facing condition (no/ambiguous device, bad config value, or
            # a transient command failure): record it but don't dump a traceback;
            # callers print the message cleanly.
            ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            ctx.log.debug("run %s aborted: %s", run_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - we record then re-raise
            ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            ctx.log.exception("run %s failed: %s", run_id, exc)
            raise
        ctx.repo.finish_run(run_id, "ok", result.summary)
        ctx.log.debug("run %s ok", run_id)
        return result


_REGISTRY: dict[str, Tool] = {}


def register(cls: type[Tool]) -> type[Tool]:
    """Class decorator that instantiates a tool and adds it to the registry.

    Args:
        cls: The :class:`Tool` subclass to register.

    Returns:
        The unchanged class (so the decorator is transparent).

    Raises:
        ValueError: If the tool has no ``name`` or the name is already registered.
    """
    instance = cls()
    if not instance.name:
        raise ValueError(f"{cls.__name__} must define a non-empty 'name'.")
    if instance.name in _REGISTRY:
        raise ValueError(f"Duplicate tool name: {instance.name!r}")
    _REGISTRY[instance.name] = instance
    return cls


def all_tools() -> list[Tool]:
    """Return all registered tools sorted by category then order then name.

    Returns:
        The registered tool instances in stable menu order.
    """
    return sorted(_REGISTRY.values(), key=lambda t: (t.category, t.order, t.name))


def get_tool(name: str) -> Optional[Tool]:
    """Look up a registered tool by name.

    Args:
        name: The tool's ``name``.

    Returns:
        The tool instance, or ``None`` if not registered.
    """
    return _REGISTRY.get(name)
