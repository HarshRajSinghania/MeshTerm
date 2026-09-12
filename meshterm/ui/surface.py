"""The UI surface: one small API tools use for input and output, in either front-end.

Tools never touch ``questionary`` or a raw console anymore — they talk to ``ctx.ui``. Two
implementations back it:

* :class:`PlainUi` (scripted CLI): prints immediately and uses a Rich progress bar, so CLI
  behavior is byte-for-byte what it was before the TUI existed.
* :class:`TuiUi` (interactive menu): collects a tool's output and presents it in a bounded,
  scrollable result window — or, when the whole result is just a line or two of text
  ("✓ flood advertisement sent"), as a small centered popup with an OK button instead of
  a full window (see :func:`_collapse_to_message`) — and routes every prompt/progress
  through the full-screen :class:`~meshterm.ui.tui.session.TuiSession`.

The interactive prompt methods are only ever reached from a tool's ``prompt_params`` (menu
only), so :class:`PlainUi` leaves them unsupported.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.text import Text

from .tui.session import TuiSession

#: A validator returns ``True`` when input is acceptable, or an error message to show.
Validator = Callable[[str], "bool | str"]

#: Buffered output no taller than this many lines qualifies for the message dialog —
#: the OK popup :meth:`TuiUi.present` shows instead of a full result window. Kept small:
#: a dialog is an acknowledgement, not a reading surface.
_DIALOG_MAX_LINES = 3

#: ... and no line may be wider than this many cells, so the popup stays a tidy box on a
#: typical terminal and never has to wrap. Anything wider (a long path, a verbose error)
#: falls back to the scrollable result window, which wraps properly.
_DIALOG_MAX_CELLS = 76


def _dialog_max_cells() -> int:
    """The popup-qualifying width for the active platform.

    The regular platform keeps the long-standing 76. On the PicoCalc the dialog frame
    itself is capped at ``cols - 6 = 47`` outer (43 inner), so a line qualifying at 76
    would wrap inside the box it was promoted into — the gate shrinks to match.
    """
    from ..platforms import get_platform

    return _DIALOG_MAX_CELLS if get_platform().frame_border else 43


class _NullBusy:
    """The scripted surface's stand-in for a busy card: a caption that goes nowhere."""

    def __init__(self) -> None:
        """Start with an empty caption; assigning to it is the whole of the contract."""
        self.message = ""


def _collapse_to_message(buffered: list[RenderableType]) -> Text | None:
    """Collapse small, text-only buffered output into one dialog message.

    This is the gate for :meth:`TuiUi.present`'s popup upgrade: output qualifies only
    when everything buffered is plain note text (:class:`Text` — a table or panel from
    ``show()`` disqualifies the lot) totalling at most :data:`_DIALOG_MAX_LINES` lines,
    none wider than :data:`_DIALOG_MAX_CELLS` cells. Styling (the ``[ok]``/``[err]``
    markup on outcome notes) is preserved in the joined message.

    Args:
        buffered: The renderables collected since the last present.

    Returns:
        The lines joined into one :class:`Text`, or ``None`` when the output belongs in
        the scrollable result window instead.
    """
    if not buffered or not all(isinstance(item, Text) for item in buffered):
        return None
    lines: list[Text] = []
    for item in buffered:
        lines.extend(item.split("\n") or [item])
    if len(lines) > _DIALOG_MAX_LINES:
        return None
    if any(line.cell_len > _dialog_max_cells() for line in lines):
        return None
    return Text("\n").join(lines)


