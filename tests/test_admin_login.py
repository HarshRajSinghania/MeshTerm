"""A repeater that says nothing has not said the password is wrong.

THE bug, reported after administering a repeater that happened to be down: MeshTerm forgot
that repeater's admin password. The login came back ``False``, every caller read ``False``
as *wrong password*, and the credential went in the bin — for a node that had never
answered at all.

Two things were wrong under that. The ``meshcore`` library's ``send_login_sync`` waits for
``LOGIN_SUCCESS`` and only ``LOGIN_SUCCESS``, so a refusal (which the firmware *does* send,
as a ``LOGIN_FAILED`` frame) times out exactly like silence and comes back as the same
``None``. And the credential policy lived, five times over, at the call sites.

Now the device listens for the refusal frame itself and answers with a three-way
:class:`~meshterm.core.models.LoginResult`, and one method —
:meth:`~meshterm.core.admin_store.AdminStore.record` — owns what that means for the stored
password: remember on accepted, forget on refused, and *leave it alone* on silence.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import DeviceCommandError, MeshCoreDevice, MockDevice
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import Contact, LoginResult
from meshterm.persistence.repository import Repository

_NODE = Contact(name="Yagi-Repeater", public_key="a1b2c3d4" * 8, key_prefix="a1b2c3d4")


# --- what the three outcomes mean -----------------------------------------------------


def test_only_an_accepted_login_is_truthy() -> None:
    """``if not await device.admin_login(...)`` has to keep meaning "we are not in".

    The three-way answer replaced a bool, and the whole point is that a call site which
    only asks "am I logged in?" reads the same as it always did — while one that *acts* on
    the failure is forced to name which failure it is acting on.
    """
    assert LoginResult.ACCEPTED
    assert not LoginResult.REFUSED
    assert not LoginResult.NO_REPLY


def test_the_two_failures_are_distinguishable() -> None:
    """They were the same ``False``; being able to tell them apart *is* the fix."""
    assert LoginResult.REFUSED is not LoginResult.NO_REPLY


# --- the credential policy, in the one place that owns it -----------------------------


@pytest.fixture()
def store(tmp_path: Path) -> AdminStore:
    """An admin store with a password already remembered for the node."""
    store = AdminStore(tmp_path / "admin.json")
    store.remember(_NODE, "hunter2")
    return store


def test_silence_leaves_the_remembered_password_exactly_where_it_was(store) -> None:  # noqa: ANN001
    """THE regression. The node was down; it never rendered a verdict on the password."""
    store.record(_NODE, "hunter2", LoginResult.NO_REPLY)

    assert store.get(_NODE) == "hunter2"


def test_a_refusal_clears_the_password_because_the_node_said_so(store) -> None:  # noqa: ANN001
    """A node that answered "no" is the one authority on the password being wrong."""
    store.record(_NODE, "hunter2", LoginResult.REFUSED)

    assert store.get(_NODE) is None


def test_a_successful_login_remembers_the_password_that_worked(store) -> None:  # noqa: ANN001
    """Including a freshly typed one — that is how the credential gets stored at all."""
    store.forget(_NODE)

    store.record(_NODE, "correct-horse", LoginResult.ACCEPTED)

    assert store.get(_NODE) == "correct-horse"


def test_silence_does_not_invent_a_password_either(tmp_path: Path) -> None:
    """A no-reply on a node we have nothing stored for must stay nothing stored."""
    store = AdminStore(tmp_path / "admin.json")

    store.record(_NODE, "typed-once", LoginResult.NO_REPLY)

    assert store.get(_NODE) is None


def test_a_node_that_goes_quiet_after_working_keeps_its_password(store) -> None:  # noqa: ANN001
    """The lived sequence: it worked yesterday, it is down today, it works tomorrow."""
    store.record(_NODE, "hunter2", LoginResult.ACCEPTED)
    store.record(_NODE, "hunter2", LoginResult.NO_REPLY)
    store.record(_NODE, "hunter2", LoginResult.NO_REPLY)

    assert store.get(_NODE) == "hunter2"


# --- reading the wire: a refusal is not a timeout -------------------------------------


class _Subscription:
    """Stands in for meshcore's Subscription handle."""

    def __init__(self, event_type, callback) -> None:  # noqa: ANN001
        self.event_type = event_type
        self.callback = callback
        self.live = True


