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

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

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


def _collapse_to_message(buffered: list[RenderableType]) -> Optional[Text]:
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
    if any(line.cell_len > _DIALOG_MAX_CELLS for line in lines):
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

    def busy_overlay(self, message: str = ""):  # noqa: ANN201 - async CM, varies by backend
        """Return an async context manager that floats a "working" spinner while its block runs.

        In the interactive menu this shows the top-most ring spinner (see
        :meth:`~meshterm.ui.tui.session.TuiSession.busy_overlay`), used to cover the lag of a
        Bluetooth operation that would otherwise leave the screen blank. In scripted CLI mode
        it does nothing (there is no full-screen surface to float over)."""
        raise NotImplementedError

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        wrap: bool = True,
        filterable: bool = True,
    ) -> Any:
        """Prompt the user to choose one item; return its value or ``None`` if cancelled.

        ``prompt`` draws an instruction inside the popup, above the list. Pass
        ``filterable=False`` for a short, fixed list so a stray key can't narrow (and
        resize) it.
        """
        raise NotImplementedError

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: Optional[str] = None,
    ) -> Any:
        """Choose one item on a chromeless startup splash; ``None`` if skipped."""
        raise NotImplementedError

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: Optional[str] = None,
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
        footnote: Optional[str] = None,
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
        footnote: Optional[str] = None,
    ) -> Optional[str]:
        """Ask for a Bluetooth companion's pairing PIN on the startup splash; ``None`` if cancelled."""
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
        validate: Optional[Validator] = None,
        help_text: str = "",
        password: bool = False,
    ) -> Optional[str]:
        """Prompt for a line of text; return it or ``None`` if cancelled.

        Keep ``title`` short (it heads the popup's border) and put the question/instruction
        in ``prompt`` (drawn above the field), so a text popup reads like the button dialogs.
        """
        raise NotImplementedError

    async def confirm(self, title: str, *, default: bool = True) -> Optional[bool]:
        """Prompt yes/no; return the answer or ``None`` if cancelled."""
        raise NotImplementedError

    async def dialog(
        self,
        prompt: str,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: Optional[dict[str, Any]] = None,
        danger: bool = False,
    ) -> Any:
        """Show a centered button dialog; return the chosen value or ``None`` on Esc.

        The general choose-one popup (a prompt above a row of buttons). ``danger`` themes
        the prompt and border in the cautionary style for destructive or disruptive
        choices. Buttons follow the platform-dialog convention: the safe way out sits on
        the left and the committing action on the right, which is also the sensible
        ``default`` so Enter commits it while Esc always backs out.
        """
        raise NotImplementedError

    async def typed_confirm(
        self, warning: str, word: str, *, title: str = "Are you sure?"
    ) -> bool:
        """Gate a destructive action behind typing ``word``; return whether it was typed."""
        raise NotImplementedError

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Optional[Validator] = None,
    ) -> Optional[str]:
        """Prompt for free text with suggestions; return it or ``None`` if cancelled."""
        raise NotImplementedError

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> Optional[str]:
        """Prompt for a filesystem path; return it or ``None`` if cancelled."""
        raise NotImplementedError


