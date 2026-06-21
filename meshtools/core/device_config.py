"""Declarative registry of every settable device configuration value.

Each :class:`SettingSpec` describes one tunable setting: how to read its current value
from a device snapshot, how to parse/validate/format it, and how to apply it back to the
:class:`~meshtools.core.connection.Device`. The same registry drives the interactive
editor, the ``config`` CLI subcommands, and TOML backup/restore — add a setting once and
it appears everywhere, mirroring the tool registry pattern.

Settings whose protocol command takes several fields at once (radio, coordinates, tuning,
telemetry modes) rebuild the full command from the current snapshot plus the one changed
field, so a single setting can be edited in isolation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Optional

from .connection import Device

# Display categories, in the order the editor and `config show` present them.
CATEGORIES = ("Identity", "Radio", "Tuning", "Behavior", "Experimental")


class DeviceConfigError(ValueError):
    """Raised when a value cannot be parsed or fails validation.

    The message is user-facing (printed directly by the CLI and editor).
    """


@dataclass(slots=True)
class SettingSpec:
    """Specification for one settable configuration value.

    Attributes:
        key: Canonical key (matches the ``SELF_INFO`` field name where one exists).
        label: Human-friendly name for display.
        help: One-line description.
        category: One of :data:`CATEGORIES`.
        value_type: ``"str" | "int" | "float" | "bool" | "enum"``.
        choices: For ``enum``, a mapping of allowed int value to label.
        minimum: Inclusive lower bound for numeric types, if any.
        maximum: Inclusive upper bound for numeric types, if any.
        strict_choices: When ``True`` (the default) an ``enum`` value must be one of
            :attr:`choices`. When ``False`` the choices are offered as a convenience menu
            but any in-range integer is still accepted — used for firmware fields whose
            full value domain we don't enumerate exhaustively.
        getter: Extracts the current value from a snapshot dict.
        apply: Coroutine applying a parsed value to a device, given the snapshot.
    """

    key: str
    label: str
    help: str
    category: str
    value_type: str
    getter: Callable[[dict], Any]
    apply: Callable[[Device, Any, dict], Awaitable[None]]
    choices: Optional[dict[int, str]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    strict_choices: bool = True


# --- value parsing / formatting ----------------------------------------------

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def parse_value(spec: SettingSpec, raw: Any) -> Any:
    """Parse and validate a raw value (typically a CLI/TOML string) for ``spec``.

    Args:
        spec: The target setting.
        raw: The raw value to coerce (string or already-typed scalar).

    Returns:
        The typed, range-checked value ready for :meth:`SettingSpec.apply`.

    Raises:
        DeviceConfigError: If the value is the wrong type or out of range.
    """
    text = str(raw).strip()
    if spec.value_type == "str":
        return text
    if spec.value_type == "bool":
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise DeviceConfigError(f"{spec.key}: expected a boolean, got {raw!r}")

    # Numeric (int / enum / float): wrap only the conversion, so a DeviceConfigError
    # raised by the range/enum checks below is not caught and rewrapped here.
    try:
        if spec.value_type == "float":
            value: Any = float(text)
        else:  # int or enum
            value = int(text, 0) if isinstance(raw, str) else int(raw)
    except (ValueError, TypeError) as exc:
        raise DeviceConfigError(f"{spec.key}: invalid {spec.value_type} value {raw!r}") from exc

    if (
        spec.value_type == "enum"
        and spec.strict_choices
        and spec.choices is not None
        and value not in spec.choices
    ):
        allowed = ", ".join(str(k) for k in spec.choices)
        raise DeviceConfigError(f"{spec.key}: must be one of {allowed}, got {value}")
    if spec.minimum is not None and value < spec.minimum:
        raise DeviceConfigError(f"{spec.key}: must be >= {spec.minimum}, got {value}")
    if spec.maximum is not None and value > spec.maximum:
        raise DeviceConfigError(f"{spec.key}: must be <= {spec.maximum}, got {value}")
    return value


def format_value(spec: SettingSpec, value: Any) -> str:
    """Render a value for display.

    Args:
        spec: The setting the value belongs to.
        value: The current value (may be ``None`` if unknown).

    Returns:
        A human-readable string.
    """
    if value is None:
        return "?"
    if spec.value_type == "bool":
        return "true" if value else "false"
    if spec.value_type == "enum" and spec.choices is not None and value in spec.choices:
        return f"{value} ({spec.choices[value]})"
    return str(value)


# --- snapshot ----------------------------------------------------------------


async def build_snapshot(device: Device) -> dict:
    """Read a device's full current configuration into one dict.

    Merges ``SELF_INFO`` with the separately-read tuning parameters and path-hash mode.
    Optional reads that a given firmware does not support are skipped silently so the rest
    of the snapshot still renders.

    Args:
        device: A connected device.

    Returns:
        A dict keyed like ``SELF_INFO`` plus ``rx_delay``, ``airtime_factor`` and
        ``path_hash_mode``.
    """
    snapshot = dict(await device.get_self_info())
    try:
        snapshot.update(await device.get_tuning())
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        pass
    try:
        snapshot["path_hash_mode"] = await device.get_path_hash_mode()
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        pass
    return snapshot


# --- coupled-command apply helpers -------------------------------------------


def _radio_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one radio field, preserving the others."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        params = {
            "freq": snapshot.get("radio_freq"),
            "bw": snapshot.get("radio_bw"),
            "sf": snapshot.get("radio_sf"),
            "cr": snapshot.get("radio_cr"),
        }
        params[field_name] = value
        await device.set_radio(params["freq"], params["bw"], params["sf"], params["cr"])

    return apply


def _coords_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one coordinate, preserving the other."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        lat = value if field_name == "adv_lat" else snapshot.get("adv_lat", 0.0)
        lon = value if field_name == "adv_lon" else snapshot.get("adv_lon", 0.0)
        await device.set_coords(float(lat or 0.0), float(lon or 0.0))

    return apply


def _tuning_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one tuning field, preserving the other."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        rx = value if field_name == "rx_delay" else snapshot.get("rx_delay", 0)
        af = value if field_name == "airtime_factor" else snapshot.get("airtime_factor", 0)
        await device.set_tuning(int(rx or 0), int(af or 0))

    return apply


def _telemetry_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one telemetry mode, preserving the others."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        base = value if field_name == "telemetry_mode_base" else snapshot.get("telemetry_mode_base", 0)
        loc = value if field_name == "telemetry_mode_loc" else snapshot.get("telemetry_mode_loc", 0)
        env = value if field_name == "telemetry_mode_env" else snapshot.get("telemetry_mode_env", 0)
        await device.set_telemetry_modes(int(base or 0), int(loc or 0), int(env or 0))

    return apply


def _get(key: str) -> Callable[[dict], Any]:
    """Build a getter that reads ``key`` from a snapshot."""
    return lambda snapshot: snapshot.get(key)


_TELEMETRY_CHOICES = {0: "off", 1: "always", 2: "device-only", 3: "all"}

# adv_loc_policy / multi_acks are single firmware bytes; we list the values seen in the
# wild but keep them non-strict so an unfamiliar value is still accepted.
_ADV_LOC_CHOICES = {0: "don't share location", 1: "share location in adverts"}
_MULTI_ACKS_CHOICES = {0: "off", 1: "on"}
# path_hash_mode is a 2-bit field; the hash size carried per hop is mode + 1 bytes.
_PATH_HASH_CHOICES = {
    0: "1-byte hashes (default)",
    1: "2-byte hashes",
    2: "3-byte hashes",
    3: "4-byte hashes",
}


def _telemetry_spec(key: str, label: str) -> SettingSpec:
    """Build a telemetry-mode setting spec (shared shape for base/loc/env)."""
    return SettingSpec(
        key=key, label=label, help="Telemetry reporting mode.", category="Behavior",
        value_type="enum", choices=_TELEMETRY_CHOICES, minimum=0, maximum=3,
        getter=_get(key), apply=_telemetry_apply(key),
    )


# --- radio presets -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RadioPreset:
    """A named, standard set of radio parameters applied as one unit.

    Attributes:
        name: Human-friendly preset name (region or trade-off).
        help: One-line description of when to use it.
        freq: Carrier frequency in MHz.
        bw: Channel bandwidth in kHz.
        sf: LoRa spreading factor.
        cr: LoRa coding-rate denominator (5-8 = 4/5-4/8).
    """

    name: str
    help: str
    freq: float
    bw: float
    sf: int
    cr: int

    def as_settings(self) -> dict[str, Any]:
        """Return this preset as a ``{setting_key: value}`` mapping for staging."""
        return {
            "radio_freq": self.freq,
            "radio_bw": self.bw,
            "radio_sf": self.sf,
            "radio_cr": self.cr,
        }


# Standard MeshCore presets. The default modulation (250 kHz / SF11 / CR5) is shared
# across regions; the region presets differ only in the default channel frequency. The
# trade-off presets keep the EU frequency but trade airtime for range or speed.
RADIO_PRESETS: list[RadioPreset] = [
    RadioPreset("EU / UK 868", "MeshCore default for the 868 MHz band.", 869.525, 250.0, 11, 5),
    RadioPreset("US 915", "MeshCore default for the US 915 MHz band.", 910.525, 250.0, 11, 5),
    RadioPreset("AU / NZ 915", "MeshCore default for the AU/NZ 915 MHz band.", 915.525, 250.0, 11, 5),
    RadioPreset("Long range (slow)", "Narrow bandwidth + high SF: max range, low data rate.", 869.525, 125.0, 12, 8),
    RadioPreset("Fast (short range)", "Wide bandwidth + low SF: short range, low airtime.", 869.525, 500.0, 7, 5),
]


# --- the registry ------------------------------------------------------------

DEVICE_SETTINGS: list[SettingSpec] = [
    # Identity
    SettingSpec(
        "name", "Node name", "Advertised name of this node.", "Identity", "str",
        getter=_get("name"), apply=lambda d, v, s: d.set_name(v),
    ),
    SettingSpec(
        "adv_lat", "Latitude", "Advertised latitude (decimal degrees).", "Identity",
        "float", minimum=-90.0, maximum=90.0,
        getter=_get("adv_lat"), apply=_coords_apply("adv_lat"),
    ),
    SettingSpec(
        "adv_lon", "Longitude", "Advertised longitude (decimal degrees).", "Identity",
        "float", minimum=-180.0, maximum=180.0,
        getter=_get("adv_lon"), apply=_coords_apply("adv_lon"),
    ),
    SettingSpec(
        "device_pin", "Device PIN", "BLE pairing PIN.", "Identity", "int",
        minimum=0, maximum=999999,
        getter=_get("device_pin"), apply=lambda d, v, s: d.set_device_pin(v),
    ),
    # Radio
    SettingSpec(
        "radio_freq", "Frequency (MHz)", "Carrier frequency in MHz.", "Radio", "float",
        minimum=100.0, maximum=1000.0,
        getter=_get("radio_freq"), apply=_radio_apply("freq"),
    ),
    SettingSpec(
        "radio_bw", "Bandwidth (kHz)", "Channel bandwidth in kHz.", "Radio", "float",
        minimum=1.0, maximum=1000.0,
        getter=_get("radio_bw"), apply=_radio_apply("bw"),
    ),
    SettingSpec(
        "radio_sf", "Spreading factor", "LoRa spreading factor.", "Radio", "int",
        minimum=5, maximum=12,
        getter=_get("radio_sf"), apply=_radio_apply("sf"),
    ),
    SettingSpec(
        "radio_cr", "Coding rate", "LoRa coding-rate denominator (5-8 = 4/5-4/8).",
        "Radio", "int", minimum=5, maximum=8,
        getter=_get("radio_cr"), apply=_radio_apply("cr"),
    ),
    SettingSpec(
        "tx_power", "TX power (dBm)", "Transmit power.", "Radio", "int",
        minimum=1, maximum=22,
        getter=_get("tx_power"), apply=lambda d, v, s: d.set_tx_power(v),
    ),
    # Tuning
    SettingSpec(
        "rx_delay", "RX delay", "Receive delay tuning parameter.", "Tuning", "int",
        minimum=0,
        getter=_get("rx_delay"), apply=_tuning_apply("rx_delay"),
    ),
    SettingSpec(
        "airtime_factor", "Airtime factor", "Airtime budgeting factor.", "Tuning", "int",
        minimum=0,
        getter=_get("airtime_factor"), apply=_tuning_apply("airtime_factor"),
    ),
    # Behavior
    SettingSpec(
        "manual_add_contacts", "Manual add contacts",
        "Require contacts to be added manually.", "Behavior", "bool",
        getter=_get("manual_add_contacts"),
        apply=lambda d, v, s: d.set_manual_add_contacts(v),
    ),
    SettingSpec(
        "adv_loc_policy", "Advert location policy",
        "Whether this node shares its location in adverts.", "Behavior", "enum",
        choices=_ADV_LOC_CHOICES, minimum=0, strict_choices=False,
        getter=_get("adv_loc_policy"), apply=lambda d, v, s: d.set_adv_loc_policy(v),
    ),
    SettingSpec(
        "multi_acks", "Multi-acks",
        "Send multiple acknowledgements for added delivery reliability.", "Behavior",
        "enum", choices=_MULTI_ACKS_CHOICES, minimum=0, strict_choices=False,
        getter=_get("multi_acks"), apply=lambda d, v, s: d.set_multi_acks(v),
    ),
    _telemetry_spec("telemetry_mode_base", "Telemetry mode (base)"),
    _telemetry_spec("telemetry_mode_loc", "Telemetry mode (location)"),
    _telemetry_spec("telemetry_mode_env", "Telemetry mode (environment)"),
    # Experimental
    SettingSpec(
        "path_hash_mode", "Path-hash mode",
        "Per-hop path-hash size; larger resists hash collisions but adds packet overhead.",
        "Experimental", "enum", choices=_PATH_HASH_CHOICES, minimum=0, maximum=3,
        getter=_get("path_hash_mode"), apply=lambda d, v, s: d.set_path_hash_mode(v),
    ),
]

_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in DEVICE_SETTINGS}


def get_spec(key: str) -> SettingSpec:
    """Return the spec for ``key``.

    Args:
        key: A setting key.

    Returns:
        The matching :class:`SettingSpec`.

    Raises:
        DeviceConfigError: If no such setting exists.
    """
    spec = _BY_KEY.get(key)
    if spec is None:
        known = ", ".join(sorted(_BY_KEY))
        raise DeviceConfigError(f"unknown setting {key!r}. Known settings: {known}")
    return spec


def settings_by_category() -> list[tuple[str, list[SettingSpec]]]:
    """Return settings grouped by category in display order.

    Returns:
        A list of ``(category, specs)`` pairs following :data:`CATEGORIES` order.
    """
    return [
        (cat, [s for s in DEVICE_SETTINGS if s.category == cat])
        for cat in CATEGORIES
    ]
