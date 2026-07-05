"""A progress screen whose handle mimics the slice of ``rich.progress`` the tools use.

Trace and TX-optimize sweeps drive progress with ``add_task`` / ``advance`` / ``update`` on a
Rich ``Progress`` in CLI mode. In the TUI the same calls update a bounded, centered progress
dialog instead, so tool code stays identical across both front-ends (see
:meth:`meshtools.ui.surface.PlainUi.progress` vs :meth:`~meshtools.ui.surface.TuiUi.progress`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from rich.console import Group
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .render import render_lines
from .screen import Screen

if TYPE_CHECKING:
    from .session import TuiSession


@dataclass
class _Task:
    """One tracked progress task."""

    description: str
    total: Optional[float]
    completed: float = 0.0


class ProgressScreen(Screen):
    """A non-interactive dialog showing one or more running progress bars."""

    footer_hint = "working…"
    floating = True

    def __init__(self, title: str = "Working") -> None:
        """Start an empty progress dialog with the given heading."""
        super().__init__()
        self.title = title
        self._tasks: dict[int, _Task] = {}
        self._next = 0

    def add_task(self, description: str, total: Optional[float] = None) -> int:
        """Add a task and return its id (mirrors ``rich.progress.Progress.add_task``)."""
        task_id = self._next
        self._next += 1
        self._tasks[task_id] = _Task(description=description, total=total)
        return task_id

    def advance(self, task_id: int, amount: float = 1.0) -> None:
        """Advance a task's completed count (mirrors ``Progress.advance``)."""
        self._tasks[task_id].completed += amount

    def update(
        self,
        task_id: int,
        *,
        description: Optional[str] = None,
        completed: Optional[float] = None,
        total: Optional[float] = None,
    ) -> None:
        """Update a task's fields (mirrors the ``Progress.update`` kwargs used)."""
        task = self._tasks[task_id]
        if description is not None:
            task.description = description
        if completed is not None:
            task.completed = completed
        if total is not None:
            task.total = total

    def render_body(self, width: int) -> list[str]:
        """Render each task as ``description [bar] m/n``."""
        bar_width = max(10, min(40, width - 24))
        table = Table.grid(padding=(0, 1))
        table.add_column()  # description
        table.add_column()  # bar
        table.add_column(justify="right")  # counts
        for task in self._tasks.values():
            bar = ProgressBar(
                total=task.total or None,
                completed=task.completed,
                width=bar_width,
                complete_style="brand",
                finished_style="ok",
            )
            counts = (
                f"{int(task.completed)}/{int(task.total)}"
                if task.total
                else str(int(task.completed))
            )
            table.add_row(Text(task.description, style="accent"), bar, Text(counts, style="muted"))
        body = table if self._tasks else Text("starting…", style="muted")
        return render_lines(Group(body), width)

    def handle(self, action: str, data: str = "") -> None:
        """Swallow all keys: progress advances with the work, not the keyboard."""
        return


@dataclass
class TuiProgress:
    """Context manager yielding a :class:`ProgressScreen` handle, pushed for its lifetime.

    Presents the same ``with ... as progress:`` shape as ``make_progress`` so tools need
    only swap the factory. Entering pushes the dialog; the ``add_task`` / ``advance`` /
    ``update`` methods mutate it and repaint; exiting pops it.
    """

    session: "TuiSession"
    title: str = "Working"
    _screen: ProgressScreen = field(init=False, default=None)  # type: ignore[assignment]

    def __enter__(self) -> ProgressScreen:
        """Push the progress dialog and return it as the handle."""
        self._screen = ProgressScreen(self.title)
        # Wrap add_task/advance/update so each repaints the session automatically.
        for name in ("add_task", "advance", "update"):
            setattr(self._screen, name, self._wrap(getattr(self._screen, name)))
        self.session.push(self._screen)
        return self._screen

    def __exit__(self, *exc: object) -> None:
        """Pop the progress dialog."""
        self.session.pop(self._screen)

    def _wrap(self, method):  # type: ignore[no-untyped-def]
        """Wrap a mutating method so it repaints the session after running."""

        def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = method(*args, **kwargs)
            self.session.invalidate()
            return result

        return wrapped
