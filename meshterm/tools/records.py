"""The Trophy case tool: the record-setting walks the trace tools have turned up.

The menu face opens the browser (:mod:`meshterm.ui.records_screen`): every discipline's
records — longest distance, farthest node, most nodes (with and without revisits),
weakest surviving link, biggest loop — each under its own description, opened for their
full stats, re-walked in Trace path, or deleted. Records are *earned* on the trace
screens, where every walk that comes home is scored and offered to the boards (see
:mod:`meshterm.services.records`); this tool never transmits.

The CLI face prints the stored boards: ``meshterm records`` reads the tables so a script
(or a curious shell) can see the standings without a device attached.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..services.records import CATEGORIES, CATEGORY_BY_ID
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..persistence.repository import DiscoveredPath
    from ..services.records import Category


@register
class TrophyCaseTool(Tool):
    """The record-setting mesh walks the trace tools scored, browsed or listed."""

    name = "records"
    title = "Trophy case"
    icon = "🏆"
    help = "Record-setting walks, scored from every trace"
    category = "Explore"
    order = 40  # what the two walks above it score into

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the browser; returning ``None`` completes the invocation with no run row.

        The screen reads stored records and transmits nothing (and logs no rows of its
        own), so it lives in :meth:`prompt_params` and needs no logged run — the mesh walk /
        dashboard pattern.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.records_screen import open_records

        await open_records(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print the stored boards — only reachable from a scripted call (never transmits).

        Args:
            ctx: Shared application context.
            params: ``category``/``width`` filters and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the record count.
        """
        return self._show_records(
            ctx,
            category=params.get("category"),
            width=params.get("width"),
        )

    def _show_records(
        self, ctx: AppContext, *, category: str | None, width: int | None
    ) -> ToolResult:
        """Print the stored record boards (no device, no transmissions).

        Args:
            ctx: Shared application context.
            category: Only this category id, or ``None`` for every discipline.
            width: Only this hash width, or ``None`` for every width.

        Returns:
            A :class:`ToolResult` with the record count.
        """
        from ..services import trace_runner
        from ..ui import script

        wanted = [CATEGORY_BY_ID[category]] if category in CATEGORY_BY_ID else CATEGORIES
        # Names come from stored history alone — no contacts, because this command reads
        # the database and must work with no radio attached at all.
        resolve = trace_runner.make_node_resolver(None, ctx.repo.node_names())

        table = script.columns(
            "CATEGORY",
            "WIDTH",
            "SCORE",
            "UNIT",
            "RECORDED",
            "VERSION",
            "ROUTE",
            right=("WIDTH", "SCORE"),
        )
        total = 0
        for cat in wanted:
            rows = ctx.repo.discoveries(cat.id, width_bytes=width)
            rows.sort(key=lambda r: (r.width_bytes, r.score if cat.ascending else -r.score))
            for record in rows:
                table.add_row(
                    cat.id,
                    str(record.width_bytes),
                    _score(cat, record),
                    cat.unit,
                    script.stamp(record.discovered_at),
                    record.app_version,
                    _route(record, resolve),
                )
                total += 1
        if total:
            ctx.ui.show(table)
        return ToolResult(
            summary={"records": total},
            message=f"{total} record{'s' if total != 1 else ''} stored",
            exit_code=exitcodes.OK if total else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``records`` subcommand (read-only record listing).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help="List the record-setting walks (reads the database; never transmits)",
        )
        def _records(
            category: str | None = typer.Option(
                None,
                "--category",
                "-c",
                help="Only this discipline (long_haul, far_point, long_leg, "
                "grand_tour, clean_trail, thin_thread, big_loop)",
            ),
            width: int | None = typer.Option(
                None, "--width", "-w", help="Only records at this hash width (1, 2, or 4)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {}
            if category:
                tool_params["category"] = category
            if width:
                tool_params["width"] = width
            run_tool_command(self, tool_params)


def _score(category: Category, record: DiscoveredPath) -> str:
    """A record's score as a bare number, in the category's unit (which is its own column).

    ``format_score`` writes ``273.0 km`` for a reader; the unit belongs in the ``UNIT``
    column here so the ``SCORE`` column is one comparable number per row.

    The one qualifier that survives is the ``>=`` on a Longest-distance walk whose
    kilometre total is incomplete — some hop on it has no known position, so the distance
    is a floor rather than a measurement. Dropping it would turn a lower bound into a
    claim.

    Args:
        category: The discipline the record was set in.
        record: The record.

    Returns:
        The score, prefixed ``>=`` where it is a lower bound.
    """
    if category.unit == "nodes":
        value = str(int(record.score))
    elif category.unit == "dB":
        value = f"{record.score:+.1f}"
    else:
        value = f"{record.score:.1f}"
    incomplete = category.id == "long_haul" and not record.stats.get("km_complete", True)
    return f">={value}" if incomplete else value


def _route(record: DiscoveredPath, resolve: Callable[[str], str | None]) -> str:
    """A record's walk as the CLI's path line.

    Each hop is named where history knows the node and carries the hash it was actually
    transmitted at — the spec's own hop, at this record's width — so the line can be read
    against the ``spec`` a re-walk would take. Where the two lists disagree in length the
    node id stands in, which is the only identity that hop has.

    Args:
        record: The record whose walk to render.
        resolve: Maps a node id to a friendly name, or ``None`` when unknown.

    Returns:
        The path line (see :func:`meshterm.ui.script.path`).
    """
    from ..ui import script

    spec = record.spec.split(",") if record.spec else []
    hops: list[tuple[str, str | None]] = []
    for index, node in enumerate(record.route):
        addressed = spec[index] if index < len(spec) else node
        hops.append((resolve(node) or node, addressed))
    return script.path(hops)
