"""The repeater-admin screens: configure a remote node over the mesh, editor-style.

The interactive face of the ``repeater-admin`` tool. Picking a node (the shared
credentialed-first picker) and logging in (remembered passwords, the one-time prompt
otherwise) opens an editor deliberately shaped like the local device-configuration
screen — the same setting / value / description lanes, staged ``current → new`` values,
Apply — but speaking the repeater's text CLI (see :mod:`meshterm.core.remote_config`)
instead of the companion's binary protocol, which brings the repeater-only knobs the
local editor never had: TX delay, Direct TX delay, airtime factor, advert intervals.

Because every read is one paced mesh round trip, the editor never bulk-reads on open:
values show what the last read (or the last applied set) said, stamped with age, from
the per-node cache (:class:`~meshterm.core.remote_store.RemoteStore`); the *Read
settings* action refreshes them all under an abortable progress dialog, one paced
``get`` at a time. Apply sends the staged ``set`` commands the same way and folds each
confirmed value straight back into the cache.

Beyond the settings, the action rows cover the box itself — advert, clock sync, change
admin password, reboot, each behind its own floating confirmation — and the **Command
line** opens the readline-style remote CLI (:mod:`meshterm.ui.remote_cli`) for anything
the catalog doesn't spell.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Optional

from rich.cells import cell_len
from rich.text import Text

from ..core.models import Contact
from ..core.remote_config import (
    RemoteSetting,
    parse_reply_value,
    reply_is_error,
    settings_by_category,
)
from .menus import confirm_discard, exit_rows, lane_row, menu_rows, section_heading
from .trace_screen import TracingDialog
from .tui import Choice, Separator
from .tui.spinner import Spinner
from .widgets import _age_seconds, _format_age

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

#: Seconds between spinner frames while a remote command is in flight.
_SPINNER_INTERVAL = 0.12

#: Seconds to wait for one remote reply. Repeaters answer over the mesh — multi-hop
#: routes take seconds — so this is deliberately more patient than a local command.
_REPLY_TIMEOUT_S = 10.0

# Menu action sentinels (distinct from setting keys, which are CLI parameter names).
_CLI = "__cli__"
_READ = "__read__"
_ADVERT = "__advert__"
_CLOCK = "__clock__"
_PASSWORD = "__password__"
_REBOOT = "__reboot__"
_APPLY = "__apply__"
_CANCEL = "__cancel__"


async def open_repeater_admin(ctx: "AppContext") -> Optional[dict[str, Any]]:
    """Run the repeater-admin flow: pick a node, log in, and administer it.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).

    Returns:
        A summary of what happened (for the tool's log), or ``None`` on cancel.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .admin_picker import pick_admin_node
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("repeater admin is only available in the menu")
    session = ctx.ui.session

    device = await ctx.device()
    contacts = await device.get_contacts()
    node = await pick_admin_node(
        ctx,
        contacts,
        title="Repeater admin — node to manage",
        prompt="The remote node to set up (you need its admin password):",
    )
    if node is None:
        return None
    if not await _login(ctx, device, node):
        return None
    return await _admin_session(ctx, device, node)


async def _login(ctx: "AppContext", device: "Device", node: Contact) -> bool:
    """Log in to ``node``: remembered password silently, else one floating prompt.

    A working password is remembered; a rejected one is forgotten so the next attempt
    asks fresh (the app-wide remote-admin convention).
    """
    session = ctx.ui.session
    password = ctx.admin_store.get(node)
    prompted = password is None
    if password is None:
        password = await session.text(
            f"Admin password for {node.name}",
            prompt="The node ignores admin commands without a login.",
            password=True,
        )
        if not password:
            return False
    async with ctx.ui.busy_overlay():
        accepted = await device.admin_login(node, password)
    if not accepted:
        ctx.admin_store.forget(node)
        await session.message_dialog(
            Text(
                f"{node.name!r} rejected the admin login (wrong password?). "
                + ("" if prompted else "The saved password was cleared; ")
                + "try again to enter a new one.",
                style="err",
            ),
            title="Admin login",
        )
        return False
    ctx.admin_store.remember(node, password)
    return True


async def _admin_session(
    ctx: "AppContext", device: "Device", node: Contact
) -> dict[str, Any]:
    """Run the editor loop for one logged-in node (the persistent-backdrop pattern)."""
    from .tui import CANCEL, SelectScreen

    session = ctx.ui.session
    loop = asyncio.get_running_loop()

    pending: dict[str, str] = {}  # setting key -> staged new value
    applied = 0
    cursor: Any = None  # the row to re-highlight, so the menu reopens where you left it

    while True:
        cache = ctx.remote_store.settings(node)
        title, items = _menu_items(node, cache, pending)
        menu = SelectScreen(
            title, items, default=cursor, wrap=False,
            footer_hint="↑↓ move · type to filter · Enter select · Esc back",
        )
        menu.future = loop.create_future()
        session.push(menu)
        try:
            choice = await menu.future
            if choice is CANCEL:
                choice = _CANCEL
            if choice not in (None, _CANCEL):
                cursor = choice

            if choice in (None, _CANCEL):
                if pending and not await confirm_discard(ctx, len(pending), verb="sending"):
                    continue
                return {"node": node.name, "applied": applied}
            if choice == _APPLY:
                applied += await _apply(ctx, device, node, pending)
            elif choice == _READ:
                await _read_all(ctx, device, node)
            elif choice == _CLI:
                await _command_line(ctx, device, node)
            elif choice == _ADVERT:
                await _simple_action(
                    ctx, device, node, "advert",
                    title="Send advert",
                    prompt=f"Ask {node.name} to announce itself (flood) now?",
                    commit="Send",
                )
            elif choice == _CLOCK:
                await _simple_action(
                    ctx, device, node, "clock sync",
                    title="Sync clock",
                    prompt=f"Set {node.name}'s clock from this companion's time?",
                    commit="Sync",
                )
            elif choice == _PASSWORD:
                await _change_password(ctx, device, node)
            elif choice == _REBOOT:
                await _simple_action(
                    ctx, device, node, "reboot",
                    title="Reboot node",
                    prompt=f"Reboot {node.name} now? It drops off the mesh while booting.",
                    commit="Reboot",
                    danger=True,
                )
            else:  # a setting key
                await _stage_setting(ctx, str(choice), cache, pending)
        finally:
            session.pop(menu)


# --- the menu ------------------------------------------------------------------------


def _menu_items(
    node: Contact, cache: dict, pending: dict[str, str]
) -> tuple[str, list]:
    """Build the editor menu's title and rows for the cache + staged state.

    The same lane layout as the local device-configuration editor — setting, value
    (with any staged ``→ new``), description under one header line — so administering
    a remote node reads exactly like configuring the local one.
    """
    sections: list[tuple[str, list[tuple[str, Text, str, Any]]]] = []
    for category, specs in settings_by_category():
        rows: list[tuple[str, Text, str, Any]] = []
        for spec in specs:
            rows.append((spec.label, _value_text(spec, cache, pending), spec.help, spec.key))
        sections.append((category, rows))

    label_w = max(cell_len(label) for _, rows in sections for label, _, _, _ in rows)
    value_w = max(cell_len(value.plain) for _, rows in sections for _, value, _, _ in rows)

    items: list = [
        Separator(
            "  " + "SETTING".ljust(label_w + 2) + "VALUE".ljust(value_w + 2) + "DESCRIPTION"
        )
    ]
    for category, rows in sections:
        items.append(section_heading(category))
        for label, value, help_text, key in rows:
            items.append(
                Choice(title=lane_row(label, value, help_text, label_w, value_w), value=key)
            )

    items.append(section_heading("Actions"))
    items.extend(
        menu_rows(
            [
                ("↻ Read settings", "Fetch every value from the node, one paced get", _READ),
                ("⌨ Command line…", "Talk to the node's CLI directly", _CLI),
                ("📡 Send advert…", "Have the node announce itself now", _ADVERT),
                ("🕒 Sync clock…", "Set the node's clock over the mesh", _CLOCK),
                ("🔐 Admin password…", "Change the node's admin password", _PASSWORD),
                ("🔄 Reboot node…", "Restart it remotely", _REBOOT),
            ]
        )
    )

    staged = len(pending)
    items.extend(exit_rows(staged, apply_value=_APPLY, back_value=_CANCEL))

    title = f"Repeater admin — {node.name}" + (f" · {staged} staged" if staged else "")
    return title, items


def _value_text(spec: RemoteSetting, cache: dict, pending: dict[str, str]) -> Text:
    """One setting's VALUE lane: cached value (with its age), and any staged arrow."""
    cached = cache.get(spec.key)
    if not spec.readable:
        value = Text("write-only", style="faint")
    elif cached is None:
        value = Text("?", style="muted")
    else:
        value = Text(cached.value)
        age = _format_age(_age_seconds(cached.read_at))
        if age != "now":
            value.append(f" ·{age}", style="faint")
    if spec.key in pending:
        value.append(f" → {pending[spec.key]}", style="warn")
    return value


# --- staging and applying ---------------------------------------------------------


async def _stage_setting(
    ctx: "AppContext", key: str, cache: dict, pending: dict[str, str]
) -> None:
    """Prompt for one setting's new value and stage it (nothing is sent yet)."""
    from ..core.remote_config import get_setting, validate_value

    spec = get_setting(key)
    if spec is None or not spec.writable:  # pragma: no cover - menu offers only real keys
        return
    cached = cache.get(key)
    current = pending.get(key, cached.value if cached is not None else "")

    if spec.kind == "bool":
        picked = await ctx.ui.dialog(
            spec.help,
            [("Off", "off"), ("On", "on")],
            title=spec.label,
            default=1 if current == "on" else 0,
            keys={"0": "off", "1": "on", "n": "off", "y": "on"},
        )
        if picked is None:
            return
        value = str(picked)
    else:
        hint = ""
        if spec.minimum is not None and spec.maximum is not None:
            hint = f"Allowed: {spec.minimum:g} – {spec.maximum:g}"
        elif spec.minimum is not None:
            hint = f"Allowed: ≥ {spec.minimum:g}"
        if spec.unit:
            hint = f"{hint}  ({spec.unit})" if hint else f"In {spec.unit}."
        raw = await ctx.ui.text(
            spec.label,
            prompt=spec.help,
            default=str(current),
            validate=lambda t: validate_value(spec, t),
            help_text=hint,
        )
        if raw is None:
            return
        value = raw.strip()

    if cached is not None and value == cached.value:
        pending.pop(key, None)  # back to what the node last said — nothing to send
    else:
        pending[key] = value


async def _apply(
    ctx: "AppContext", device: "Device", node: Contact, pending: dict[str, str]
) -> int:
    """Send every staged ``set`` command, paced, under an abortable progress dialog.

    Each confirmed value folds straight into the per-node cache; a rejected or
    unanswered set stays staged so it can be retried (or unstaged) rather than being
    silently dropped. Records one ``runs`` row for the batch.

    Returns:
        How many settings the node accepted.
    """
    from ..core.remote_config import get_setting

    session = ctx.ui.session
    run_id = ctx.repo.start_run(
        "repeater-admin",
        {"node": node.name, "mode": "apply", "settings": sorted(pending)},
        ctx.profile_name,
    )
    outcomes: list[Text] = []
    applied = 0

    async def work(dialog: TracingDialog) -> None:
        nonlocal applied
        total = len(pending)
        for i, (key, value) in enumerate(sorted(pending.items()), start=1):
            spec = get_setting(key)
            assert spec is not None
            dialog.status = f"set {key} · {i}/{total}"
            session.invalidate()
            reply = await device.send_remote_command(
                node, spec.set_command(value), timeout=_REPLY_TIMEOUT_S
            )
            if reply is not None and not reply_is_error(reply):
                ctx.remote_store.remember_setting(node, key, value)
                pending.pop(key, None)
                applied += 1
                outcomes.append(Text.assemble(("✓ ", "ok"), f"{spec.label} = {value}"))
            elif reply is None:
                outcomes.append(Text.assemble(
                    ("? ", "warn"), f"{spec.label} — no reply (still staged)"
                ))
            else:
                outcomes.append(Text.assemble(
                    ("✗ ", "err"), f"{spec.label} — {reply.strip()} (still staged)"
                ))
            if i < total:
                await asyncio.sleep(ctx.settings.trace_cooldown_s)

    aborted = await _run_under_dialog(ctx, f"Applying — {node.name}", work)
    ctx.repo.finish_run(
        run_id, "error" if aborted else "ok",
        {"applied": applied, "staged_left": len(pending)},
    )
    summary = Text.assemble(
        (f"{applied}", "brand"), f" of {applied + len(pending)} settings applied"
    )
    if aborted:
        summary.append("  (aborted — the rest stay staged)", style="warn")
    await ctx.ui.session.message_dialog(
        Text("\n").join([summary, Text(), *outcomes]) if outcomes else summary,
        title=f"Apply — {node.name}",
    )
    return applied


async def _read_all(ctx: "AppContext", device: "Device", node: Contact) -> None:
    """Refresh every readable setting from the node, one paced ``get`` at a time.

    Values that parse land in the cache (and on screen); firmware that doesn't know a
    key just leaves that row unknown. Abort keeps everything already read.
    """
    session = ctx.ui.session
    specs = [s for cat, ss in settings_by_category() for s in ss if s.readable]
    run_id = ctx.repo.start_run(
        "repeater-admin", {"node": node.name, "mode": "read"}, ctx.profile_name
    )
    read = 0

    async def work(dialog: TracingDialog) -> None:
        nonlocal read
        for i, spec in enumerate(specs, start=1):
            dialog.status = f"get {spec.key} · {i}/{len(specs)}"
            session.invalidate()
            reply = await device.send_remote_command(
                node, spec.get_command, timeout=_REPLY_TIMEOUT_S
            )
            value = parse_reply_value(spec, reply)
            if value is not None:
                ctx.remote_store.remember_setting(node, spec.key, value)
                read += 1
            if i < len(specs):
                await asyncio.sleep(ctx.settings.trace_cooldown_s)

    aborted = await _run_under_dialog(ctx, f"Reading — {node.name}", work)
    ctx.repo.finish_run(
        run_id, "error" if aborted else "ok", {"read": read, "asked": len(specs)}
    )


async def _run_under_dialog(ctx: "AppContext", title: str, work) -> bool:
    """Run an async remote batch under a floating spinner dialog with Abort.

    Args:
        ctx: The shared application context.
        title: The dialog's heading.
        work: ``async work(dialog)`` performing the batch, updating ``dialog.status``.

    Returns:
        ``True`` if the user aborted, ``False`` if the batch ran to completion.
    """
    session = ctx.ui.session
    spinner = Spinner()
    dialog = TracingDialog(title, spinner=spinner, on_abort=lambda: None)
    dialog.show_last = False

    task = asyncio.ensure_future(work(dialog))
    dialog.on_abort = task.cancel

    async def animate() -> None:
        while True:
            await asyncio.sleep(_SPINNER_INTERVAL)
            spinner.tick()
            session.invalidate()

    ticker = asyncio.ensure_future(animate())
    session.push(dialog)
    aborted = False
    try:
        await task
    except asyncio.CancelledError:
        aborted = True
    except Exception as exc:  # noqa: BLE001 - surface in a dialog, keep the screen
        await session.message_dialog(Text(str(exc), style="err"), title=title)
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - a spinner hiccup must never break the batch
            pass
        session.pop(dialog)
    return aborted


# --- one-shot actions ------------------------------------------------------------


async def _simple_action(
    ctx: "AppContext",
    device: "Device",
    node: Contact,
    command: str,
    *,
    title: str,
    prompt: str,
    commit: str,
    danger: bool = False,
) -> None:
    """Confirm and send one fixed CLI command, showing the node's reply."""
    choice = await ctx.ui.dialog(
        prompt,
        [("Cancel", None), (commit, "go")],
        title=title,
        default=1,
        danger=danger,
    )
    if choice != "go":
        return
    async with ctx.ui.busy_overlay():
        reply = await device.send_remote_command(node, command, timeout=_REPLY_TIMEOUT_S)
    if reply is None:
        body = Text("no reply — the command may still have landed", style="warn")
    else:
        body = Text(reply.strip(), style="err" if reply_is_error(reply) else "ok")
    await ctx.ui.session.message_dialog(body, title=title)


async def _change_password(ctx: "AppContext", device: "Device", node: Contact) -> None:
    """Change the node's admin password (and re-remember it on success)."""
    new = await ctx.ui.text(
        f"New admin password for {node.name}",
        prompt="Sent over the mesh; the node applies it immediately.",
        password=True,
    )
    if not new:
        return
    confirmed = await ctx.ui.dialog(
        f"Change {node.name}'s admin password now? You will need the new one everywhere.",
        [("Cancel", None), ("Change", "go")],
        title="Admin password",
        default=1,
        danger=True,
    )
    if confirmed != "go":
        return
    async with ctx.ui.busy_overlay():
        reply = await device.send_remote_command(
            node, f"password {new}", timeout=_REPLY_TIMEOUT_S
        )
    if reply is not None and not reply_is_error(reply):
        ctx.admin_store.remember(node, new)  # the working password just changed
        body = Text("✓ password changed and remembered", style="ok")
    elif reply is None:
        body = Text(
            "no reply — the change may or may not have landed; the old password "
            "stays remembered until a login proves otherwise",
            style="warn",
        )
    else:
        body = Text(reply.strip(), style="err")
    await ctx.ui.session.message_dialog(body, title="Admin password")


# --- the command line ---------------------------------------------------------------


async def _command_line(ctx: "AppContext", device: "Device", node: Contact) -> None:
    """Open the readline-style remote CLI for ``node`` (history persists per node)."""
    from .remote_cli import RemoteCliScreen

    session = ctx.ui.session
    worker: Optional[asyncio.Task] = None
    ticker: Optional[asyncio.Task] = None

    def send(command: str) -> None:
        nonlocal worker
        if worker is not None and not worker.done():
            return  # one command in flight at a time — every send is a transmission
        screen.sent(command)
        ctx.remote_store.append_history(node, command)
        worker = asyncio.ensure_future(roundtrip(command))

    async def roundtrip(command: str) -> None:
        try:
            reply = await device.send_remote_command(
                node, command, timeout=_REPLY_TIMEOUT_S
            )
        except asyncio.CancelledError:
            screen.failed("cancelled", error=False)
            raise
        except Exception as exc:  # noqa: BLE001 - shown inline, the screen stays up
            screen.failed(str(exc), error=True)
            return
        if reply is None:
            screen.failed(f"no reply within {_REPLY_TIMEOUT_S:.0f} s")
        else:
            screen.reply(reply)

    screen = RemoteCliScreen(
        node_label=node.name,
        history=ctx.remote_store.history(node),
        send=send,
        session=session,
    )

    async def animate() -> None:
        while True:
            await asyncio.sleep(_SPINNER_INTERVAL)
            if screen.busy:
                screen.tick()
                session.invalidate()

    ticker = asyncio.ensure_future(animate())
    try:
        await session.run_screen(screen)
    finally:
        for task in (worker, ticker):
            if task is not None and not task.done():
                task.cancel()
        for task in (worker, ticker):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - the screen is closed; nothing to surface
                    pass
