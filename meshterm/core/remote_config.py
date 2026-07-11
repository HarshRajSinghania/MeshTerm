"""The remote-node settings catalog: what a repeater can be asked and told over the mesh.

Repeaters and room servers are administered through their text CLI — ``get``/``set``
commands sent as admin messages (see
:meth:`~meshterm.core.connection.Device.send_remote_command`) — not through the binary
companion protocol the local device-configuration editor drives. This module is the
data-driven bridge: one :class:`RemoteSetting` per knob the standard MeshCore repeater
firmware exposes, each carrying its CLI spelling, a one-line explanation, and enough
typing to prompt and validate sensibly. The repeater-admin screen renders the catalog in
the local editor's lanes, stages values, and applies them as ``set`` commands; anything
the catalog doesn't cover is one keystroke away in the raw command line.

Replies are parsed *loosely* on purpose: repeater firmware answers tersely and
inconsistently across versions (``"tx: 20"``, ``"TX power = 20 dBm"``, ``"> ok"``), so
values are extracted rather than pattern-matched, and the raw reply is always kept for
display. Firmware without a given key simply answers with an error string, which shows
in the value lane instead of breaking the screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

#: Reply text (lowercased) that reads as the firmware refusing/unknowing a command.
_ERRORISH = ("unknown", "error", "err:", "invalid", "denied", "bad ")


@dataclass(frozen=True, slots=True)
class RemoteSetting:
    """One remote-CLI setting: its spelling, presentation, and typing.

    Attributes:
        key: The CLI parameter name as the firmware spells it (e.g. ``"txdelay"``).
        label: The display name shown in the editor lane.
        help: One-line explanation (no trailing period), shown beside the value.
        category: Editor section heading the setting sorts under.
        kind: ``"int"``, ``"float"``, ``"str"``, or ``"bool"`` (on/off).
        writable: Whether the firmware accepts a ``set`` for this key.
        readable: Whether the firmware answers a ``get`` (passwords do not).
        minimum: Lower bound for numeric prompts, when known.
        maximum: Upper bound for numeric prompts, when known.
        unit: Display unit hint (e.g. ``"dBm"``, ``"min"``), purely cosmetic.
    """

    key: str
    label: str
    help: str
    category: str
    kind: str = "str"
    writable: bool = True
    readable: bool = True
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: str = ""

    @property
    def get_command(self) -> str:
        """The CLI command that reads this setting."""
        return f"get {self.key}"

    def set_command(self, value: str) -> str:
        """The CLI command that writes ``value`` to this setting."""
        return f"set {self.key} {value}"


#: Every knob the standard MeshCore repeater/room firmware exposes over its CLI, in the
#: editor's display order. TX delay and Direct TX delay — absent from the local
#: companion editor because companions don't repeat — are first-class here.
REPEATER_SETTINGS: tuple[RemoteSetting, ...] = (
    # -- Identity ---------------------------------------------------------------
    RemoteSetting(
        key="name", label="Name", category="Identity", kind="str",
        help="The node's advertised name",
    ),
    RemoteSetting(
        key="lat", label="Latitude", category="Identity", kind="float",
        minimum=-90, maximum=90, help="Advertised position, decimal degrees",
    ),
    RemoteSetting(
        key="lon", label="Longitude", category="Identity", kind="float",
        minimum=-180, maximum=180, help="Advertised position, decimal degrees",
    ),
    # -- Radio ------------------------------------------------------------------
    RemoteSetting(
        key="freq", label="Frequency", category="Radio", kind="float", unit="MHz",
        help="Carrier frequency — every node on the mesh must match",
    ),
    RemoteSetting(
        key="bw", label="Bandwidth", category="Radio", kind="float", unit="kHz",
        help="Channel bandwidth — must match the mesh",
    ),
    RemoteSetting(
        key="sf", label="Spreading factor", category="Radio", kind="int",
        minimum=7, maximum=12, help="LoRa spreading factor — must match the mesh",
    ),
    RemoteSetting(
        key="cr", label="Coding rate", category="Radio", kind="int",
        minimum=5, maximum=8, help="LoRa coding rate denominator — must match the mesh",
    ),
    RemoteSetting(
        key="tx", label="TX power", category="Radio", kind="int", unit="dBm",
        minimum=1, maximum=30,
        help="Transmit power — the TX optimize tool tunes this by measurement",
    ),
    # -- Repeating ----------------------------------------------------------------
    RemoteSetting(
        key="repeat", label="Repeat", category="Repeating", kind="bool",
        help="Whether the node rebroadcasts mesh traffic at all",
    ),
    RemoteSetting(
        key="txdelay", label="TX delay", category="Repeating", kind="int",
        minimum=0, help="Delay factor before rebroadcasting a flood packet",
    ),
    RemoteSetting(
        key="direct.txdelay", label="Direct TX delay", category="Repeating", kind="int",
        minimum=0, help="Delay factor before forwarding a directed packet",
    ),
    RemoteSetting(
        key="rxdelay", label="RX delay", category="Repeating", kind="int",
        minimum=0, help="Base delay before acting on a received packet",
    ),
    RemoteSetting(
        key="af", label="Airtime factor", category="Repeating", kind="float",
        minimum=0, help="Duty-cycle multiplier applied to every transmission",
    ),
    RemoteSetting(
        key="allow.read.only", label="Allow read-only", category="Repeating", kind="bool",
        help="Whether guests may log in with the guest password",
    ),
    # -- Adverts -----------------------------------------------------------------
    RemoteSetting(
        key="advert.interval", label="Advert interval", category="Adverts", kind="int",
        unit="min", minimum=0,
        help="Minutes between the node's own zero-hop adverts (0 = off)",
    ),
    RemoteSetting(
        key="flood.advert.interval", label="Flood advert interval", category="Adverts",
        kind="int", unit="h", minimum=0,
        help="Hours between the node's own flood adverts (0 = off)",
    ),
    RemoteSetting(
        key="flood.max", label="Flood max", category="Adverts", kind="int", minimum=0,
        help="Hop cap the node applies when rebroadcasting floods (0 = no cap)",
    ),
    # -- Access ------------------------------------------------------------------
    RemoteSetting(
        key="guest.password", label="Guest password", category="Access", kind="str",
        readable=False, help="Read-only login password (write-only over the CLI)",
    ),
)


#: Commands the remote CLI screen offers as completions, beyond the catalog's
#: ``get``/``set`` spellings: the fixed verbs a MeshCore repeater understands.
KNOWN_COMMANDS: tuple[str, ...] = (
    "advert",
    "clock",
    "clock sync",
    "get ",
    "neighbors",
    "password ",
    "reboot",
    "set ",
    "start ota",
    "time ",
    "ver",
)


def known_commands() -> list[str]:
    """Every completion the remote CLI offers: verbs plus catalog get/set spellings."""
    commands = set(KNOWN_COMMANDS)
    for spec in REPEATER_SETTINGS:
        if spec.readable:
            commands.add(spec.get_command)
        if spec.writable:
            commands.add(f"set {spec.key} ")
    return sorted(commands)


def get_setting(key: str) -> Optional[RemoteSetting]:
    """Look up a catalog setting by its CLI key."""
    return next((s for s in REPEATER_SETTINGS if s.key == key), None)


def settings_by_category() -> list[tuple[str, list[RemoteSetting]]]:
    """The catalog grouped by category, in declaration order (the editor's layout)."""
    grouped: dict[str, list[RemoteSetting]] = {}
    for spec in REPEATER_SETTINGS:
        grouped.setdefault(spec.category, []).append(spec)
    return list(grouped.items())


def reply_is_error(reply: str) -> bool:
    """Whether a reply text reads as the firmware refusing the command."""
    lowered = reply.lower()
    return any(marker in lowered for marker in _ERRORISH)


def parse_reply_value(spec: RemoteSetting, reply: Optional[str]) -> Optional[str]:
    """Extract a display value from a ``get`` reply, or ``None`` when unusable.

    Numeric settings pull the first number out of the terse, version-varying reply
    text (the ``get tx`` convention); strings strip a leading ``key:``/``>`` echo.
    Error-ish replies parse as ``None`` so a firmware without the key shows unknown
    rather than adopting the error string as a value.

    Args:
        spec: The setting the reply answers.
        reply: The raw reply text, or ``None`` if the node never answered.

    Returns:
        The value as display text, or ``None``.
    """
    if not reply or reply_is_error(reply):
        return None
    text = reply.strip()
    if spec.kind in ("int", "float"):
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match is None:
            return None
        raw = match.group()
        if spec.kind == "int":
            return str(int(float(raw)))
        return f"{float(raw):g}"
    if spec.kind == "bool":
        lowered = text.lower()
        if any(t in lowered for t in ("on", "true", "yes", "1")):
            return "on"
        if any(t in lowered for t in ("off", "false", "no", "0")):
            return "off"
        return None
    # Strings: drop a "key:" or "-> " echo prefix if the firmware included one.
    text = re.sub(rf"^\s*(?:>|->|{re.escape(spec.key)}\s*[:=])\s*", "", text, flags=re.I)
    return text or None


def validate_value(spec: RemoteSetting, raw: str) -> "bool | str":
    """Validate a prompted value for ``spec``: ``True``, or the error message to show."""
    text = raw.strip()
    if not text:
        return "Enter a value."
    if spec.kind == "bool":
        if text.lower() in ("on", "off"):
            return True
        return "Enter on or off."
    if spec.kind in ("int", "float"):
        try:
            value = float(text)
        except ValueError:
            return "Enter a number."
        if spec.kind == "int" and not float(text).is_integer():
            return "Enter a whole number."
        if spec.minimum is not None and value < spec.minimum:
            return f"Must be ≥ {spec.minimum:g}."
        if spec.maximum is not None and value > spec.maximum:
            return f"Must be ≤ {spec.maximum:g}."
    return True
