"""A process-wide map from a node's key prefix to its advert type.

PicoCalc's 16-slot console palette can't afford the regular platform's 256-hue key-derived
name spectrum, so there a name is coloured by its node's *type* (client / repeater / room /
sensor) instead — see ``name_colour`` on :class:`~meshterm.platforms.Platform`. The theme's
type-based ``name_style`` needs the type for a bare key at render time, long after the
advert that carried it; this registry is that lookup.

It is fed by the reception layer wherever typed nodes materialize — live observations
(:class:`~meshterm.services.monitor_service.MonitorService`), heard nodes read back from
the history DB (:meth:`~meshterm.persistence.repository.Repository.heard_nodes`), and the
device's contact list (:func:`~meshterm.core.contact_store.merge_contacts`) — so both a
fresh session and a long-running one resolve the same nodes to the same types.

Keys are reduced to their **first byte**, the same slice every key-prefix surface agrees
on (see :func:`~meshterm.ui.theme.node_style`): any prefix of a key a caller happens to
hold — a 2-hex path hop, the stored 12-hex id, the full public key — lands on the same
entry. Two nodes sharing a first byte with different types will colour by whichever was
registered last; colour is a hint, not a guarantee, and the collision odds mirror the
hue-collision odds the regular platform already accepts.
"""

from __future__ import annotations

from typing import Optional

from .models import NODE_TYPE_LABELS

#: first key byte (two lowercase hex chars) → type name ("node"/"repeater"/"room"/"sensor").
_TYPES_BY_PREFIX: dict[str, str] = {}


def _prefix(key: str) -> Optional[str]:
    """The registry key for ``key``: its first byte as two lowercase hex chars."""
    raw = key.lower().removeprefix("0x")
    return raw[:2] if len(raw) >= 2 else None


def register_node_type(key: Optional[str], node_type: Optional[int]) -> None:
    """Record that the node ``key`` belongs to advertises as ``node_type``.

    A no-op for a missing key, a too-short prefix, or a type outside the known
    ``NODE_TYPE_*`` set, so reception paths can call it unconditionally.

    Args:
        key: The node's public key or any hex prefix of it (first byte is used).
        node_type: The advert type (``NODE_TYPE_CHAT``/``REPEATER``/``ROOM``/``SENSOR``),
            or ``None`` when the packet didn't carry one.
    """
    if not key or node_type is None:
        return
    name = NODE_TYPE_LABELS.get(node_type)
    prefix = _prefix(key)
    if name and prefix:
        _TYPES_BY_PREFIX[prefix] = name


def node_type_name(key: Optional[str]) -> Optional[str]:
    """The registered type name for ``key``'s node, or ``None`` when never registered.

    Args:
        key: The node's key/hash as hex (any length ≥ 1 byte).

    Returns:
        ``"node"``, ``"repeater"``, ``"room"``, ``"sensor"``, or ``None``.
    """
    if not key:
        return None
    prefix = _prefix(key)
    return _TYPES_BY_PREFIX.get(prefix) if prefix else None
