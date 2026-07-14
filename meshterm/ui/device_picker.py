"""Interactive companion-device picker: the startup splash shown before the menu.

Shown once at the start of the interactive menu when no port was given explicitly. Unlike the
in-menu prompts, it is drawn as a chromeless splash — the MeshTerm wordmark centered above a
content-sized box, with no header/footer status bars. It lists the discovered devices in
aligned columns — name, connection target, a TYPE glyph (wired serial vs Bluetooth), and a
HARDWARE column (the confirmed device's firmware model, else the USB vendor) — tags the ones
already confirmed as MeshCore companions, marks the remembered "last known good" one, and
preselects it as the default.

Confirmed companions are sorted to the top, most-recently-used first, and shown by the mesh
node name we learned when we last talked to them (in white, so they stand out from ports we've
merely detected). Everything else follows in discovery order.

Selecting a device runs an immediate smoke test (via the ``verify`` callback): a genuine
MeshCore companion is remembered — forever — as confirmed and becomes the session's active
device; anything else sends the user back to the list to choose another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from rich.cells import cell_len
from rich.text import Text

from .. import copyright_notice
from ..core.connection import DeviceAuthenticationError, DeviceCommandError
from ..core.device_store import DeviceStore, RememberedDevice
from ..core.discovery import DiscoveredDevice
from .logo import load_logo
from .tui import Choice, Separator

if TYPE_CHECKING:
    from .surface import Ui

#: A smoke test: probe a chosen device with an optional Bluetooth PIN and return its self-info
#: dict if it is a MeshCore companion, else ``None``. Supplied by the caller so this UI module
#: stays free of the connection *logic*. It may raise :class:`DeviceAuthenticationError` when
#: the device needs a PIN (the picker then collects one and retries with it), or another
#: :class:`DeviceCommandError` for a different actionable failure (shown verbatim). The second
#: argument is the PIN to try, or ``None`` to use whatever default the caller holds.
Verify = Callable[[DiscoveredDevice, Optional[str]], Awaitable[Optional[dict]]]

#: The Quit row's value. Selecting it — like pressing Esc — leaves the splash without a
#: device, which the caller treats as "exit the program".
_QUIT = object()

#: TYPE-column glyphs marking how a device connects. Kept as module constants so the splash's
#: look can be retuned without touching the row-building logic. ``ᛒ`` is the *Bjarkan* rune the
#: Bluetooth logo is drawn from — rendered white on the Bluetooth blue (see the ``bluetooth``
#: theme style), flanked by the half-blocks below so it reads as a slim rounded badge — and
#: ``🔌`` is a plain plug for a wired serial link.
_BLE_ICON = "ᛒ"
_SERIAL_ICON = "🔌"

#: Half-block glyphs that taper the Bluetooth badge: ``▐`` fills a cell's right half (so it
#: hugs the rune's left edge) and ``▌`` its left half (hugging the right edge). Drawn in the
#: badge's blue over the terminal background, they widen the blue by half a cell on each side.
_BADGE_LEFT = "▐"
_BADGE_RIGHT = "▌"


def _type_cell(device: DiscoveredDevice) -> Text:
    """The TYPE-column badge for ``device`` as a styled fragment.

    Serial is a bare plug emoji (two cells, its own colour). Bluetooth is the rune on its blue
    badge, flanked by half-block slivers in the same blue so the fill reads as a slightly
    rounded chip a touch wider than the lone rune rather than a single hard-edged cell.

    The plug carries a leading space so its two cells sit centred under the "TYPE" heading,
    lining up with the Bluetooth rune (which the badge's left half-block already nudges in a
    cell) rather than hugging the column's left edge.
    """
    if not device.is_ble:
        return Text(" " + _SERIAL_ICON)
    cell = Text()
    cell.append(_BADGE_LEFT, style="bluetooth.edge")
    cell.append(_BLE_ICON, style="bluetooth")
    cell.append(_BADGE_RIGHT, style="bluetooth.edge")
    return cell


def _pad(text: str, width: int) -> str:
    """Right-pad ``text`` with spaces to ``width`` display cells (wide-char aware)."""
    return text + " " * max(0, width - cell_len(text))


def _hardware_name(device: DiscoveredDevice) -> str:
    """The device's product name without its trailing ``(port)`` (that is its own column)."""
    if device.is_ble:
        return device.name or device.product or device.description or "Bluetooth device"
    name = device.product or device.description or device.vendor_label or "Serial device"
    suffix = f"({device.port})"
    if name.endswith(suffix):
        name = name[: -len(suffix)].rstrip()
    return name


