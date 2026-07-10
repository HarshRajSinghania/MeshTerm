"""The ``link-budget`` tool: predict airtime, range, and duty-cycle headroom offline.

Unlike the empirical ``tx-optimize`` sweep, this tool measures nothing on the air — it
answers "*should* I try SF11?" or "how many packets an hour can I legally send?" from the
LoRa math alone. Radio parameters default to the connected device's current config (so it
reads like a what-if on your live setup), but every value can be overridden, and a fully
specified invocation needs no radio at all.
"""

from __future__ import annotations

from typing import Any, Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..services import link_budget
from ..services.link_budget import PATH_LOSS_EXPONENTS, RadioConfig
from ..ui.tui import Choice
from .base import Tool, ToolResult, register


@register
class LinkBudgetTool(Tool):
    """Compute time-on-air, sensitivity, range, and duty-cycle headroom for a radio config."""

    name = "link-budget"
    title = "Link budget"
    help = "Predict airtime, range, and duty-cycle headroom for a radio config"
    category = "Optimization"
    order = 20

    async def prompt_params(self, ctx: AppContext) -> Optional[dict[str, Any]]:
        """Interactively gather payload, geometry, and environment, prefilled from the device.

        Args:
            ctx: Shared application context.

        Returns:
            A parameter dict, or ``None`` if the user cancelled.
        """
        defaults = await self._device_radio(ctx)

        payload = await ctx.ui.text(
            "Payload size (bytes):", default="32", validate=_is_pos_int
        )
        if payload is None:
            return None
        tx = await ctx.ui.text(
            "TX power (dBm):",
            default=str(int(defaults.get("tx_power") or 22)),
            validate=_is_number,
        )
        if tx is None:
            return None
        tx_gain = await ctx.ui.text("TX antenna gain (dBi):", default="2", validate=_is_number)
        if tx_gain is None:
            return None
        rx_gain = await ctx.ui.text("RX antenna gain (dBi):", default="2", validate=_is_number)
        if rx_gain is None:
            return None
        env = await ctx.ui.select(
            "Environment (path-loss model):",
            [Choice(name, name) for name in PATH_LOSS_EXPONENTS],
            default="suburban",
        )
        if env is None:
            return None
        duty = await ctx.ui.text("Duty-cycle limit (%):", default="1.0", validate=_is_number)
        if duty is None:
            return None

        params: dict[str, Any] = {
            "payload": int(payload),
            "tx": float(tx),
            "tx_gain": float(tx_gain),
            "rx_gain": float(rx_gain),
            "env": env,
            "duty": float(duty),
        }
        params.update(defaults.get("radio", {}))
        return params

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Build the radio config, compute the budget, and render it.

        Radio parameters come from ``params`` when fully supplied (so the tool runs with
        no radio attached); any missing value is read from the connected device.

        Args:
            ctx: Shared application context.
            params: ``payload``, ``tx``, ``tx_gain``, ``rx_gain``, ``env``/``exponent``,
                ``duty``, optional ``dwell``/``path_loss``, and the radio parameters
                ``freq``/``bw``/``sf``/``cr`` (else read from the device).

        Returns:
            A :class:`ToolResult` summarizing the budget.
        """
        config = await self._resolve_config(ctx, params)
        exponent = float(
            params.get("exponent", PATH_LOSS_EXPONENTS.get(params.get("env", "suburban"), 3.0))
        )
        budget = link_budget.compute_link_budget(
            config,
            payload_bytes=int(params.get("payload", 32)),
            tx_power_dbm=float(params.get("tx", 22.0)),
            tx_gain_dbi=float(params.get("tx_gain", 2.0)),
            rx_gain_dbi=float(params.get("rx_gain", 2.0)),
            path_loss_exponent=exponent,
            reference_path_loss_db=params.get("path_loss"),
            duty_limit_percent=float(params.get("duty", 1.0)),
            max_dwell_ms=params.get("dwell"),
        )

        ctx.ui.show(_radio_panel(config))
        ctx.ui.show(_budget_panel(budget))

        return ToolResult(
            summary={
                "freq_mhz": config.freq_mhz,
                "bw_khz": config.bw_khz,
                "sf": config.sf,
                "cr_denom": config.cr_denom,
                "payload_bytes": budget.toa.payload_bytes,
                "time_on_air_ms": round(budget.toa.total_ms, 2),
                "bitrate_bps": round(budget.toa.bitrate_bps, 1),
                "sensitivity_dbm": round(budget.sensitivity_dbm, 1),
                "max_path_loss_db": round(budget.max_path_loss_db, 1),
                "range_km": round(budget.range_km, 2),
                "max_packets_per_hour": round(budget.duty.max_packets_per_hour, 1),
                "dwell_ok": budget.duty.dwell_ok,
            },
            message=(
                f"[ok]✓[/ok] SF{config.sf}/BW{config.bw_khz:g} → "
                f"[brand]{budget.toa.total_ms:.0f} ms[/brand] airtime, "
                f"~[brand]{budget.range_km:.1f} km[/brand] range "
                f"({budget.duty.max_packets_per_hour:.0f} pkts/h at "
                f"{budget.duty.limit_percent:g}% duty)"
            ),
        )

    async def _resolve_config(self, ctx: AppContext, params: dict[str, Any]) -> RadioConfig:
        """Build a :class:`RadioConfig` from params, filling gaps from the device.

        Args:
            ctx: Shared application context.
            params: Tool params that may carry ``freq``/``bw``/``sf``/``cr``.

        Returns:
            The resolved radio configuration.
        """
        radio_keys = ("freq", "bw", "sf", "cr")
        if not all(params.get(k) is not None for k in radio_keys):
            info = await (await ctx.device()).get_self_info()
            params = {
                "freq": params.get("freq", info.get("radio_freq")),
                "bw": params.get("bw", info.get("radio_bw")),
                "sf": params.get("sf", info.get("radio_sf")),
                "cr": params.get("cr", info.get("radio_cr")),
                **{k: v for k, v in params.items() if k not in radio_keys},
            }
        return RadioConfig(
            freq_mhz=float(params["freq"]),
            bw_khz=float(params["bw"]),
            sf=int(params["sf"]),
            cr_denom=int(params.get("cr", 5)),
        )

    async def _device_radio(self, ctx: AppContext) -> dict[str, Any]:
        """Read the device's radio config for prompt prefill; tolerate no device.

        Args:
            ctx: Shared application context.

        Returns:
            A dict with a ``radio`` sub-dict (freq/bw/sf/cr) and ``tx_power`` when a
            device is reachable, otherwise empty defaults.
        """
        try:
            info = await (await ctx.device()).get_self_info()
        except Exception:  # noqa: BLE001 - offline use is fully supported; fall back
            return {}
        return {
            "radio": {
                "freq": info.get("radio_freq"),
                "bw": info.get("radio_bw"),
                "sf": info.get("radio_sf"),
                "cr": info.get("radio_cr"),
            },
            "tx_power": info.get("tx_power"),
        }

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``link-budget`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _link_budget(
            freq: Optional[float] = typer.Option(None, "--freq", help="Frequency (MHz)"),
            bw: Optional[float] = typer.Option(None, "--bw", help="Bandwidth (kHz)"),
            sf: Optional[int] = typer.Option(None, "--sf", help="Spreading factor (6-12)"),
            cr: Optional[int] = typer.Option(None, "--cr", help="Coding-rate denom (5-8)"),
            payload: int = typer.Option(32, "--payload", help="Payload size (bytes)"),
            tx: float = typer.Option(22.0, "--tx", help="TX power (dBm)"),
            tx_gain: float = typer.Option(2.0, "--tx-gain", help="TX antenna gain (dBi)"),
            rx_gain: float = typer.Option(2.0, "--rx-gain", help="RX antenna gain (dBi)"),
            env: str = typer.Option(
                "suburban", "--env", help="Path-loss model: free-space/rural/suburban/urban"
            ),
            exponent: Optional[float] = typer.Option(
                None, "--exponent", help="Override the path-loss exponent (2 = free space)"
            ),
            duty: float = typer.Option(1.0, "--duty", help="Duty-cycle limit (%)"),
            dwell: Optional[float] = typer.Option(
                None, "--dwell", help="Max dwell time per TX (ms), e.g. 400 for US915"
            ),
            path_loss: Optional[float] = typer.Option(
                None, "--path-loss", help="A known path loss (dB) to report link margin at"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {
                "payload": payload, "tx": tx, "tx_gain": tx_gain, "rx_gain": rx_gain,
                "env": env, "duty": duty,
            }
            for key, value in (
                ("freq", freq), ("bw", bw), ("sf", sf), ("cr", cr),
                ("exponent", exponent), ("dwell", dwell), ("path_loss", path_loss),
            ):
                if value is not None:
                    tool_params[key] = value
            run_tool_command(self, tool_params)


def _radio_panel(config: RadioConfig) -> Panel:
    """Render the radio configuration being evaluated.

    Args:
        config: The radio configuration.

    Returns:
        A Rich :class:`Panel` describing the config.
    """
    ldr = "on" if config.low_data_rate_enabled() else "off"
    body = Text.assemble(
        ("frequency     ", "muted"), (f"{config.freq_mhz:g} MHz\n", ""),
        ("bandwidth     ", "muted"), (f"{config.bw_khz:g} kHz\n", ""),
        ("spreading     ", "muted"), (f"SF{config.sf}\n", "brand"),
        ("coding rate   ", "muted"), (f"4/{config.cr_denom}\n", ""),
        ("symbol time   ", "muted"), (f"{config.symbol_time_ms():.2f} ms\n", ""),
        ("low data rate ", "muted"), (ldr, ""),
    )
    return Panel(body, title="[accent]RADIO CONFIG[/accent]", border_style="muted", expand=False)


def _budget_panel(budget: link_budget.LinkBudget) -> Panel:
    """Render the computed link budget as a labelled table.

    Args:
        budget: The link budget to display.

    Returns:
        A Rich :class:`Panel` with airtime, sensitivity, range, and duty figures.
    """
    table = Table(box=None, padding=(0, 2, 0, 0), expand=False)
    table.add_column("METRIC", style="muted")
    table.add_column("VALUE", justify="right")

    toa = budget.toa
    table.add_row("payload", f"{toa.payload_bytes} bytes")
    table.add_row("time on air", Text(f"{toa.total_ms:.1f} ms", style="brand"))
    table.add_row("bitrate", f"{toa.bitrate_bps:.0f} bps")
    table.add_section()
    table.add_row("TX EIRP", f"{budget.tx_power_dbm + budget.tx_gain_dbi:.1f} dBm")
    table.add_row("RX sensitivity", f"{budget.sensitivity_dbm:.1f} dBm")
    table.add_row("max path loss", Text(f"{budget.max_path_loss_db:.1f} dB", style="brand"))
    table.add_row(
        f"est. range (n={budget.path_loss_exponent:g})",
        Text(f"{budget.range_km:.2f} km", style="brand"),
    )
    if budget.link_margin_db is not None:
        style = "ok" if budget.link_margin_db >= 0 else "err"
        table.add_row("link margin", Text(f"{budget.link_margin_db:+.1f} dB", style=style))
    table.add_section()
    duty = budget.duty
    table.add_row(f"duty limit ({duty.limit_percent:g}%)", "")
    table.add_row("  max packets/hour", f"{duty.max_packets_per_hour:.0f}")
    table.add_row("  min spacing", f"{duty.min_interval_s:.1f} s")
    if duty.max_dwell_ms is not None:
        ok = duty.dwell_ok
        cell = Text(
            f"{'ok' if ok else 'OVER'} ({duty.max_dwell_ms:g} ms)",
            style="ok" if ok else "err",
        )
        table.add_row("  dwell-time", cell)

    return Panel(table, title="[accent]LINK BUDGET[/accent]", border_style="accent", expand=False)


def _is_pos_int(value: str) -> bool | str:
    """Validate a positive integer for a questionary text answer."""
    try:
        return True if int(value) > 0 else "Enter a number greater than zero."
    except ValueError:
        return "Enter a whole number."


def _is_number(value: str) -> bool | str:
    """Validate a real number for a questionary text answer."""
    try:
        float(value)
        return True
    except ValueError:
        return "Enter a number."
