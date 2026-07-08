"""Interactive companion-device picker: the startup splash shown before the menu.

Shown once at the start of the interactive menu when no port was given explicitly. Unlike the
in-menu prompts, it is drawn as a chromeless splash — the MeshTerm wordmark centered above a
content-sized box, with no header/footer status bars. It lists the discovered devices in
aligned columns, tags the ones already confirmed as MeshCore companions, marks the remembered
"last known good" one, and preselects it as the default.

Selecting a device runs an immediate smoke test (via the ``verify`` callback): a genuine
MeshCore companion is remembered — forever — as confirmed and becomes the session's active
device; anything else sends the user back to the list to choose another.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from rich.cells import cell_len
from rich.text import Text

from ..core.device_store import DeviceStore, RememberedDevice
from ..core.discovery import DiscoveredDevice
from .logo import load_logo
from .tui import Choice, Separator

if TYPE_CHECKING:
    from .surface import Ui

#: A smoke test: probe a chosen device and return its self-info dict if it is a MeshCore
#: companion, else ``None``. Supplied by the caller so this UI module stays free of the
#: connection layer.
Verify = Callable[[DiscoveredDevice], Awaitable[Optional[dict]]]


def _copyright() -> str:
    """The splash's muted copyright line, dated to the current year."""
    return f"© {datetime.now().year} Johnputer"


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


def _node_name_from(info: dict) -> str:
    """Return a device's own mesh node name from its self-info payload, or ``""``."""
    return str(info.get("adv_name") or info.get("name") or "")


async def prompt_device(
    ui: "Ui",
    devices: list[DiscoveredDevice],
    store: DeviceStore,
    verify: Verify,
) -> Optional[DiscoveredDevice]:
    """Prompt the user to choose a companion device on the startup splash.

    The chosen device is smoke-tested before it is accepted: only a device that answers the
    MeshCore identity query is returned (and recorded as confirmed). A device that fails the
    test re-opens the picker with a message asking the user to choose another.

    Args:
        ui: The interactive UI surface used to render the picker.
        devices: Discovered devices (likely-LoRa first).
        store: The confirmed-device registry, used to tag/preselect known devices and to
            record a device once its smoke test passes.
        verify: Async smoke test returning a device's self-info dict, or ``None`` if it is
            not a reachable MeshCore companion.

    Returns:
        The chosen, confirmed :class:`DiscoveredDevice`, or ``None`` if the user skipped the
        picker (e.g. pressed Esc to run against ``--mock`` / configure later).
    """
    if not devices:
        await ui.notify_startup(
            Text.from_markup(
                "[warn]No serial devices detected.[/warn]\n"
                "Plug one in, pass [accent]--port[/accent], or run with [accent]--mock[/accent]."
            ),
            title="Select a companion device",
            banner=load_logo(),
            footnote=_copyright(),
        )
        return None

    remembered = store.load()
    known: set[str] = set(store.load_all())
    # Preselect the remembered "last known good" device when it is currently attached.
    default = next((d for d in devices if remembered and remembered.matches(d)), None)

    while True:
        chosen = await ui.select_startup(
            "Select a companion device",
            _build_items(devices, remembered, known),
            default=default,
            banner=load_logo(),
            footnote=_copyright(),
        )
        if chosen is None:
            return None

        name = remembered.node_name if (
            remembered and remembered.matches(chosen) and remembered.node_name
        ) else _hardware_name(chosen)
        # Smoke-test the choice in place: the splash keeps its wordmark and box, only the box
        # contents swap for an animated spinner while we talk to the device.
        info = await ui.busy_startup(
            f"Talking to {name} on {chosen.port}…",
            verify(chosen),
            title="Checking companion",
            banner=load_logo(),
            footnote=_copyright(),
        )

        if info is None:
            await ui.notify_startup(
                Text.from_markup(
                    f"[warn]{name} on {chosen.port} didn't answer as a MeshCore device.[/warn]\n"
                    "It may be a different kind of serial device, powered off, or busy.\n"
                    "Choose another device."
                ),
                title="Not a MeshCore device",
                banner=load_logo(),
                footnote=_copyright(),
            )
            continue

        # Confirmed: remember it forever, and reflect that on any re-entry of the loop.
        store.remember(chosen, node_name=_node_name_from(info))
        known.add(chosen.stable_id)
        return chosen


def _build_items(
    devices: list[DiscoveredDevice],
    remembered: Optional[RememberedDevice],
    known: set[str],
) -> list:
    """Build the aligned splash rows (a muted header + one :class:`Choice` per device).

    The row for each device leads with its display name (the remembered node's name when
    known, else the hardware name); columns are padded to a shared width so they align.
    Devices already confirmed as MeshCore companions carry a bright tag; the remembered
    default is starred.
    """
    def _name(device: DiscoveredDevice) -> str:
        if remembered is not None and remembered.matches(device) and remembered.node_name:
            return remembered.node_name
        return _hardware_name(device)

    name_w = max(cell_len(_name(d)) for d in devices)
    name_w = max(name_w, len("DEVICE"))
    port_w = max(cell_len(d.port) for d in devices)
    port_w = max(port_w, len("PORT"))
    vendor_w = max(cell_len(d.vendor_label) for d in devices)
    vendor_w = max(vendor_w, len("VENDOR"))

    # A muted, aligned header. The leading spaces mirror the row pointer (2) and the star
    # column (2) so the labels sit above their columns.
    header = Separator(
        "    "
        + _pad("DEVICE", name_w)
        + "  " + _pad("PORT", port_w)
        + "  " + "VENDOR"
    )

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
        # Only devices we've actually confirmed are billed as MeshCore companions; the USB
        # vendor ID is a sort hint, not a claim. A bare serial bridge earns an honest label.
        if device.stable_id in known:
            row.append("  ")
            row.append("· MeshCore device", style="ok")
        elif device.confidence == "bridge":
            row.append("  ")
            row.append("· serial adapter", style="muted")
        items.append(Choice(title=row, value=device))
    return items