def _display_name(
    device: DiscoveredDevice, registry: dict[str, "RememberedDevice"]
) -> str:
    """The name to show for ``device``: its remembered mesh node name, else the hardware name.

    Any device we've confirmed before — not just the single most-recent one — is shown by the
    mesh node name we learned at connect time, so a known ``COM11`` reads as "BaseStation"
    rather than the OS's generic "USB Serial Device". Devices with no record (or an empty
    remembered name) fall back to their hardware/product name.
    """
    record = registry.get(device.stable_id)
    if record is not None and record.node_name:
        return record.node_name
    return _hardware_name(device)


def _where(device: DiscoveredDevice) -> str:
    """The connection target shown in the middle column: serial port or BLE address."""
    return device.target


def _hardware_label(
    device: DiscoveredDevice, registry: dict[str, "RememberedDevice"]
) -> str:
    """The HARDWARE column text: the remembered firmware model, else the USB vendor.

    A confirmed device shows what it actually is ("Seeed Tracker T1000-E", learned from the
    device-query at connect time) — the only reliable source, since a BLE companion advertises
    no maker. A device we've never connected has no model on file, so it falls back to the USB
    vendor name (blank for an unconnected BLE advert, which genuinely tells us nothing yet).
    """
    record = registry.get(device.stable_id)
    if record is not None and record.hardware_model:
        return record.hardware_model
    return device.vendor_label


def _node_name_from(info: dict) -> str:
    """Return a device's own mesh node name from its self-info payload, or ``""``."""
    return str(info.get("adv_name") or info.get("name") or "")


def _model_from(info: dict) -> str:
    """Return the firmware's hardware model from the probe payload, or ``""``.

    The smoke-test probe folds the device-query's ``model`` into the identity dict, so this
    is the one chance to learn (and then remember) what the box actually is.
    """
    return str(info.get("model") or "")


