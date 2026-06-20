"""The interactive main menu, built dynamically from the tool registry.

The menu groups registered tools by category and runs the selected tool's interactive
flow. Because it is generated from the registry, adding a tool automatically adds a menu
entry with no changes here.
"""

from __future__ import annotations

import questionary

from ..context import AppContext
from ..tools import all_tools
from .theme import make_console
from .widgets import banner

_MENU_STYLE = questionary.Style(
    [
        ("qmark", "fg:#5eead4 bold"),
        ("pointer", "fg:#818cf8 bold"),
        ("highlighted", "fg:#5eead4 bold"),
        ("selected", "fg:#4ade80"),
        ("separator", "fg:#475569"),
    ]
)


async def run_menu(ctx: AppContext) -> None:
    """Run the interactive menu loop until the user quits.

    Before showing the menu, prompts for a companion device (unless the simulator is in
    use or a port/profile was already chosen explicitly), so the banner reflects the
    target and tools connect to the right radio.

    Args:
        ctx: The shared application context.
    """
    console = ctx.console
    await _select_device_at_startup(ctx)
    console.print(banner(ctx.profile_name, ctx.mock, ctx.selected_device))

    while True:
        choices: list[questionary.Choice | questionary.Separator] = []
        current_category: str | None = None
        for tool in all_tools():
            if tool.category != current_category:
                current_category = tool.category
                choices.append(questionary.Separator(f"── {current_category} ──"))
            choices.append(
                questionary.Choice(title=f"{tool.name}  —  {tool.help}", value=tool.name)
            )
        choices.append(questionary.Separator(" "))
        choices.append(questionary.Choice(title="quit", value="__quit__"))

        selection = await questionary.select(
            "What would you like to do?",
            choices=choices,
            style=_MENU_STYLE,
            qmark="◆",
        ).ask_async()

        if selection in (None, "__quit__"):
            console.print("[muted]bye 73![/muted]")
            return

        await _run_selection(ctx, selection)
        console.print()


async def _select_device_at_startup(ctx: AppContext) -> None:
    """Discover devices and let the user pick one, recording it on the context.

    No-ops when the simulator is in use or a ``--port``/``--profile`` was given
    explicitly. A cancelled prompt leaves the context unresolved; tools needing the radio
    will then surface a clear error (or the user can pick again via the ``devices`` tool).

    Args:
        ctx: The shared application context to update with the selection.
    """
    if ctx.mock or ctx.explicit_selection:
        return

    from ..core.discovery import discover_devices
    from .device_picker import prompt_device

    devices = discover_devices()
    chosen = await prompt_device(ctx.console, devices, ctx.device_store.load())
    if chosen is not None:
        ctx.selected_device = chosen
        ctx.port_override = chosen.port


async def _run_selection(ctx: AppContext, name: str) -> None:
    """Gather parameters for and execute a single tool from the menu.

    Args:
        ctx: The shared application context.
        name: The selected tool's name.
    """
    from ..tools import get_tool

    tool = get_tool(name)
    if tool is None:  # pragma: no cover - registry and menu are always in sync
        ctx.console.print(f"[err]Unknown tool: {name}[/err]")
        return

    try:
        params = await tool.prompt_params(ctx)
        if params is None:  # user cancelled a prompt
            return
        result = await tool.execute(ctx, params)
    except Exception as exc:  # noqa: BLE001 - surface errors without crashing the menu
        ctx.console.print(f"[err]✗ {tool.name} failed:[/err] {exc}")
        return

    if result.message:
        ctx.console.print(result.message)
    for artifact in result.artifacts:
        ctx.console.print(f"[ok]●[/ok] wrote [accent]{artifact}[/accent]")


# Exposed for tools that need a standalone console outside a context (rare).
default_console = make_console
