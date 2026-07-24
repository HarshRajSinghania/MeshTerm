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

from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, Optional

from rich.cells import cell_len
from rich.text import Text

from .. import copyright_notice
from ..core.config import DeviceProfile
from ..core.connection import DeviceAuthenticationError, DeviceCommandError
from ..core.device_store import DeviceStore, RememberedDevice
from ..core.discovery import (
    DEFAULT_TCP_PORT,
    DiscoveredDevice,
    parse_tcp_endpoint,
    tcp_device,
)
from .logo import load_logo
from .tui import Choice, DeleteRequest, Separator

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

#: The "add a network device" row's value. Selecting it opens a host:port prompt (TCP
#: companions aren't discoverable, so they're named by hand) and smoke-tests the result.
_ADD_TCP = object()

#: TYPE-column glyphs marking how a device connects. Kept as module constants so the splash's
#: look can be retuned without touching the row-building logic. ``ᛒ`` is the *Bjarkan* rune the
#: Bluetooth logo is drawn from — rendered white on the Bluetooth blue (see the ``bluetooth``
#: theme style), flanked by the half-blocks below so it reads as a slim rounded badge — and
#: ``🔌`` is a plain plug for a wired serial link.
_BLE_ICON = "ᛒ"
_SERIAL_ICON = "🔌"

#: TYPE-column glyph for a TCP companion — a globe, marking a device reached over the network
#: rather than a wired or Bluetooth link. Two cells like the serial plug, so it aligns the same.
_TCP_ICON = "🌐"

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
    if device.is_tcp:
        return Text(" " + _TCP_ICON)
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
    if device.is_tcp:
        return device.name or device.product or device.description or "Network device"
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
            if chosen.is_tcp:
                reason = (
                    "Check the host and port — it may be unreachable, powered off, already "
                    "connected elsewhere, or busy."
                )
            elif chosen.is_ble:
                reason = (
                    "It may be out of range, powered off, already connected elsewhere, or busy."
                )
            else:
                reason = "It may be a different kind of serial device, powered off, or busy."
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
    profiles: Optional[Mapping[str, DeviceProfile]] = None,
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
        profiles: The configured device profiles (``config.toml`` ``[profiles.*]``). Their TCP
            entries are folded into the list so a hand-authored network companion appears here
            without having to be connected to first (see :func:`_profile_tcp_devices`).

    Returns:
        The chosen, confirmed :class:`DiscoveredDevice`, or ``None`` if the user chose to
        leave the picker without selecting one — by pressing Esc or choosing the Quit row —
        which the caller treats as a request to exit.
    """
    scanned = list(devices)
    while True:
        remembered = store.load()
        # The full registry (not just the single last device) so *every* confirmed companion
        # can be named, highlighted, and sorted to the top — keyed by stable_id. Reloaded each
        # pass so an add or a removal is reflected the next time the list is drawn.
        registry = store.load_all()
        # A TCP companion isn't discoverable, so a previously confirmed one only reappears if we
        # rebuild it from its remembered endpoint and fold it into the list alongside the
        # scanned devices (the scan never produces it). Configured TCP profiles are folded in
        # the same way, after the scanned/remembered set so an already-known endpoint keeps its
        # richer remembered row rather than being shadowed by the profile.
        listed = scanned + _remembered_tcp_devices(scanned, registry)
        listed += _profile_tcp_devices(listed, profiles)
        # Preselect the remembered "last known good" device when it is currently attached/in range.
        default = next((d for d in listed if remembered and remembered.matches(d)), None)

        # Build the rows once so the same list can be redrawn as the backdrop behind a
        # removal confirm (so it floats over the picker rather than replacing it).
        items = _build_items(listed, remembered, registry)
        chosen = await ui.select_startup(
            "Select a companion device",
            items,
            default=default,
            banner=load_logo(),
            footnote=copyright_notice(),
        )
        # Esc (``None``) and the Quit row both mean "leave the picker" — surface that to the
        # caller as ``None`` so it can exit the program instead of continuing device-less.
        if chosen is None or chosen is _QUIT:
            return None

        if chosen is _ADD_TCP:
            # Name a network companion by hand and smoke-test it. On success it's returned like
            # any picked device; on cancel/failure we loop back to the list.
            added = await _add_network_device(ui, store, registry, verify)
            if added is not None:
                return added
            continue

        if isinstance(chosen, DeleteRequest):
            # Delete was pressed on a removable (network) row: confirm, forget, and re-draw
            # the list — the row's disappearance is the visible feedback. The list is passed
            # through so the confirm floats over it (the row it removes stays highlighted).
            await _remove_network_device(
                ui, store, registry, chosen.value, backdrop_items=items
            )
            continue

        name = _display_name(chosen, registry)
        # A human phrase for the transport: an address/endpoint reads worse than a plain word.
        where = _where_phrase(chosen)
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


def _where_phrase(device: DiscoveredDevice) -> str:
    """A human phrase for a device's transport, woven into the smoke-test spinner line."""
    if device.is_tcp:
        return f"at {device.target}"
    if device.is_ble:
        return "over Bluetooth"
    return f"on {device.port}"