async def _smoke_test(
    ui: "Ui",
    chosen: DiscoveredDevice,
    name: str,
    where: str,
    verify: Verify,
) -> Optional[dict]:
    """Smoke-test ``chosen`` behind the splash spinner, collecting a Bluetooth PIN if needed.

    Runs the ``verify`` probe on the chromeless splash (its wordmark and box unchanged, only an
    animated spinner where the device list was). If the device answers but demands a pairing
    PIN, opens the :class:`~meshterm.ui.tui.prompt.PinDialog` popup and retries with what the
    user enters — re-opening it with a "rejected" note on a wrong code — until the device
    connects or the user presses Esc.

    Args:
        ui: The interactive surface used for the spinner, PIN dialog, and failure notices.
        chosen: The device being tested.
        name: Its display name, woven into the spinner line and the PIN prompt.
        where: A human phrase for its transport ("over Bluetooth" / "on COM5").
        verify: The smoke-test callback (see :data:`Verify`); called with the PIN to try.

    Returns:
        The device's self-info dict once it answers, or ``None`` — after showing the relevant
        notice — to send the user back to the device list (not a MeshCore endpoint, an
        unrecoverable failure, or a cancelled PIN prompt).
    """
    pin: Optional[str] = None
    pin_error = ""  # empty on the first ask; set once a PIN has been rejected
    while True:
        # BLE connect and service discovery take a few seconds, so the spinner matters most here.
        try:
            info = await ui.busy_startup(
                f"Talking to {name} {where}…",
                verify(chosen, pin),
                title="Checking companion",
                banner=load_logo(),
                footnote=copyright_notice(),
            )
        except DeviceAuthenticationError:
            # The device answered the scan but won't connect until it's bonded (or the last PIN
            # was wrong). Collect one in the popup and loop to retry; Esc returns to the list.
            entered = await ui.prompt_pin_startup(
                name,
                error=pin_error,
                help_text="The 6-digit code shown on the device or in the MeshCore app",
                banner=load_logo(),
                footnote=copyright_notice(),
            )
            if entered is None:
                return None  # the user gave up → back to the device list
            pin = entered
            pin_error = "That PIN was rejected — check the code and try again."
            continue
        except DeviceCommandError as exc:
            # A different actionable failure (not a PIN): show its remedy verbatim, then back to
            # the list. Plain styled text so the message's own punctuation isn't parsed as markup.
            notice = Text()
            notice.append(str(exc), style="warn")
            notice.append("\nChoose another device.")
            await ui.notify_startup(
                notice, title="Can't connect yet", banner=load_logo(), footnote=copyright_notice()
            )
            return None

        if info is None:
            reason = (
                "It may be out of range, powered off, already connected elsewhere, or busy."
                if chosen.is_ble
                else "It may be a different kind of serial device, powered off, or busy."
            )
            await ui.notify_startup(
                Text.from_markup(
                    f"[warn]{name} {where} didn't answer as a MeshCore device.[/warn]\n"
                    f"{reason}\n"
                    "Choose another device."
                ),
                title="Not a MeshCore device",
                banner=load_logo(),
                footnote=copyright_notice(),
            )
            return None

        return info


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
        The chosen, confirmed :class:`DiscoveredDevice`, or ``None`` if the user chose to
        leave the picker without selecting one — by pressing Esc, choosing the Quit row, or
        having no devices to pick — which the caller treats as a request to exit.
    """
    if not devices:
        await ui.notify_startup(
            Text.from_markup(
                "[warn]No companion devices detected.[/warn]\n"
                "Plug one in over USB or power on a Bluetooth companion nearby, pass "
                "[accent]--port[/accent]/[accent]--ble[/accent], or run with "
                "[accent]--mock[/accent]."
            ),
            title="Select a companion device",
            banner=load_logo(),
            footnote=copyright_notice(),
        )
        return None

    remembered = store.load()
    # The full registry (not just the single last device) so *every* confirmed companion can
    # be named, highlighted, and sorted to the top — keyed by stable_id.
    registry = store.load_all()
    # Preselect the remembered "last known good" device when it is currently attached.
    default = next((d for d in devices if remembered and remembered.matches(d)), None)

    while True:
        chosen = await ui.select_startup(
            "Select a companion device",
            _build_items(devices, remembered, registry),
            default=default,
            banner=load_logo(),
            footnote=copyright_notice(),
        )
        # Esc (``None``) and the Quit row both mean "leave the picker" — surface that to the
        # caller as ``None`` so it can exit the program instead of continuing device-less.
        if chosen is None or chosen is _QUIT:
            return None

        name = _display_name(chosen, registry)
        # "over Bluetooth" reads better than an address; a serial device names its port.
        where = "over Bluetooth" if chosen.is_ble else f"on {chosen.port}"
        # Smoke-test the choice in place (prompting for a PIN and retrying if it needs one).
        # ``None`` means the smoke test failed and already showed the user why — pick again.
        info = await _smoke_test(ui, chosen, name, where, verify)
        if info is None:
            continue

        # Confirmed: remember it forever. We return straight away, so there's no need to
        # fold it back into the local registry for a re-render.
        store.remember(
            chosen, node_name=_node_name_from(info), hardware_model=_model_from(info)
        )
        return chosen


def _order(
    devices: list[DiscoveredDevice], registry: dict[str, RememberedDevice]
) -> list[DiscoveredDevice]:
    """Confirmed companions first (most-recently-used first), then everything else as found.

    The devices we've actually spoken to are the ones the user almost always wants, so they
    rise to the top ordered by their last-connected timestamp (newest first). Unknown ports
    keep their incoming discovery order — a stable sort preserves it since they all tie.
    """
    known = [d for d in devices if d.stable_id in registry]
    others = [d for d in devices if d.stable_id not in registry]
    known.sort(key=lambda d: registry[d.stable_id].last_connected, reverse=True)
    return known + others


def _build_items(
    devices: list[DiscoveredDevice],
    remembered: Optional[RememberedDevice],
    registry: dict[str, RememberedDevice],
) -> list:
    """Build the aligned splash rows (a muted header + one :class:`Choice` per device).

    The row for each device leads with its display name (the remembered node's name when
    known, else the hardware name), followed by its connection target, a TYPE glyph marking
    the transport, and the HARDWARE column (the remembered firmware model, else the USB
    vendor); columns are padded to a shared width so they align. Confirmed companions sort to the top (most-recent first), wear their name in white
    and a bright tag; the remembered default is starred. A trailing Quit row (like the menu's)
    lets the user exit from here.
    """
    devices = _order(devices, registry)
    known: set[str] = set(registry)

    name_w = max(cell_len(_display_name(d, registry)) for d in devices)
    name_w = max(name_w, len("DEVICE"))
    # The middle column holds a serial port or a BLE address; label it for whichever kinds
    # are present so a Bluetooth address never sits under a bare "PORT" heading.
    port_label = "PORT"
    if any(d.is_ble for d in devices):
        port_label = "ADDRESS" if all(d.is_ble for d in devices) else "PORT / ADDRESS"
    port_w = max(cell_len(_where(d)) for d in devices)
    port_w = max(port_w, len(port_label))
    # The TYPE column holds a small transport badge (at most 3 cells); its heading is wider,
    # so the four-cell "TYPE" label sets the column width and every badge pads out to it.
    type_w = len("TYPE")
    hardware_w = max(cell_len(_hardware_label(d, registry)) for d in devices)
    hardware_w = max(hardware_w, len("HARDWARE"))

    # A muted, aligned header. The leading spaces mirror the row pointer (2) and the star
    # column (2) so the labels sit above their columns.
    header = Separator(
        "    "
        + _pad("DEVICE", name_w)
        + "  " + _pad(port_label, port_w)
        + "  " + _pad("TYPE", type_w)
        + "  " + "HARDWARE"
    )

    items: list = [header]
    for device in devices:
        is_remembered = remembered is not None and remembered.matches(device)
        is_known = device.stable_id in known
        row = Text()
        row.append("★" if is_remembered else " ", style="warn" if is_remembered else "")
        row.append(" ")
        # A confirmed companion wears its name in white so it stands out from mere detections.
        row.append(_pad(_display_name(device, registry), name_w),
                   style="device.known" if is_known else "")
        row.append("  ")
        row.append(_pad(_where(device), port_w), style="muted")
        row.append("  ")
        # The TYPE badge marks the transport: a plug emoji for serial, or the Bluetooth rune
        # on its blue badge for BLE. It's built with its own colours, then the column is
        # padded with plain spaces — so the blue fill hugs just the badge, and the differing
        # badge widths (emoji 2 cells, rune-plus-edges 3) still line up under "TYPE".
        cell = _type_cell(device)
        row.append_text(cell)
        row.append(" " * max(0, type_w - cell.cell_len))
        row.append("  ")
        row.append(_pad(_hardware_label(device, registry), hardware_w), style="muted")
        # Only devices we've actually confirmed are billed as MeshCore companions; a USB
        # vendor ID (or a BLE advert) is a sort hint, not a claim. A bare serial bridge earns
        # an honest label; a MeshCore-named BLE advert is flagged as a likely companion.
        if is_known:
            row.append("  ")
            row.append("· MeshCore device", style="ok")
        elif device.is_ble:
            row.append("  ")
            row.append("· Bluetooth companion", style="muted")
        elif device.confidence == "bridge":
            row.append("  ")
            row.append("· serial adapter", style="muted")
        items.append(Choice(title=row, value=device))
    # A trailing Quit row, mirroring the main menu, so exiting is an explicit choice as well
    # as an Esc away — the leading spaces line it up under the device-name column.
    items.append(Separator(" "))
    items.append(Choice(title="  Quit", value=_QUIT))
    return items
