# SPDX-License-Identifier: Apache-2.0
"""The ``diagnostics`` tool: everything a bug report needs, in one block worth pasting.

A report that opens with "it crashed" costs two or three round trips before anyone knows
which MeshTerm, which OS, which terminal, which radio — and the person who hit the bug is
usually the one least able to answer, because half of it is resolved at boot and never
shown. This states all of it at once.

**What it deliberately does not say.** The block is designed to be pasted in public by
somebody who has not read it, so nothing that identifies a person or unlocks anything may
be in it: no pairing PIN, no admin password, no channel secret, no private key, no
position, and no contact's name or key. The mesh is described in **aggregate** — how many
rows of each kind, and the span of time they cover — which is the half that explains a bug
(a database with four observations and one with four hundred thousand fail differently)
without naming anyone the reporter talks to. :func:`~meshterm.core.hostinfo.host` and
:meth:`~meshterm.persistence.repository.Repository.table_counts` hold that line on their
own side, so it cannot be crossed here by accident.

**One report, three faces**, which is the whole reason this is a report and not a
``print``: the menu draws it on a bare frame with nothing around it to select by mistake
(see :mod:`meshterm.ui.diagnostics`), ``meshterm diagnostics`` prints the same rows for
anyone who would rather redirect than copy, and ``--json`` hands the same facts to
whatever files the issue.

The device is asked but never required. A report about a radio that will not connect is
exactly the report that most needs to be filed, so a failure to reach it becomes a fact in
the block (``connected``/``error``) rather than an error that replaces it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..context import AppContext
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Report

#: MeshCore advert-type byte -> the role a person calls it. Duplicated from ``info``
#: rather than shared: that module's copy is part of the device-status answer, and a
#: cross-import between two tools to save five entries buys a dependency and no clarity.
_ROLES = {1: "client", 2: "repeater", 3: "room server", 4: "sensor"}


@register
class DiagnosticsTool(Tool):
    """What MeshTerm, this machine, this terminal and this radio actually are."""

    name = "diagnostics"
    title = "Diagnostics"
    icon = "🩺"
    help = "Version, host, terminal and device facts for a bug report"
    category = "This app"
    order = 7  # after Preferences (the row that changes MeshTerm), before the pages

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the page in the menu; returning ``None`` completes with no run row.

        The same shape the About pages use: the menu's whole interaction *is* the screen,
        and a run row recording that somebody looked at their own version number would be
        a line in the log for every glance.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None`` in the menu; the CLI never reaches this.
        """
        from ..ui.diagnostics import open_diagnostics_page

        await open_diagnostics_page(ctx, await build_report(ctx))
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """State the diagnostics — only reachable from the CLI (the menu opens the screen).

        Args:
            ctx: Shared application context.
            params: Unused beyond the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` carrying the whole block as its report.
        """
        return ToolResult(summary={"tool": self.name}, report=await build_report(ctx))


async def build_report(ctx: AppContext) -> Report:
    """Gather every diagnostic fact, as the report both faces render.

    Five blocks in the order a reader needs them: what this program is, what it is running
    on, what it is drawing into, what radio it found, and how much history it has. The
    three :class:`~meshterm.ui.report.Facts` blocks merge into one flat JSON object and the
    two :class:`~meshterm.ui.report.Listing` blocks keep a key of their own, which is also
    why the table counts are a listing — a table named after a fact would otherwise be able
    to collide with it.

    Args:
        ctx: Shared application context.

    Returns:
        The five-block report.
    """
    return (
        _app_facts(),
        _host_facts(),
        await _device_facts(ctx),
        _mesh_facts(ctx),
        _table_listing(ctx),
        _preference_listing(ctx),
    )


def _app_facts() -> Any:
    """What this program is: version, how it was installed, and where it keeps things."""
    from .. import __version__
    from ..core import hostinfo
    from ..ui import fields
    from ..ui.report import Facts

    return Facts(
        key="meshterm",
        fields=(
            fields.word("meshterm", "meshterm"),
            fields.word("install", "install"),
        ),
        values={"meshterm": __version__, "install": hostinfo.install_kind()},
    )