class PlainUi(Ui):
    """CLI surface: print directly to the console; interactive prompts are unsupported."""

    def __init__(self, console: Console) -> None:
        """Bind the surface to a Rich console.

        Args:
            console: The console tool output is printed to.
        """
        self.console = console

    def show(self, *renderables: RenderableType) -> None:
        """Print each renderable to the console immediately."""
        for renderable in renderables:
            self.console.print(renderable)

    def note(self, markup: str) -> None:
        """Print a markup line to the console immediately."""
        self.console.print(markup)

    async def view(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Print the renderable immediately (there is no windowing in CLI mode)."""
        self.console.print(renderable)

    def progress(self, title: str = "Working"):  # noqa: ANN201
        """Return the Rich progress bar used for scripted runs."""
        from .widgets import make_progress

        return make_progress(self.console)

    @asynccontextmanager
    async def busy_overlay(self, message: str = "") -> AsyncIterator[None]:
        """Do nothing: the scripted CLI has no full-screen surface to float a spinner over."""
        yield

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
        wrap: bool = True,
        filterable: bool = True,
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
        footnote: Optional[str] = None,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: Optional[str] = None,
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
        footnote: Optional[str] = None,
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
        footnote: Optional[str] = None,
    ) -> Optional[str]:
        """Unsupported in scripted CLI mode — a PIN must be supplied non-interactively.

        The scripted path can't pop a dialog, so a PIN-protected device is handled by the
        clean ``DeviceAuthenticationError`` message (pass ``--ble-pin``) rather than a prompt.
        """
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
        validate: Optional[Validator] = None,
        help_text: str = "",
        password: bool = False,
    ) -> Optional[str]:
        """Prompt on the terminal (line editor / getpass), re-asking until valid.

        A few tools (e.g. a remote-admin password) can legitimately prompt from a scripted
        run when no flag was supplied, so this stays functional on the CLI. An in-body
        ``prompt`` (used by the interactive popups) is printed once as a lead-in line here.

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

    async def confirm(self, title: str, *, default: bool = True) -> Optional[bool]:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def dialog(
        self,
        prompt: str,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: Optional[dict[str, Any]] = None,
        danger: bool = False,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def typed_confirm(
        self, warning: str, word: str, *, title: str = "Are you sure?"
    ) -> bool:
        """Unsupported in scripted CLI mode (destructive CLI commands gate on ``--yes``)."""
        raise self._no_prompt()

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Optional[Validator] = None,
    ) -> Optional[str]:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> Optional[str]:
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

    def busy_overlay(self, message: str = ""):  # noqa: ANN201
        """Float the session's top-most ring spinner while the wrapped block runs."""
        return self.session.busy_overlay(message)

    # --- input ---------------------------------------------------------------

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        wrap: bool = True,
        filterable: bool = True,
    ) -> Any:
        """Delegate to the session's select screen."""
        return await self.session.select(
            title, items, prompt=prompt, default=default, wrap=wrap, filterable=filterable
        )

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: Optional[str] = None,
    ) -> Any:
        """Delegate to the session's chromeless startup select splash."""
        return await self.session.select_startup(
            title, items, default=default, banner=banner, footnote=footnote
        )

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: Optional[str] = None,
    ) -> None:
        """Delegate to the session's chromeless startup message splash."""
        await self.session.notify_startup(
            renderable, title=title, banner=banner, footnote=footnote
        )

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any = None,
        footnote: Optional[str] = None,
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
        footnote: Optional[str] = None,
    ) -> Optional[str]:
        """Delegate to the session's startup PIN dialog."""
        return await self.session.prompt_pin_startup(
            device_name, error=error, help_text=help_text, banner=banner, footnote=footnote
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
        validate: Optional[Validator] = None,
        help_text: str = "",
        password: bool = False,
    ) -> Optional[str]:
        """Delegate to the session's text screen."""
        return await self.session.text(
            title,
            prompt=prompt,
            default=default,
            validate=validate,
            help_text=help_text,
            password=password,
        )

    async def confirm(self, title: str, *, default: bool = True) -> Optional[bool]:
        """Delegate to the session's confirm screen."""
        return await self.session.confirm(title, default=default)

    async def dialog(
        self,
        prompt: str,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: Optional[dict[str, Any]] = None,
        danger: bool = False,
    ) -> Any:
        """Delegate to the session's button dialog, themed cautionary when ``danger``."""
        return await self.session.button_dialog(
            prompt,
            buttons,
            title=title,
            default=default,
            keys=keys,
            prompt_style="warn" if danger else "",
            border_style="warn" if danger else "accent",
        )

    async def typed_confirm(
        self, warning: str, word: str, *, title: str = "Are you sure?"
    ) -> bool:
        """Delegate to the session's typed-confirmation dialog."""
        return await self.session.typed_confirm(warning, word, title=title)

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Optional[Validator] = None,
    ) -> Optional[str]:
        """Delegate to the session's autocomplete screen."""
        return await self.session.autocomplete(
            title, choices, prompt=prompt, default=default, validate=validate
        )

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> Optional[str]:
        """Prompt for a path as free text (with the current value prefilled)."""
        return await self.session.text(
            title, prompt=prompt, default=default, help_text="filesystem path"
        )
