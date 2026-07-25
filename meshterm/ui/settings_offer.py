"""The connect-time offer to reconcile a device's settings with what MeshTerm remembers.

A firmware-less radio bridge forgets its configuration whenever it restarts, so the settings you
changed through MeshTerm last session come back as firmware defaults. Unlike channels — silently
replayed because filling an empty slot overwrites nothing — a setting is a single canonical
value, so restoring a stale one could clobber a change made elsewhere. This surface therefore
*asks* rather than acting: on connect, when the device's current settings drift from what
MeshTerm saved for it, it offers to restore the saved values, adopt the device's current ones, or
stop remembering the device (see :mod:`meshterm.core.settings_store`).

It runs once at startup, on the splash, only when a device is connected and something is actually
remembered for it — so a firmware radio you don't manage through MeshTerm is never interrupted.
"""

from __future__ import annotations

from rich.text import Text

from ..context import AppContext
from ..core.device_config import build_snapshot, get_spec
from ..core.settings_store import SettingDrift, adopt, restore, settings_drift
from .menus import menu_rows
from .tui import Separator

# The offer's actions, returned by the startup select.
_RESTORE = "restore"
_ADOPT = "adopt"
_FORGET = "forget"


async def offer_remembered_settings(ctx: AppContext) -> None:
    """Offer to reconcile the connected device's settings with MeshTerm's saved copy.

    Best-effort and quiet: does nothing without a live link, without anything remembered for the
    device, or when the device already matches. Never opens the radio itself — a deferred connect
    (``connect_on_start`` off) simply skips this, since ``is_connected`` is false. A failure here
    must not block reaching the menu, so everything is guarded.

    Args:
        ctx: The shared application context.
    """
    store = ctx.settings_store
    if store is None or not ctx.is_connected:
        return
    try:
        device = await ctx.device()
        info = await device.get_self_info()
        pubkey = str(info.get("public_key") or "")
        if not store.settings(pubkey):
            return  # nothing remembered — a firmware radio pays only this cheap probe
        snapshot = await build_snapshot(device)
        drifted = settings_drift(store, pubkey, snapshot)
    except Exception as exc:  # noqa: BLE001 - a probe failure must not block the menu
        ctx.log.debug("settings: drift check on connect failed: %s", exc)
        return
    if not drifted:
        return

    choice = await _prompt(ctx, drifted)
    keys = [d.key for d in drifted]
    try:
        if choice == _RESTORE:
            restored = await restore(store, device, snapshot, keys)
            ctx.devstate.invalidate_config()
            ctx.log.info("settings: restored %d remembered setting(s) to the device", restored)
        elif choice == _ADOPT:
            adopt(store, pubkey, snapshot, keys)
            ctx.log.info("settings: adopted the device's values for %d setting(s)", len(keys))
        elif choice == _FORGET:
            store.forget_all(pubkey)
            ctx.log.info("settings: stopped remembering this device's settings")
    except Exception as exc:  # noqa: BLE001 - acting on the choice must not block the menu
        ctx.log.debug("settings: reconciling on connect failed: %s", exc)


async def _prompt(ctx: AppContext, drifted: list[SettingDrift]) -> object:
    """Show the startup offer and return the chosen action, or ``None`` if dismissed."""
    from .. import copyright_notice
    from .logo import load_logo

    count = len(drifted)
    plural = "" if count == 1 else "s"
    rows = menu_rows(
        [
            (
                "📂 Restore my saved settings",
                f"Write the {count} saved value{plural} back onto the device",
                _RESTORE,
            ),
            (
                "💾 Keep the device's settings",
                "Update my saved copy to match the device now",
                _ADOPT,
            ),
            (
                "🗑 Stop remembering this device",
                "Forget the saved settings and don't ask again",
                _FORGET,
            ),
        ]
    )
    items = [
        Separator(
            f"  This device reports {count} setting{plural} that differ from what "
            "you last saved through MeshTerm:"
        ),
        Separator(_drift_summary(drifted)),
        Separator(" "),
        *rows,
    ]
    return await ctx.ui.select_startup(
        "Saved settings differ",
        items,
        default=_RESTORE,
        banner=load_logo(),
        footnote=copyright_notice(),
    )


def _drift_summary(drifted: list[SettingDrift], *, cap: int = 6) -> Text:
    """A muted one-line list of the drifted settings' labels, capped with a ``+N more`` tail."""
    labels = [get_spec(d.key).label for d in drifted]
    shown = labels[:cap]
    text = "  " + ", ".join(shown)
    if len(labels) > cap:
        text += f", +{len(labels) - cap} more"
    return Text(text, style="muted")
