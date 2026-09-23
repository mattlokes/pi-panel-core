"""Shared Textual building blocks for the pi-panel TUIs.

Both `pi-panel-ctl tui` and `pi-panel-pkg tui` are built from these, so they
share a look (app.tcss), a layout (status bar, main table, log pane, footer),
modals, and key conventions (`q` leaves the TUI; destructive actions ask
first).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Footer, Header, Input, Label, RichLog, Static

CSS_PATH = Path(__file__).with_name("app.tcss")


def dot(up: bool) -> str:
    return "[green]●[/green]" if up else "[red]●[/red]"


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no gate for destructive actions. Escape cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.question = question
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(self.question, classes="modal-title")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", variant="primary", id="cancel")
                yield Button(self.confirm_label, variant="error", id="confirm")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass(frozen=True, slots=True)
class Field:
    key: str
    label: str
    placeholder: str = ""
    value: str = ""
    required: bool = True


class FormScreen(ModalScreen[dict[str, str] | None]):
    """A small form: one Input per Field. Dismisses with {key: value} or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, fields: Iterable[Field], submit_label: str = "OK") -> None:
        super().__init__()
        self.title_text = title
        self.fields = list(fields)
        self.submit_label = submit_label

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="modal-box"):
            yield Label(self.title_text, classes="modal-title")
            for f in self.fields:
                yield Label(f.label, classes="field-label")
                yield Input(value=f.value, placeholder=f.placeholder, id=f"field-{f.key}")
            with Horizontal(classes="modal-buttons"):
                yield Button("Cancel", variant="primary", id="cancel")
                yield Button(self.submit_label, variant="success", id="ok")

    def on_mount(self) -> None:
        if self.fields:
            self.query_one(f"#field-{self.fields[0].key}", Input).focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._submit()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self._submit()

    def _submit(self) -> None:
        values = {f.key: self.query_one(f"#field-{f.key}", Input).value.strip() for f in self.fields}
        missing = [f.label for f in self.fields if f.required and not values[f.key]]
        if missing:
            self.notify(f"Required: {', '.join(missing)}", severity="warning")
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PiPanelApp(App[None]):
    """Base app: header, status bar, the subclass's body, log pane, footer."""

    CSS_PATH = CSS_PATH
    BINDINGS = [Binding("q,ctrl+c", "quit", "Exit")]

    def body(self) -> Iterable[Widget]:
        """The subclass's main widgets, between the status bar and the log."""
        return []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("connecting…", id="statusbar")
        yield from self.body()
        yield RichLog(id="log", markup=True, wrap=True, max_lines=1000)
        yield Footer()

    def set_status(self, text: str) -> None:
        self.query_one("#statusbar", Static).update(text)

    def log_line(self, message: str, error: bool = False) -> None:
        """Log to the pane; errors also raise a toast (an error scrolled off
        the log is an error missed)."""
        log = self.query_one("#log", RichLog)
        if error:
            log.write(f"[red]{message}[/red]")
            self.notify(message, severity="error")
        else:
            log.write(message)


__all__ = ["ConfirmScreen", "Field", "FormScreen", "PiPanelApp", "dot"]