def _host_facts() -> Any:
    """The machine and the terminal, including every verdict resolved once at boot.

    The boot-time verdicts are the point of this block. Whether icons draw, whether the
    path widget's separators draw, which platform flavour resolved and *why* — each is
    decided from the environment before the first frame, none is visible on any screen,
    and each is the answer to a whole family of "it looks wrong" reports.
    """
    from ..core import hostinfo
    from ..platforms import get_platform
    from ..ui import fields
    from ..ui.report import Facts
    from ..ui.termfont import detect_terminal_font, emoji_support, powerline_support

    machine, term = hostinfo.host(), hostinfo.terminal()
    emoji = emoji_support()
    powerline = powerline_support()
    font = detect_terminal_font()
    return Facts(
        key="host",
        fields=(
            fields.word("os", "os"),
            fields.word("os_build", "os_build"),
            fields.word("arch", "arch"),
            fields.word("python", "python"),
            fields.word("terminal", "terminal"),
            fields.word("term", "term"),
            fields.word("colorterm", "colorterm"),
            fields.word("terminal_size", "terminal_size"),
            fields.flag("over_ssh", "over_ssh"),
            fields.word("platform", "platform"),
            fields.flag("icons", "icons"),
            fields.word("icons_source", "icons_source"),
            fields.word("font", "font"),
            fields.word("powerline", "powerline"),
        ),
        values={
            "os": machine.os,
            "os_build": machine.os_build,
            "arch": machine.arch,
            "python": machine.python,
            "terminal": term.program,
            "term": term.term,
            "colorterm": term.colorterm,
            "terminal_size": f"{term.cols}x{term.rows}",
            "over_ssh": term.over_ssh,
            "platform": get_platform().name,
            "icons": emoji.supported,
            "icons_source": emoji.source,
            # The face, and which terminal it was read from — an unidentified terminal
            # (or an ssh session, where the font lives on the far client) has no answer,
            # and saying so is worth more than naming a font nobody here can see.
            "font": f"{font.face} ({font.source})" if font else None,
            # Level and source together: `none (font:windows-terminal)` is a measurement
            # and `unknown (ssh)` is an admission, and they lead somewhere different.
            "powerline": f"{powerline.level} ({powerline.source})",
        },
    )


async def _device_facts(ctx: AppContext) -> Any:
    """The radio: what it is, how it is attached, and what its link is set to.

    Best-effort by design. Every value here is ``None`` on a machine whose radio is
    unplugged, asleep, or refusing to pair — and ``connected: false`` with the failure's
    own words in ``error`` is a better first line for that bug report than a command that
    refused to produce one.
    """
    from ..ui import fields
    from ..ui.report import Facts

    values: dict[str, Any] = dict.fromkeys(
        (
            "connected",
            "transport",
            "port",
            "device_role",
            "device_model",
            "firmware",
            "radio_freq_mhz",
            "radio_bw_khz",
            "radio_sf",
            "radio_cr",
            "tx_power_dbm",
            "error",
        )
    )
    values["connected"] = False
    try:
        device = await ctx.device()
        snapshot = await _snapshot(ctx, device)
        info = await _device_info(device)
        adv_type = snapshot.get("adv_type")
        values.update(
            connected=True,
            transport="mock" if ctx.mock else (ctx.active_transport or "serial"),
            port=ctx.active_port,
            device_role=(
                _ROLES.get(int(adv_type), f"type {adv_type}") if adv_type is not None else None
            ),
            device_model=info.get("model") or None,
            firmware=" ".join(str(info[k]) for k in ("ver", "fw_build") if info.get(k)) or None,
            radio_freq_mhz=snapshot.get("radio_freq"),
            radio_bw_khz=snapshot.get("radio_bw"),
            radio_sf=snapshot.get("radio_sf"),
            radio_cr=snapshot.get("radio_cr"),
            tx_power_dbm=snapshot.get("tx_power"),
        )
    except Exception as exc:  # noqa: BLE001 - any failure to reach the radio is the fact
        values["error"] = str(exc) or exc.__class__.__name__

    return Facts(
        key="device",
        fields=(
            fields.flag("connected", "connected"),
            fields.word("transport", "transport"),
            fields.word("port", "port"),
            fields.word("device_role", "device_role"),
            fields.word("device_model", "device_model"),
            fields.word("firmware", "firmware"),
            fields.decimal("radio_freq_mhz", "radio_freq_mhz", ".4f"),
            fields.decimal("radio_bw_khz", "radio_bw_khz", ".2f"),
            fields.integer("radio_sf", "radio_sf"),
            fields.integer("radio_cr", "radio_cr"),
            fields.integer("tx_power_dbm", "tx_power_dbm"),
            fields.free("error", "error"),
        ),
        values=values,
    )


