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
    assert "white = selected path" in body  # the graph caption
    assert "★ you" in body and "▲ repeater" in body  # the node-type legend
    assert screen.cursor_line() is not None


def test_paths_screen_labels_every_relay_with_its_hash_byte() -> None:
    """Graph relays carry their first hash byte, both paths at once; names stay
    in the rows, annotated with the same byte."""
    screen = _screen(_arrivals())
    body = _plain(screen.render_body(76))
    graph = body.split("origin →")[0]
    for byte in ("3d", "a1", "77"):  # every relay labelled, selected or not
        assert byte in graph
    assert "YUL-Cartier" not in graph and "Waymarker" not in graph
    assert "YUL-Cartierville (3d)" in body  # the rows carry name + hash byte
    assert "Waymarker (a1)" in body


def test_paths_screen_shows_unknown_relay_as_grey_mode_width_hash() -> None:
    """An unnamed relay stands in its own hash at the device's path-hash width, muted
    grey and annotated with its addressed byte — not the bare one-byte prefix, lit."""
    now = utcnow()
    arrivals = [
        Arrival(when=now, hops=("3d63",), snr=4.0),  # selected: a named relay
        Arrival(when=now + timedelta(seconds=2), hops=("e839f2ab",), snr=-2.0),
    ]
    screen = _screen(arrivals, prefix_bytes=3)  # 3-byte routing → e839f2
    lines = screen.render_body(76)
    row = next(ln for ln in lines if "e839f2" in _plain([ln]))
    assert "e839f2 (e8)" in _plain([row])  # mode-width identity, then the byte
    assert "38;2;148;163;184" in row  # muted grey over the hash
    assert "38;2;94;234;212" not in row  # never the brand highlight
    graph = _plain(lines).split("origin →")[0]
    assert "e8" in graph  # the row's byte still cross-references the graph label


def test_paths_screen_draws_selected_path_white_over_gray() -> None:
    """The selected path's edges render white; the unused path's edges gray."""
    screen = _screen(_arrivals())
    raw = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw  # the selected path, white
    assert "38;2;110;110;110" in raw  # the other path, gray beneath it
    screen.handle("down")  # move the selection to the other path
    raw2 = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw2 and "38;2;110;110;110" in raw2


def test_paths_screen_marks_a_repeater_relay_with_its_triangle() -> None:
    """A relay whose type resolves to a repeater draws ▲, not the generic dot."""
    screen = _screen(_arrivals(), type_of=lambda h: 2 if h == "3d63" else None)
    graph = _plain(screen.render_body(76)).split("origin →")[0]
    assert "▲" in graph  # the repeater relay wears its map glyph
    screen.handle("down")
    raw = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw and "38;2;110;110;110" in raw


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