class Ui:
    """Abstract UI surface. See :class:`PlainUi` and :class:`TuiUi` for the two backends."""

    def show(self, *renderables: RenderableType) -> None:
        """Display one or more Rich renderables (tables, panels, text)."""
        raise NotImplementedError

    def note(self, markup: str) -> None:
        """Display a short line of Rich-markup text."""
        raise NotImplementedError

    def ack(self, markup: str) -> None:
        """Acknowledge that an action was carried out — in the menu only.

        The distinction from :meth:`note` is who the line is for. A note is *output*: the
        answer the caller asked for. An acknowledgement is reassurance that a thing
        happened — "✓ device clock set", "✓ channel 2 = #general" — which a person watching
        a screen needs and a script does not, because the exit status already said it and
        the line would only be something to filter out of the real output.

        Both surfaces show them; what differs is *where*. The menu prints them in its
        result window. The CLI prints them on **stderr** (see :meth:`PlainUi.ack`), which
        keeps the promise the dropping was made to keep — ``meshterm contacts > f`` still
        catches only the answer — while giving the person at the prompt back the ✓ and the
        count they were losing to a rule written for a redirect they were not doing.
        """
        raise NotImplementedError

    async def view(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Show a renderable immediately in a dismissable window (prints in CLI mode)."""
        raise NotImplementedError

    async def present(self, *, title: str = "") -> None:
        """Flush any buffered output to the user (a no-op when output is immediate)."""

    def discard(self) -> None:
        """Drop any buffered-but-unshown output (a no-op when output is immediate)."""

    def progress(self, title: str = "Working"):  # noqa: ANN201 - context manager, varies by backend
        """Return a progress context manager exposing ``add_task``/``advance``/``update``."""
        raise NotImplementedError

    def busy_overlay(self, message: str = "", *, title: str = ""):  # noqa: ANN201 - async CM, varies by backend
        """Return an async context manager that floats a "working" skeleton while its block runs.

        In the interactive menu this shows the top-most skeleton card (see
        :meth:`~meshterm.ui.tui.session.TuiSession.busy_overlay`), used to cover the lag of a
        Bluetooth operation that would otherwise leave the screen blank. In scripted CLI mode
        it does nothing (there is no full-screen surface to float over).
        """
        raise NotImplementedError

    def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201 - async CM, varies by backend
        """Return an async context manager that floats a **modal** busy card over a screen.

        The counterpart to :meth:`busy_overlay`, for slow work a *screen* starts rather than
        work that happens in the gap between screens. The overlay is not a screen at all: it
        draws only on an empty stack and it takes no keys, so a hub that stays pushed while
        it works gets neither the card nor the protection. This one is pushed and modal — see
        :meth:`~meshterm.ui.tui.session.TuiSession.busy_dialog` for what that buys. In
        scripted CLI mode it does nothing, as the overlay does.
        """
        raise NotImplementedError

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        filterable: bool = True,
        delete_hint: str = "",
        floating: bool = False,
    ) -> Any:
        """Prompt the user to choose one item; return its value or ``None`` if cancelled.

        ``prompt`` draws an instruction inside the popup, above the list. Pass
        ``filterable=False`` for a short, fixed list so a stray key can't narrow (and
        resize) it. ``delete_hint`` (with rows marked deletable) enables the Delete
        key's remove flow, and ``floating`` keeps a lead-in question drawn as a box even
        with nothing under it — see :meth:`~meshterm.ui.tui.session.TuiSession.select`.
        """
        raise NotImplementedError

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: str | None = None,
        footer_hint: str | None = None,
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
    ) -> Any:
        """Choose one item on a chromeless startup splash; ``None`` if skipped.

        ``keys`` declares bare-key shortcuts the splash answers, resolving with a
        :class:`~meshterm.ui.tui.select.KeyRequest` (the picker's hide/show-all pair), and
        ``key_hint`` names them per highlighted row so the footer only advertises a key
        where it would act. ``footer_hint`` replaces the base sentence.
        """
        raise NotImplementedError

    async def confirm_startup(
        self,
        prompt: str | Text,
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Any = None,
        footnote: str | None = None,
        backdrop_items: list | None = None,
        backdrop_default: Any = None,
    ) -> bool:
        """Confirm a destructive action on the startup splash; ``True`` only if committed.

        ``backdrop_items`` (the picker's rows) floats the confirm over a redrawn device list;
        ``backdrop_default`` pre-highlights the row it acts on.
        """
        raise NotImplementedError

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> None:
        """Show a message on a chromeless startup splash until the user dismisses it."""
        raise NotImplementedError

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> Any:
        """Await ``coro`` while showing a spinner on the startup splash; return its result."""
        raise NotImplementedError

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Ask for a Bluetooth companion's pairing PIN on the splash; ``None`` if cancelled."""
        raise NotImplementedError

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Ask for a line of text on a chromeless startup splash; ``None`` if cancelled."""
        raise NotImplementedError

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Let the user rearrange rows with the arrows; return the new order of row indices."""
        raise NotImplementedError

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        floating: bool = False,
    ) -> str | None:
        """Prompt for a line of text; return it or ``None`` if cancelled.

        Keep ``title`` short (it heads the popup's border) and put the question/instruction
        in ``prompt`` (drawn above the field), so a text popup reads like the button dialogs.
        ``floating`` forces the prompt to draw as a centered popup even with nothing beneath
        it (a mid-flow modal such as a remote-admin password) instead of filling the frame;
        surfaces without a screen stack ignore it.
        """
        raise NotImplementedError

    async def confirm(self, title: str, *, default: bool = True) -> bool | None:
        """Prompt yes/no; return the answer or ``None`` if cancelled."""
        raise NotImplementedError

    async def dialog(
        self,
        prompt: str,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, Any] | None = None,
        danger: bool = False,
        destructive: bool = False,
    ) -> Any:
        """Show a centered button dialog; return the chosen value or ``None`` on Esc.

        The general choose-one popup (a prompt above a row of buttons). Two escalating
        cautionary tiers theme the prompt and border: ``danger`` (amber) for a disruptive
        choice — discarding edits, a reboot — and ``destructive`` (the reserved error red)
        for irreversible data loss, so a delete confirm reads red like its typed-confirm
        sibling. Buttons follow the platform-dialog convention: the safe way out sits on
        the left and the committing action on the right, which is also the sensible
        ``default`` so Enter commits it while Esc always backs out.
        """
        raise NotImplementedError

    async def typed_confirm(self, warning: str, word: str, *, title: str = "Are you sure?") -> bool:
        """Gate a destructive action behind typing ``word``; return whether it was typed."""
        raise NotImplementedError

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
    ) -> str | None:
        """Prompt for free text with suggestions; return it or ``None`` if cancelled."""
        raise NotImplementedError

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> str | None:
        """Prompt for a filesystem path; return it or ``None`` if cancelled."""
        raise NotImplementedError