def _mesh_facts(ctx: AppContext) -> Any:
    """How much history this install holds, and where it holds it.

    Counts and a span, never a row. The span is the fact that sizes every other number
    here: a thousand observations over two years and a thousand in an afternoon describe
    two different installs, and only one of them has a radio worth suspecting.
    """
    from ..core.preferences import current
    from ..persistence.logging import log_path
    from ..ui import fields
    from ..ui.report import Facts

    preferences = ctx.preferences or current()
    db_path = ctx.settings.db_path
    first, last = ctx.repo.observation_span()
    return Facts(
        key="mesh",
        fields=(
            fields.path("config_dir", "config_dir"),
            fields.integer("db_size_kb", "db_size_kb"),
            fields.word("log_level", "log_level"),
            fields.integer("log_size_kb", "log_size_kb"),
            fields.integer("runs_failed", "runs_failed"),
            fields.instant("first_heard", "first_heard"),
            fields.instant("last_heard", "last_heard"),
        ),
        values={
            "config_dir": ctx.settings.config_dir,
            "db_size_kb": _size_kb(db_path),
            "log_level": preferences.log_level,
            # How much the log holds, so a maintainer asking for it knows what they are
            # asking for — and so a file still at zero says the level never let it write.
            "log_size_kb": _size_kb(log_path(ctx.settings.config_dir)),
            "runs_failed": ctx.repo.failed_run_count(),
            "first_heard": first,
            "last_heard": last,
        },
    )


def _table_listing(ctx: AppContext) -> Any:
    """Every table in the database and how many rows it holds.

    A listing rather than more facts, for two reasons. The keys are discovered from the
    schema rather than declared, so a table named after a fact could otherwise collide
    with it in the merged JSON object; and this genuinely is a set of records — one per
    table — which is the shape a reader and a parser both already know.
    """
    from ..ui import fields
    from ..ui.report import Column, Listing

    counts = ctx.repo.table_counts()
    return Listing(
        key="tables",
        columns=(fields.word("table", "TABLE"), Column(key="rows", lanes=(_rows_lane(),))),
        rows=[{"table": name, "rows": n} for name, n in counts.items()],
    )


def _preference_listing(ctx: AppContext) -> Any:
    """The preferences that differ from their defaults — the whole list, and only those.

    An override is the only part of the preference set that can explain anything: the
    defaults are in the source, the same for everyone, and dumping all forty would bury
    the two the reporter actually changed.
    """
    from ..core.preferences import current
    from ..ui import fields
    from ..ui.report import Listing

    preferences = ctx.preferences or current()
    return Listing(
        key="preferences",
        columns=(fields.word("preference", "PREFERENCE"), fields.free("value", "VALUE")),
        rows=[
            {"preference": key, "value": str(value)}
            for key, value in preferences.overrides().items()
        ],
    )


def _rows_lane() -> Any:
    """The row-count lane: right-aligned, because a column of magnitudes only compares aligned."""
    from ..ui.report import Lane

    return Lane(header="ROWS", render=lambda v: str(v if v is not None else 0), align="right")


def _size_kb(path: Any) -> int | None:
    """A file's size in whole kilobytes, or ``None`` when it is not there to measure."""
    if path is None:
        return None
    try:
        return round(path.stat().st_size / 1024)
    except OSError:
        return None


async def _snapshot(ctx: AppContext, device: Any) -> dict:
    """The device's settings snapshot, or an empty one when the read fails."""
    from ..ui.config_editor import cached_snapshot

    try:
        return await cached_snapshot(ctx, device)
    except Exception:  # noqa: BLE001 - a settings read that fails leaves the radio facts blank
        return {}


async def _device_info(device: Any) -> dict:
    """The device's self-description, or an empty one when the firmware does not answer."""
    try:
        return await device.get_device_info() or {}
    except Exception:  # noqa: BLE001 - firmware predating the query contributes nothing
        return {}