class _Event:
    def __init__(self, type_, payload=None) -> None:  # noqa: ANN001
        self.type = type_
        self.payload = payload or {}


class _FakeCommands:
    """``send_login_sync`` as the library really behaves: it only ever sees success.

    A refusal reaches the app the way it reaches it on hardware — dispatched to whoever
    subscribed to ``LOGIN_FAILED`` while the library's own wait quietly times out.
    """

    def __init__(self, mc, *, success: bool, dispatch=()) -> None:  # noqa: ANN001
        self._mc = mc
        self._success = success
        self._dispatch = list(dispatch)

    async def send_login_sync(self, pubkey, password):  # noqa: ANN001
        from meshcore import EventType

        self._mc.sent.append((pubkey, password))
        self._mc.subscribed_before_send = bool(self._mc.subscriptions)
        for event in self._dispatch:
            self._mc.dispatch(event)
        await asyncio.sleep(0)
        return _Event(EventType.LOGIN_SUCCESS, {"is_admin": True}) if self._success else None


class _FakeMeshCore:
    def __init__(self, *, success: bool = False, dispatch=()) -> None:  # noqa: ANN001
        self.subscriptions: list[_Subscription] = []
        self.sent: list[tuple] = []
        self.subscribed_before_send = False
        self.commands = _FakeCommands(self, success=success, dispatch=dispatch)

    def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001
        sub = _Subscription(event_type, callback)
        self.subscriptions.append(sub)
        return sub

    def unsubscribe(self, subscription) -> None:  # noqa: ANN001
        subscription.live = False
        self.subscriptions.remove(subscription)

    def dispatch(self, event) -> None:  # noqa: ANN001
        for sub in list(self.subscriptions):
            if sub.event_type is event.type:
                sub.callback(event)


def _device(mc) -> MeshCoreDevice:  # noqa: ANN001
    device = MeshCoreDevice(port="mock")
    device._mc = mc
    return device


def _login_failed(prefix: str | None = None):
    from meshcore import EventType

    return _Event(EventType.LOGIN_FAILED, {"pubkey_prefix": prefix} if prefix else {})


def test_a_login_the_node_accepts_reads_as_accepted() -> None:
    """The happy path is unchanged; the session is open."""
    mc = _FakeMeshCore(success=True)

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.ACCEPTED
    assert mc.sent == [(_NODE.public_key, "hunter2")]


def test_a_dispatched_refusal_frame_reads_as_refused() -> None:
    """The library's wait times out on it, so the app has to hear the frame itself.

    Without this, a genuinely wrong password would report no-reply and be kept forever —
    the mirror image of the reported bug, and the reason the refusal is not simply assumed.
    """
    mc = _FakeMeshCore(success=False, dispatch=[_login_failed("a1b2c3d4a1b2")])

    assert asyncio.run(_device(mc).admin_login(_NODE, "wrong")) is LoginResult.REFUSED


def test_silence_reads_as_no_reply() -> None:
    """THE case that started this: nothing came back, so nothing is known."""
    mc = _FakeMeshCore(success=False)

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.NO_REPLY


def test_a_terse_refusal_with_no_key_prefix_still_counts_as_one() -> None:
    """Firmware only stamps the sender's prefix when the frame is long enough to carry it.

    Demanding one would quietly turn every refusal from terse firmware back into a
    no-reply — and back into keeping a password the node has already rejected.
    """
    mc = _FakeMeshCore(success=False, dispatch=[_login_failed()])

    assert asyncio.run(_device(mc).admin_login(_NODE, "wrong")) is LoginResult.REFUSED


def test_a_refusal_meant_for_a_different_node_is_not_ours() -> None:
    """Two admin flows can overlap; a stranger's rejection must not clear our password."""
    mc = _FakeMeshCore(success=False, dispatch=[_login_failed("ffeeddccbbaa")])

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.NO_REPLY


