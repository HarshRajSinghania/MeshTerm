"""Interactive companion-device picker, rendered in the full-screen session.

Shown once at the start of the interactive menu when no port was given explicitly. It lists
the discovered devices, marks the remembered "last known good" one, and preselects it as the
default. The chosen device becomes the session's active device; it is persisted as the new
default once it actually connects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rich.text import Text

from ..core.device_store import RememberedDevice
from ..core.discovery import DiscoveredDevice
from .tui import Choice

if TYPE_CHECKING:
    from .surface import Ui

#: Trailing hint per discovery confidence tier (see :attr:`DiscoveredDevice.confidence`).
#: A bare serial bridge is only a weak hint, so it is not billed as a LoRa device.
_QUALIFIER: dict[str, str] = {
    "board": "  · LoRa device",
    "bridge": "  · serial adapter",
    "unknown": "",
}


async def prompt_device(
    ui: "Ui",
    devices: list[DiscoveredDevice],
    remembered: Optional[RememberedDevice],
) -> Optional[DiscoveredDevice]:
    """Prompt the user to choose a companion device.

    Args:
        ui: The interactive UI surface used to render the picker.
        devices: Discovered devices (likely-LoRa first).
        remembered: The remembered default, if any, used to mark and preselect a row.

    Returns:
        The chosen :class:`DiscoveredDevice`, or ``None`` if the user cancelled the picker
        (e.g. pressed Esc to run against ``--mock`` / configure later).
    """
    if not devices:
        await ui.view(
            Text.from_markup(
                "[warn]No serial devices detected.[/warn]\n"
                "Plug one in, pass [accent]--port[/accent], or run with [accent]--mock[/accent]."
            ),
            title="Select a companion device",
        )
        return None

    default: Optional[DiscoveredDevice] = None
    items: list = []
    for device in devices:
        is_remembered = remembered is not None and remembered.matches(device)
        star = "★ " if is_remembered else "  "
        # Lead with the remembered node name so a known radio is recognizable at a glance.
        name = f"{remembered.node_name} — " if is_remembered and remembered.node_name else ""
        vendor = f"  [{device.vendor_label}]" if device.vendor_label else ""
        items.append(
            Choice(title=f"{star}{name}{device.label}{vendor}{_QUALIFIER[device.confidence]}",
                   value=device)
        )
        if is_remembered:
            default = device

    return await ui.select("Select a companion device:", items, default=default)
