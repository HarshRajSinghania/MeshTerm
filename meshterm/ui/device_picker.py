"""Interactive companion-device picker: the startup splash shown before the menu.

Shown once at the start of the interactive menu when no port was given explicitly. Unlike the
in-menu prompts, it is drawn as a chromeless splash — the MeshTerm wordmark centered above a
content-sized box, with no header/footer status bars. It lists the discovered devices in
aligned columns, marks the remembered "last known good" one, and preselects it as the default.
The chosen device becomes the session's active device; it is persisted as the new default once
it actually connects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from rich.cells import cell_len
from rich.text import Text

from ..core.device_store import RememberedDevice
from ..core.discovery import DiscoveredDevice
from .logo import LOGO
from .tui import Choice, Separator

if TYPE_CHECKING:
    from .surface import Ui

#: Trailing tag per discovery confidence tier (see :attr:`DiscoveredDevice.confidence`).
#: A bare serial bridge is only a weak hint, so it is not billed as a LoRa device.
_TAG: dict[str, str] = {
    "board": "· LoRa device",
    "bridge": "· serial adapter",
    "unknown": "",
}


def _pad(text: str, width: int) -> str:
    """Right-pad ``text`` with spaces to ``width`` display cells (wide-char aware)."""
    return text + " " * max(0, width - cell_len(text))


def _hardware_name(device: DiscoveredDevice) -> str:
    """The device's product name without its trailing ``(port)`` (that is its own column)."""
    name = device.product or device.description or device.vendor_label or "Serial device"
    suffix = f"({device.port})"
    if name.endswith(suffix):
        name = name[: -len(suffix)].rstrip()
    return name


async def prompt_device(
    ui: "Ui",
    devices: list[DiscoveredDevice],
    remembered: Optional[RememberedDevice],
) -> Optional[DiscoveredDevice]:
    """Prompt the user to choose a companion device on the startup splash.

    Args:
        ui: The interactive UI surface used to render the picker.
        devices: Discovered devices (likely-LoRa first).
        remembered: The remembered default, if any, used to mark and preselect a row.

    Returns:
        The chosen :class:`DiscoveredDevice`, or ``None`` if the user skipped the picker
        (e.g. pressed Esc to run against ``--mock`` / configure later).
    """
    if not devices:
        await ui.notify_startup(
            Text.from_markup(
                "[warn]No serial devices detected.[/warn]\n"
                "Plug one in, pass [accent]--port[/accent], or run with [accent]--mock[/accent]."
            ),
            title="Select a companion device",
            banner=LOGO,
        )
        return None

    # The row for each device leads with its display name (the remembered node's name when
    # known, else the hardware name); columns are padded to a shared width so they align.
    def _name(device: DiscoveredDevice) -> str:
        if remembered is not None and remembered.matches(device) and remembered.node_name:
            return remembered.node_name
        return _hardware_name(device)

    name_w = max(cell_len(_name(d)) for d in devices) if devices else 0
    name_w = max(name_w, len("DEVICE"))
    port_w = max((cell_len(d.port) for d in devices), default=0)
    port_w = max(port_w, len("PORT"))
    vendor_w = max((cell_len(d.vendor_label) for d in devices), default=0)
    vendor_w = max(vendor_w, len("VENDOR"))

    # A muted, aligned header. The leading spaces mirror the row pointer (2) and the star
    # column (2) so the labels sit above their columns.
    header = Separator(
        "    "
        + _pad("DEVICE", name_w)
        + "  " + _pad("PORT", port_w)
        + "  " + "VENDOR"
    )

    default: Optional[DiscoveredDevice] = None
    items: list = [header]
    for device in devices:
        is_remembered = remembered is not None and remembered.matches(device)
        row = Text()
        row.append("★" if is_remembered else " ", style="warn" if is_remembered else "")
        row.append(" ")
        row.append(_pad(_name(device), name_w))
        row.append("  ")
        row.append(_pad(device.port, port_w), style="muted")
        row.append("  ")
        row.append(_pad(device.vendor_label, vendor_w), style="muted")
        tag = _TAG[device.confidence]
        if tag:
            row.append("  ")
            row.append(tag, style="muted")
        items.append(Choice(title=row, value=device))
        if is_remembered:
            default = device

    return await ui.select_startup(
        "Select a companion device", items, default=default, banner=LOGO
    )
