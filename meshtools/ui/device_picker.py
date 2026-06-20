"""Interactive companion-device picker (questionary).

Shown at the start of the interactive menu (and from the ``devices`` tool) when no port
was given explicitly. It lists the discovered devices, marks the remembered "last known
good" one, and preselects it as the default. The chosen device becomes the session's
active device; it is persisted as the new default once it actually connects.
"""

from __future__ import annotations

from typing import Optional

import questionary
from rich.console import Console

from ..core.device_store import RememberedDevice
from ..core.discovery import DiscoveredDevice
from .menu import _MENU_STYLE


async def prompt_device(
    console: Console,
    devices: list[DiscoveredDevice],
    remembered: Optional[RememberedDevice],
) -> Optional[DiscoveredDevice]:
    """Prompt the user to choose a companion device.

    Args:
        console: Console used for any surrounding messaging.
        devices: Discovered devices (likely-LoRa first).
        remembered: The remembered default, if any, used to mark and preselect a row.

    Returns:
        The chosen :class:`DiscoveredDevice`, or ``None`` if the user cancelled or chose
        to skip device selection (e.g. to run against ``--mock`` / configure later).
    """
    if not devices:
        console.print(
            "[warn]No serial devices detected.[/warn] "
            "Plug one in, pass [accent]--port[/accent], or run with [accent]--mock[/accent]."
        )
        return None

    default_choice: Optional[questionary.Choice] = None
    choices: list[questionary.Choice | questionary.Separator] = []
    for device in devices:
        is_remembered = remembered is not None and remembered.matches(device)
        star = "★ " if is_remembered else "  "
        vendor = f"  [{device.vendor_label}]" if device.vendor_label else ""
        lora = "  · likely LoRa" if device.is_likely_lora else ""
        choice = questionary.Choice(title=f"{star}{device.label}{vendor}{lora}", value=device)
        choices.append(choice)
        # Preselect the remembered device; else default to the first (top-ranked) row.
        if is_remembered:
            default_choice = choice
    if default_choice is None:
        default_choice = choices[0]  # type: ignore[assignment]  # always a Choice here

    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice(title="skip (use --mock / configure later)", value=None))

    return await questionary.select(
        "Select a companion device:",
        choices=choices,
        default=default_choice,
        style=_MENU_STYLE,
        qmark="◆",
    ).ask_async()