class PlainUi(Ui):
    """CLI surface: print straight to the scripted console; interactive prompts are unsupported.

    The console it prints on emits no colour, wraps nothing, and trims trailing whitespace
    (:func:`meshterm.ui.script.console`), and everything a tool hands over passes through
    :func:`~meshterm.ui.script.flatten` on the way out, so no border, box or hoisted title
    reaches stdout. That fold is the net, not the design: a surface a script is meant to
    read is *written* in the CLI's own vocabulary — see :mod:`meshterm.ui.script`.
    """

    def __init__(self, console: Console) -> None:
        """Bind the surface to a Rich console.

        Args:
            console: The console tool output is printed to.
        """
        self.console = console

    def show(self, *renderables: RenderableType) -> None:
        """Print each renderable immediately, unframed (see :func:`~meshterm.ui.script.flatten`)."""
        from . import script

        for renderable in renderables:
            for item in script.flatten(renderable):
                self.console.print(item)

    def note(self, markup: str) -> None:
        """Print a markup line to the console immediately, as plain text.

        The parameter is markup — that is the shared surface's contract, and forty-odd
        callers write it — but the scripted console does not interpret markup: a node
        broadcasts its own name, and Rich would read ``[...]`` in one as a style tag. So
        the tags are resolved *here* rather than by the console, and what reaches stdout
        is the text they were wrapping. Printing them raw put ``[muted]`` and ``[/muted]``
        around the private key that ``config export-key > key.hex`` is supposed to be the
        whole content of.

        Malformed markup — which is what a *name* holding a bracket looks like — is not an
        error to report but a string to print, so it falls through verbatim.
        """
        from rich.errors import MarkupError
        from rich.markup import render

        try:
            self.console.print(render(markup).plain)
        except MarkupError:
            self.console.print(markup)

    def ack(self, markup: str) -> None:
        """Print it on stderr, where everything *about* the run goes.

        These used to be dropped, and the reason was sound as far as it went: an
        acknowledgement is not the answer, and a caller redirecting stdout must catch only
        the answer. But stdout is not the only stream — putting them on stderr honours that
        rule exactly, and a person watching a five-minute ``config restore`` gets the
        setting-by-setting ✓ they had no way to see.

        The markup is resolved here rather than by the console, as :meth:`note` explains,
        and a *name* holding a square bracket is not an error to report but a string to
        print — so malformed markup falls through verbatim.
        """
        from rich.errors import MarkupError
        from rich.markup import render

        from . import script

        try:
            line: Text | str = render(markup)
        except MarkupError:
            line = markup
        script.stderr_console().print(line, highlight=False)

    async def view(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Print the renderable immediately, unframed (there is no windowing in CLI mode)."""
        from . import script

        for item in script.flatten(renderable):
            self.console.print(item)

    def progress(self, title: str = "Working"):  # noqa: ANN201
        """Return a progress bar drawn on stderr, so it never lands in piped output.

        A progress bar is a courtesy to someone watching a long command run, and noise in
        anything reading the command's output. stderr is where it belongs: visible in a
        terminal, absent from ``meshterm contacts > contacts.txt``. Rich also draws
        nothing at all when stderr is not a terminal, so a fully redirected run is silent.
        """
        from . import script
        from .widgets import make_progress

        console = script.stderr_console()
        progress = make_progress(console)
        progress.disable = not console.is_terminal
        return progress

    @asynccontextmanager
    async def busy_overlay(self, message: str = "", *, title: str = "") -> AsyncIterator[None]:
        """Do nothing: the scripted CLI has no full-screen surface to float a skeleton over."""
        yield

    @asynccontextmanager
    async def busy_dialog(self, message: str = "", *, title: str = "") -> AsyncIterator[_NullBusy]:
        """Do nothing: there is no screen to interrupt and no keyboard to take.

        Still yields something with a settable caption, because a caller that retitles the
        card mid-batch must not have to ask which surface it is running on.
        """
        yield _NullBusy()

    def _no_prompt(self) -> RuntimeError:
        """Build the error raised if a rich prompt is reached on the non-interactive path."""
        return RuntimeError("interactive prompts are only available in the menu")

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        filterable: bool = True,
        delete_hint: str = "",
        floating: bool = False,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: str | None = None,
        footer_hint: str | None = None,
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def confirm_startup(
        self,
        prompt: str | Text,
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Any = None,
        footnote: str | None = None,
        backdrop_items: list | None = None,
        backdrop_default: Any = None,
    ) -> bool:
        """Unsupported in scripted CLI mode — the picker splash is interactive-only."""
        raise self._no_prompt()

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> Any:
        """No splash in scripted CLI mode; just await the task and return its result."""
        return await coro

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Unsupported in scripted CLI mode — a PIN must be supplied non-interactively.

        The scripted path can't pop a dialog, so a PIN-protected device is handled by the
        clean ``DeviceAuthenticationError`` message (pass ``--ble-pin``) rather than a prompt.
        """
        raise self._no_prompt()

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Unsupported in scripted CLI mode — a network endpoint comes from ``--tcp`` instead."""
        raise self._no_prompt()

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        floating: bool = False,
    ) -> str | None:
        """Prompt on the terminal (line editor / getpass), re-asking until valid.

        A few tools (e.g. a remote-admin password) can legitimately prompt from a scripted
        run when no flag was supplied, so this stays functional on the CLI. An in-body
        ``prompt`` (used by the interactive popups) is printed once as a lead-in line here.
        ``floating`` is a full-screen-popup nicety with no meaning on the plain terminal, so
        it is accepted and ignored.

        Returns:
            The entered string, or ``None`` on EOF / interrupt.
        """
        import getpass

        if prompt:
            self.console.print(prompt)
        head = f"{title} " if not default else f"{title} [{default}] "
        prompt = head
        while True:
            try:
                raw = getpass.getpass(prompt) if password else input(prompt)
            except (EOFError, KeyboardInterrupt):
                return None
            value = raw if raw != "" else default
            if validate is not None:
                result = validate(value)
                if result is not True:
                    self.console.print(f"[err]{result}[/err]")
                    continue
            return value

    async def confirm(self, title: str, *, default: bool = True) -> bool | None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def dialog(
        self,
        prompt: str,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, Any] | None = None,
        danger: bool = False,
        destructive: bool = False,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def typed_confirm(self, warning: str, word: str, *, title: str = "Are you sure?") -> bool:
        """Unsupported in scripted CLI mode (destructive CLI commands gate on ``--yes``)."""
        raise self._no_prompt()

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
    ) -> str | None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> str | None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()


class TuiUi(Ui):
    """Interactive surface: buffer output for a result window; route prompts to the session."""

    def __init__(self, session: TuiSession) -> None:
        """Bind the surface to a running session.

        Args:
            session: The full-screen session that renders prompts and windows.
        """
        self.session = session
        self._buffer: list[RenderableType] = []

    # --- output --------------------------------------------------------------

    def show(self, *renderables: RenderableType) -> None:
        """Collect renderables for the next result window."""
        self._buffer.extend(renderables)

    def note(self, markup: str) -> None:
        """Collect a markup line for the next result window."""
        self._buffer.append(Text.from_markup(markup))

    def ack(self, markup: str) -> None:
        """Collect an acknowledgement — on this surface, exactly a note."""
        self.note(markup)

    async def present(self, *, title: str = "") -> None:
        """Show everything buffered since the last present; window or popup to fit.

        A short, text-only outcome (a line or two of notes — "✓ flood advertisement
        sent") floats as a centered OK dialog over the current screen, so a one-line
        result never commandeers the whole frame; anything bigger, or anything holding
        a table/panel, opens the bounded scrollable result window as before (see
        :func:`_collapse_to_message` for the exact gate). Clears the buffer afterward.
        Does nothing if nothing was buffered (e.g. a tool that only produced a file
        artifact and an empty message).

        Args:
            title: Heading for the result window or popup.
        """
        if not self._buffer:
            return
        buffered = self._buffer
        self._buffer = []
        message = _collapse_to_message(buffered)
        if message is not None:
            await self.session.message_dialog(message, title=title)
            return
        body = buffered[0] if len(buffered) == 1 else Group(*buffered)
        await self.session.scroll(body, title=title)

    def discard(self) -> None:
        """Drop any buffered output without showing it."""
        self._buffer = []

    async def view(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Show a renderable immediately in a dismissable scroll window."""
        await self.session.scroll(renderable, title=title, footer_hint=footer_hint)

    def progress(self, title: str = "Working"):  # noqa: ANN201
        """Return a progress dialog context manager for the session."""
        return self.session.progress(title)

    def busy_overlay(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        """Float the session's top-most skeleton card while the wrapped block runs."""
        return self.session.busy_overlay(message, title=title)

    def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        """Push the session's modal busy card over the current screen while the block runs."""
        return self.session.busy_dialog(message, title=title)

    # --- input ---------------------------------------------------------------

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        filterable: bool = True,
        delete_hint: str = "",
        floating: bool = False,
    ) -> Any:
        """Delegate to the session's select screen."""
        return await self.session.select(
            title,
            items,
            prompt=prompt,
            default=default,
            filterable=filterable,
            delete_hint=delete_hint,
            floating=floating,
        )

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: str | None = None,
        footer_hint: str | None = None,
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
    ) -> Any:
        """Delegate to the session's chromeless startup select splash."""
        return await self.session.select_startup(
            title,
            items,
            default=default,
            banner=banner,
            footnote=footnote,
            keys=keys,
            key_hint=key_hint,
            **({} if footer_hint is None else {"footer_hint": footer_hint}),
        )

    async def confirm_startup(
        self,
        prompt: str | Text,
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Any = None,
        footnote: str | None = None,
        backdrop_items: list | None = None,
        backdrop_default: Any = None,
    ) -> bool:
        """Delegate to the session's startup confirm dialog (floated over the picker)."""
        return await self.session.confirm_startup(
            prompt,
            title=title,
            confirm_label=confirm_label,
            banner=banner,
            footnote=footnote,
            backdrop_items=backdrop_items,
            backdrop_default=backdrop_default,
        )

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> None:
        """Delegate to the session's chromeless startup message splash."""
        await self.session.notify_startup(renderable, title=title, banner=banner, footnote=footnote)

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> Any:
        """Delegate to the session's animated-spinner startup splash."""
        return await self.session.busy_startup(
            message, coro, title=title, banner=banner, footnote=footnote
        )

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Delegate to the session's startup PIN dialog."""
        return await self.session.prompt_pin_startup(
            device_name, error=error, help_text=help_text, banner=banner, footnote=footnote
        )

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Delegate to the session's chromeless startup text dialog."""
        return await self.session.prompt_text_startup(
            title,
            prompt=prompt,
            default=default,
            validate=validate,
            help_text=help_text,
            banner=banner,
            footnote=footnote,
        )

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Delegate to the session's reorder screen."""
        return await self.session.reorder(title, labels)

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        byte_limit: int | None = None,
        floating: bool = False,
    ) -> str | None:
        """Delegate to the session's text screen."""
        return await self.session.text(
            title,
            prompt=prompt,
            default=default,
            validate=validate,
            help_text=help_text,
            password=password,
            byte_limit=byte_limit,
            floating=floating,
        )

    async def confirm(self, title: str, *, default: bool = True) -> bool | None:
        """Delegate to the session's confirm screen."""
        return await self.session.confirm(title, default=default)

    async def dialog(
        self,
        prompt: str,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, Any] | None = None,
        danger: bool = False,
        destructive: bool = False,
    ) -> Any:
        """Delegate to the session's button dialog, in the caution tier the caller asked for.

        ``danger`` themes the frame cautionary; ``destructive`` uses the reserved error
        red, and is only for irreversible data loss.
        """
        tier = "err" if destructive else "warn" if danger else ""
        return await self.session.button_dialog(
            prompt,
            buttons,
            title=title,
            default=default,
            keys=keys,
            prompt_style=tier,
            border_style=tier or "accent",
        )

    async def typed_confirm(self, warning: str, word: str, *, title: str = "Are you sure?") -> bool:
        """Delegate to the session's typed-confirmation dialog."""
        return await self.session.typed_confirm(warning, word, title=title)

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
    ) -> str | None:
        """Delegate to the session's autocomplete screen."""
        return await self.session.autocomplete(
            title, choices, prompt=prompt, default=default, validate=validate
        )

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> str | None:
        """Prompt for a path as free text (with the current value prefilled)."""
        return await self.session.text(
            title, prompt=prompt, default=default, help_text="filesystem path"
        )