def test_the_refusal_watch_is_in_place_before_the_request_goes_out() -> None:
    """A refusal can land the instant the request does — subscribing after would miss it."""
    mc = _FakeMeshCore(success=True)

    asyncio.run(_device(mc).admin_login(_NODE, "hunter2"))

    assert mc.subscribed_before_send


def test_the_refusal_watch_is_released_even_when_the_send_blows_up() -> None:
    """One subscription per attempt; a leaked one would accumulate over a session."""

    class _Exploding(_FakeCommands):
        async def send_login_sync(self, pubkey, password):  # noqa: ANN001
            raise RuntimeError("the companion dropped the link")

    mc = _FakeMeshCore()
    mc.commands = _Exploding(mc, success=False)

    with pytest.raises(RuntimeError):
        asyncio.run(_device(mc).admin_login(_NODE, "hunter2"))
    assert mc.subscriptions == []


def test_a_companion_side_error_is_silence_not_a_denial() -> None:
    """The request never left the radio, so the node cannot have rejected anything."""
    from meshcore import EventType

    mc = _FakeMeshCore(success=False)
    mc.commands = _FakeCommands(mc, success=False)

    async def _errored(pubkey, password):  # noqa: ANN001
        return _Event(EventType.ERROR, {})

    mc.commands.send_login_sync = _errored

    assert asyncio.run(_device(mc).admin_login(_NODE, "hunter2")) is LoginResult.NO_REPLY


# --- the simulator speaks the same three answers --------------------------------------


async def test_the_simulator_can_model_a_node_that_is_simply_down() -> None:
    """``--mock`` has to be able to walk the down-repeater path, or nobody sees the dialog."""
    device = MockDevice(admin_password="secret")
    await device.connect()
    node = (await device.get_contacts())[0]
    device._unreachable.add(node.name)

    assert await device.admin_login(node, "secret") is LoginResult.NO_REPLY
    assert await device.admin_login(node, "wrong") is LoginResult.NO_REPLY  # still silence