def _remembered_tcp_devices(
    discovered: list[DiscoveredDevice], registry: dict[str, RememberedDevice]
) -> list[DiscoveredDevice]:
    """Rebuild the confirmed TCP companions from the registry as :class:`DiscoveredDevice`.

    TCP companions don't advertise, so the scan never finds them; this reconstructs each
    remembered one from its stored ``host:port`` (carrying its known node name for the DEVICE
    column) so it reappears in the picker. Any that happen to already be in ``discovered``
    (e.g. re-run within a session) are skipped so they aren't listed twice.
    """
    seen = {d.stable_id for d in discovered}
    rebuilt: list[DiscoveredDevice] = []
    for record in registry.values():
        if not record.is_tcp or not record.host or not record.tcp_port:
            continue
        device = tcp_device(record.host, record.tcp_port, name=record.node_name)
        if device.stable_id not in seen:
            rebuilt.append(device)
    return rebuilt


def _profile_tcp_devices(
    listed: list[DiscoveredDevice],
    profiles: Optional[Mapping[str, DeviceProfile]],
) -> list[DiscoveredDevice]:
    """Rebuild configured TCP profiles as :class:`DiscoveredDevice` rows for the picker.

    A ``[profiles.<alias>]`` block with ``transport = "tcp"`` names a network companion the
    user wants to reach — but a TCP endpoint isn't discoverable, so without this it would only
    ever surface via ``meshterm -p <alias>`` on the command line, never in the interactive
    splash. Each such profile is turned into a device carrying its alias as the DEVICE-column
    name (that is how the user addresses it), so it lists like a hand-added network device and
    smoke-tests the same way. Any profile whose endpoint is already present — scanned, or a
    remembered companion carrying its real node name — is skipped so the richer existing row
    wins rather than being duplicated by the bare profile.

    Args:
        listed: The devices already gathered (scanned + remembered), for de-duplication.
        profiles: The configured profiles, or ``None`` when none are loaded.

    Returns:
        One TCP :class:`DiscoveredDevice` per not-yet-listed TCP profile, in profile order.
    """
    if not profiles:
        return []
    seen = {d.stable_id for d in listed}
    rebuilt: list[DiscoveredDevice] = []
    for profile in profiles.values():
        if not profile.is_tcp or not profile.host:
            continue
        port = profile.tcp_port or DEFAULT_TCP_PORT
        device = tcp_device(profile.host, port, name=profile.name)
        if device.stable_id in seen:
            continue
        seen.add(device.stable_id)
        rebuilt.append(device)
    return rebuilt


async def _add_network_device(
    ui: "Ui",
    store: DeviceStore,
    registry: dict[str, RememberedDevice],
    verify: Verify,
) -> Optional[DiscoveredDevice]:
    """Collect a ``host:port``, smoke-test the network companion there, and remember it.

    A network (TCP) companion is named by hand — it isn't attached and doesn't advertise — so
    this opens a text prompt on the splash, parses the endpoint (a bare host defaults its
    port), and runs the same smoke test the discovered transports use. On success the device
    is remembered forever and returned; on a cancelled prompt or a failed test the caller
    re-opens the device list.

    Args:
        ui: The interactive surface for the prompt, spinner, and notices.
        store: The confirmed-device registry, updated once the device answers.
        registry: The current registry, for naming the smoke-test spinner line.
        verify: The smoke-test callback (see :data:`Verify`).

    Returns:
        The confirmed TCP :class:`DiscoveredDevice`, or ``None`` to return to the device list.
    """
    def _validate(text: str) -> object:
        try:
            parse_tcp_endpoint(text)
        except ValueError as exc:
            return str(exc)
        return True

    entered = await ui.prompt_text_startup(
        "Add a network device",
        prompt="Enter the companion's network address:",
        validate=_validate,
        help_text=f"host or host:port — the port defaults to {DEFAULT_TCP_PORT}",
        banner=load_logo(),
        footnote=copyright_notice(),
    )
    if entered is None:
        return None  # cancelled → back to the device list
    host, port = parse_tcp_endpoint(entered)  # already validated above
    device = tcp_device(host, port)
    name = _display_name(device, registry)
    info = await _smoke_test(ui, device, name, _where_phrase(device), verify)
    if info is None:
        return None  # not a reachable companion — the smoke test already explained why
    store.remember(
        device, node_name=_node_name_from(info), hardware_model=_model_from(info)
    )
    return device


