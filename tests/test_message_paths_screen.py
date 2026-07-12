"""Message-paths dialog tests: the graph-over-rows view behind the chat's ^P.

Driven headless like the atlas tests: render_body is pure lines-out, handle() pure
state, so the selection cursor, the horizontal line scroll, and the selected-path
labelling are all assertable without a terminal.
"""

from __future__ import annotations

import re
from datetime import timedelta

from meshterm.core.models import ChatMessage, utcnow
from meshterm.services.message_paths import Arrival
from meshterm.ui.message_paths_screen import MessagePathsScreen


def _resolve(hop: str) -> str:
    return {"3d63": "YUL-Cartierville", "a1b2": "Waymarker"}.get(hop, hop)


def _plain(lines: list[str]) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(lines))


def _screen(arrivals: list[Arrival], **kwargs) -> MessagePathsScreen:
    message = ChatMessage(text="on my way", is_channel=True, created_at=utcnow())
    defaults = dict(
        matched=True, resolve=_resolve, prefix_bytes=1, self_name="Homestead",
        summary="heard twice", source="Alice",
    )
    defaults.update(kwargs)
    return MessagePathsScreen(message, arrivals, **defaults)


def _arrivals() -> list[Arrival]:
    now = utcnow()
    return [
        Arrival(when=now, hops=("3d63",), snr=4.0),
        Arrival(when=now + timedelta(seconds=2), hops=("a1b2", "77aa"), snr=-2.0),
    ]


def test_paths_screen_renders_graph_rows_and_cursor() -> None:
    """The quoted text, the graph endpoints, and one selectable row per arrival."""
    screen = _screen(_arrivals())
    body = _plain(screen.render_body(76))
    assert "“on my way”" in body
    assert "Alice" in body and "Homestead" in body  # origin and us, on the graph
    assert body.count("via") == 2  # one row per arrival
    assert "❯" in body
    assert "bright = selected path" in body
    assert screen.cursor_line() is not None


def test_paths_screen_labels_only_the_selected_path() -> None:
    """Relay labels follow the selection: the other path keeps bare markers."""
    screen = _screen(_arrivals())
    graph = _plain(screen.render_body(76)).split("origin →")[0]
    assert "YUL-Cartier" in graph  # the selected (first) path's relay is named
    assert "Waymarker" not in graph  # the other path's relays stay unlabelled
    screen.handle("down")
    graph = _plain(screen.render_body(76)).split("origin →")[0]
    assert "Waymarker" in graph
    assert "YUL-Cartier" not in graph


def test_paths_screen_scrolls_the_selected_line_sideways() -> None:
    """→ shifts the whole selected row under a leading …; ↑↓ snap it back."""
    now = utcnow()
    long = Arrival(when=now, hops=tuple(f"{i:02x}{i:02x}" for i in range(12)), snr=1.0)
    screen = _screen([long, Arrival(when=now, hops=(), snr=None)])
    narrow = 40
    before = _plain(screen.render_body(narrow))
    selected_before = next(ln for ln in before.split("\n") if ln.startswith("❯"))
    assert screen._hmax > 0  # the row genuinely overflows at this width
    for _ in range(3):
        screen.handle("right")
    after = _plain(screen.render_body(narrow))
    selected_after = next(ln for ln in after.split("\n") if ln.startswith("❯"))
    assert selected_after != selected_before
    assert "…" in selected_after  # the left edge marks the hidden head
    stamp = long.when.astimezone().strftime("%H:%M:%S")
    assert stamp in selected_before and stamp not in selected_after  # whole line shifts
    screen.handle("down")
    assert screen._hshift == 0  # only the selected line stays scrolled


def test_paths_screen_direct_arrival_and_empty_state() -> None:
    """A hop-less arrival reads direct; no arrivals at all explain themselves."""
    now = utcnow()
    screen = _screen([Arrival(when=now, hops=(), snr=2.5)])
    body = _plain(screen.render_body(76))
    assert "direct" in body
    empty = _screen([], matched=False)
    body = _plain(empty.render_body(76))
    assert "No direct-message frames logged in the window." in body
    assert empty.footer_hint == "Esc close"