# --- the callers ----------------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path):
    """A context whose device is the simulator and whose admin store is on disk."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "admin.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


async def _mock_node(ctx, *, down: bool) -> Contact:  # noqa: ANN001
    """The simulated repeater, optionally unreachable, with a password already stored."""
    device = await ctx.device()
    node = next(c for c in await device.get_contacts() if c.name == "Yagi-Repeater")
    if down:
        device._unreachable.add(node.name)
    ctx.admin_store.remember(node, "admin")
    return node


async def test_the_scripted_tool_keeps_the_password_when_the_node_is_down(ctx) -> None:  # noqa: ANN001
    """``meshterm repeater-admin <node> <cmd>`` against a repeater that is off the air."""
    from meshterm.tools.repeater_admin import RepeaterAdminTool

    node = await _mock_node(ctx, down=True)

    with pytest.raises(DeviceCommandError, match="did not answer"):
        await RepeaterAdminTool().run(ctx, {"node": node.name, "command": "get tx"})

    assert ctx.admin_store.get(node) == "admin"


async def test_the_scripted_tool_clears_the_password_the_node_rejected(ctx) -> None:  # noqa: ANN001
    """The other half: a node that is reachable and says no really is a bad password."""
    from meshterm.tools.repeater_admin import RepeaterAdminTool

    node = await _mock_node(ctx, down=False)
    ctx.admin_store.remember(node, "not-the-password")

    with pytest.raises(DeviceCommandError, match="wrong password"):
        await RepeaterAdminTool().run(ctx, {"node": node.name, "command": "get tx"})

    assert ctx.admin_store.get(node) is None


async def test_the_tx_optimizer_keeps_the_password_when_the_node_is_down(ctx) -> None:  # noqa: ANN001
    """The sweep logs in before tuning; a down node must not cost the credential either."""
    from meshterm.tools.tx_optimize import TxOptimizeTool

    node = await _mock_node(ctx, down=True)

    with pytest.raises(DeviceCommandError, match="did not answer"):
        await TxOptimizeTool()._login(ctx, node, {})

    assert ctx.admin_store.get(node) == "admin"


async def test_the_tx_optimizer_clears_the_password_the_node_rejected(ctx) -> None:  # noqa: ANN001
    """And still forgets a password the node actually turned down."""
    from meshterm.tools.tx_optimize import TxOptimizeTool

    node = await _mock_node(ctx, down=False)
    ctx.admin_store.remember(node, "not-the-password")

    with pytest.raises(DeviceCommandError, match="wrong password"):
        await TxOptimizeTool()._login(ctx, node, {})

    assert ctx.admin_store.get(node) is None


# --- the interactive flow --------------------------------------------------------------


async def _step_until(predicate, *, limit: int = 200):
    """Yield to the event loop until ``predicate()`` is truthy (the TUI test convention)."""
    value = predicate()
    for _ in range(limit):
        if value:
            return value
        await asyncio.sleep(0)
        value = predicate()
    return value


async def _run_login(ctx, node):  # noqa: ANN001
    """Drive ``ui.repeater_admin._login`` to completion, reading and dismissing its dialog.

    Returns the flow's own answer and the words it put on screen — both matter here: the
    password's fate is one half of the fix and *not telling the user it was wrong* is the
    other.
    """
    from meshterm.ui.repeater_admin import _login
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.prompt import ButtonDialog
    from meshterm.ui.tui.session import TuiSession

    ctx.ui = TuiUi(TuiSession())
    device = await ctx.device()
    task = asyncio.ensure_future(_login(ctx, device, node))
    try:
        dialog = None
        for _ in range(200):
            top = ctx.ui.session.top
            if isinstance(top, ButtonDialog):
                dialog = top
                break
            if task.done():
                break
            await asyncio.sleep(0)
        shown = ""
        if dialog is not None:
            prompt = dialog._prompt
            shown = prompt.plain if hasattr(prompt, "plain") else str(prompt)
            dialog.resolve("ok")
        return await task, shown
    finally:
        if not task.done():
            task.cancel()


async def test_the_admin_flow_says_no_reply_and_keeps_the_password(ctx) -> None:  # noqa: ANN001
    """The screen JP was on. It must not report a wrong password, and must not clear it."""
    node = await _mock_node(ctx, down=True)

    ok, shown = await _run_login(ctx, node)

    assert ok is False
    assert "No reply" in shown and "kept" in shown
    assert "wrong password" not in shown
    assert ctx.admin_store.get(node) == "admin"


async def test_the_admin_flow_still_clears_a_password_the_node_rejected(ctx) -> None:  # noqa: ANN001
    """The behaviour that was right all along, kept honest while the other half changed."""
    node = await _mock_node(ctx, down=False)
    ctx.admin_store.remember(node, "not-the-password")

    ok, shown = await _run_login(ctx, node)

    assert ok is False
    assert "wrong password" in shown and "cleared" in shown
    assert ctx.admin_store.get(node) is None


# --- nobody gets to decide this locally again -------------------------------------------


def test_every_login_caller_goes_through_the_one_credential_policy() -> None:
    """A sixth caller must not re-derive the rule the other five got wrong.

    Source-level on purpose: the failure mode is not a wrong branch, it is a call site that
    never asks the question. Anything that logs in records the outcome through the store,
    and nothing reaches for :meth:`AdminStore.forget` on its own to do it.
    """
    import ast

    import meshterm

    def calls_admin_login(tree: ast.AST) -> bool:
        """A real call, not the word in a docstring — which is why this parses."""
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "admin_login"
            for node in ast.walk(tree)
        )

    root = Path(meshterm.__file__).parent
    callers = [
        path for path in root.rglob("*.py")
        if calls_admin_login(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert callers, "the scan found no callers at all — it has stopped testing anything"
    for path in callers:
        source = path.read_text(encoding="utf-8")
        assert "admin_store.record(" in source, f"{path.name} logs in without recording"
        assert "admin_store.forget(" not in source, (
            f"{path.name} decides the credential policy itself; use admin_store.record()"
        )