async def _remove_network_device(
    ui: "Ui",
    store: DeviceStore,
    registry: dict[str, RememberedDevice],
    device: DiscoveredDevice,
    *,
    backdrop_items: list,
) -> None:
    """Confirm and forget a remembered network (TCP) device, dropping it from the picker.

    Removal is offered only on network rows: a TCP companion is listed solely from its
    remembered endpoint, so forgetting it is what makes it leave the picker — a scanned serial
    or BLE device would just reappear on the next scan. Opens the reserved-red Cancel/Remove
    confirm as a modal popup floating over the device list (``backdrop_items``, with the row
    being removed left highlighted), so it reads as a dialog on top of the picker rather than a
    splash that replaces it. On Remove the record is pruned from the store, on Cancel/Esc
    nothing changes. Either way the caller re-opens the list, so the row's absence is the
    feedback.

    Args:
        ui: The interactive surface for the confirm dialog.
        store: The confirmed-device registry to prune.
        registry: The current registry, for naming the device in the prompt.
        device: The network device the user asked to remove.
        backdrop_items: The picker's rows, redrawn behind the floating confirm.
    """
    if not device.is_tcp:
        return  # defensive: only network rows opt into deletion (see _build_items)
    name = _display_name(device, registry)
    confirmed = await ui.confirm_startup(
        f"Remove {name} ({device.target}) from the device list?",
        title="Remove network device",
        confirm_label="Remove",
        banner=load_logo(),
        footnote=copyright_notice(),
        backdrop_items=backdrop_items,
        backdrop_default=device,
    )
    if confirmed:
        store.forget(device.stable_id)


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
    vendor); columns are padded to a shared width so they align. Confirmed companions sort to
    the top (most-recent first), wear their name in white and a bright tag; the remembered
    default is starred. Trailing rows let the user name a network device by hand and quit here.
    """
    if not devices:
        # Nothing attached or in range — but a TCP companion can still be reached by hand, so
        # show a muted note over the same action rows rather than a dead-end.
        note = Separator("    no companion devices detected — add a network device, or quit")
        return [note, *_action_rows()]
    devices = _order(devices, registry)
    known: set[str] = set(registry)

    name_w = max(cell_len(_display_name(d, registry)) for d in devices)
    name_w = max(name_w, len("DEVICE"))
    # The middle column holds a serial port, a BLE address, or a TCP host:port; label it for
    # whichever kinds are present so a non-serial endpoint never sits under a bare "PORT"
    # heading (a BLE address and a network host:port both read as an "address").
    has_serial = any(not d.is_ble and not d.is_tcp for d in devices)
    has_address = any(d.is_ble or d.is_tcp for d in devices)
    port_label = (
        "PORT / ADDRESS" if has_serial and has_address
        else "ADDRESS" if has_address
        else "PORT"
    )
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
        elif device.is_tcp:
            row.append("  ")
            row.append("· network companion", style="muted")
        elif device.is_ble:
            row.append("  ")
            row.append("· Bluetooth companion", style="muted")
        elif device.confidence == "bridge":
            row.append("  ")
            row.append("· serial adapter", style="muted")
        # Only a network device opts into Delete-to-remove: it's listed solely from its
        # remembered endpoint, so forgetting it is the only way it leaves the picker. A scanned
        # serial/BLE device would just reappear, so Delete stays inert on those rows.
        items.append(Choice(title=row, value=device, deletable=device.is_tcp))
    items.extend(_action_rows())
    return items


def _action_rows() -> list:
    """The trailing splash rows: name a network device by hand, then quit.

    A network (TCP) companion doesn't advertise and isn't attached, so it can't be scanned
    for — the "add a network device" row opens a host:port prompt to name one. It's flagged
    experimental (TCP companion support is still settling), with the tag in the cautionary
    hue so it reads as a caveat rather than a description. The Quit row mirrors the main menu.
    The leading spaces line both up under the device-name column.
    """
    add_row = Text()
    add_row.append(f"  {_TCP_ICON} Add a network device…")
    add_row.append("  · experimental", style="warn")
    return [
        Separator(" "),
        Choice(title=add_row, value=_ADD_TCP),
        Choice(title="  🚪 Quit", value=_QUIT),
    ]
