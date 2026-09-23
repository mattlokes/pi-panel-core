"""pi-panel-pkg tui: the package manager, interactively.

Same layout and conventions as `pi-panel-ctl tui` (both come from
pi-panel-tui-kit). Long operations run as workers and stream their output
into the log pane; only one package operation runs at a time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from textual import on, work
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable

from pi_panel_tui_kit import ConfirmScreen, Field, FormScreen, PiPanelApp, dot

from . import paths, scaffold
from .manager import Manager, PackageError
from .manifest import Manifest


class PluginManagerApp(PiPanelApp):
    TITLE = "pi-panel packages"
    BINDINGS = [
        *PiPanelApp.BINDINGS,
        Binding("i", "install", "Install"),
        Binding("u", "update", "Update"),
        Binding("U", "update_all", "Update all"),
        Binding("d", "remove", "Remove"),
        Binding("e", "toggle_enabled", "Enable/disable"),
        Binding("enter", "info", "Info", show=False),
        Binding("n", "new", "New"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, manager_factory: Callable[..., Manager] = Manager) -> None:
        super().__init__()
        self._factory = manager_factory
        self.manager: Manager | None = None
        self.entries: list[Any] = []
        self.ctl: dict[str, dict[str, Any]] | None = None
        self.busy = False

    def body(self) -> Iterable[Widget]:
        yield DataTable(id="packages", classes="main-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#packages", DataTable)
        table.add_columns("name", "kind", "version", "revision", "enabled", "unit", "source")
        self.manager = self._factory(output=self.log_line)
        self.sub_title = str(paths.home())
        self.action_refresh()

    # --- rendering -------------------------------------------------------

    @work(exclusive=True, group="refresh")
    async def action_refresh(self) -> None:
        assert self.manager is not None
        self.entries = self.manager.list()
        self.ctl = await self.manager.ctl_apps()
        table = self.query_one("#packages", DataTable)
        row = table.cursor_row
        table.clear()
        for e in self.entries:
            info = (self.ctl or {}).get(e.name)
            table.add_row(
                e.name, e.kind, e.version or "-",
                (e.source.commit or e.source.sha256 or "")[:10],
                ("yes" if info["enabled"] else "no") if info else "?",
                info["unit_state"] if info else "?",
                e.source.url,
                key=e.name,
            )
        if row is not None and row < table.row_count:
            table.move_cursor(row=row)
        self.set_status(
            f"ctl {dot(self.ctl is not None)}   {len(self.entries)} package(s)"
            + ("" if self.ctl is not None else "   [yellow]ctl offline: enable/disable unavailable[/yellow]")
            + ("   [yellow]working…[/yellow]" if self.busy else "")
        )

    def _selected(self) -> Any | None:
        table = self.query_one("#packages", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self.entries):
            return None
        return self.entries[table.cursor_row]

    def _start(self) -> bool:
        if self.busy:
            self.notify("another operation is still running", severity="warning")
            return False
        self.busy = True
        self.action_refresh()
        return True

    def _finish(self) -> None:
        self.busy = False
        self.action_refresh()

    # --- actions ---------------------------------------------------------

    @work(exclusive=True, group="op")
    async def action_install(self) -> None:
        form = await self.push_screen_wait(FormScreen(
            "Install a package",
            [Field("source", "Source", "https://github.com/you/pi-panel-app-x.git#main, "
                   "a .tar.gz, or a directory")],
            submit_label="Fetch",
        ))
        if not form or not self._start():
            return
        assert self.manager is not None

        async def confirm(manifest: Manifest) -> bool:
            lines = [f"Install {manifest.name} ({manifest.kind}) {manifest.version or ''}?", ""]
            if manifest.description:
                lines.append(manifest.description)
            lines.append(f"runs:  {manifest.exec}")
            if manifest.build:
                lines.append(f"build: {manifest.build}")
            lines += ["", "Its build command and program run as this user."]
            return await self.push_screen_wait(ConfirmScreen("\n".join(lines), "Install"))

        try:
            self.log_line(f"[b]install[/b] {form['source']}")
            entry = await self.manager.install(form["source"], confirm=confirm)
            self.log_line(f"[green]✓ installed {entry.name}[/green] — press e to enable it")
        except PackageError as exc:
            self.log_line(f"install failed: {exc}", error=True)
        finally:
            self._finish()

    async def _update(self, names: list[str]) -> None:
        assert self.manager is not None
        for name in names:
            try:
                self.log_line(f"[b]update[/b] {name}")
                await self.manager.update(name)
                self.log_line(f"[green]✓ updated {name}[/green]")
            except PackageError as exc:
                self.log_line(f"update of {name} failed: {exc}", error=True)

    @work(exclusive=True, group="op")
    async def action_update(self) -> None:
        entry = self._selected()
        if entry is None or not self._start():
            return
        try:
            await self._update([entry.name])
        finally:
            self._finish()

    @work(exclusive=True, group="op")
    async def action_update_all(self) -> None:
        if not self.entries:
            return
        ok = await self.push_screen_wait(ConfirmScreen(
            f"Update all {len(self.entries)} package(s)?", "Update all"))
        if not ok or not self._start():
            return
        try:
            await self._update([e.name for e in self.entries])
        finally:
            self._finish()

    @work(exclusive=True, group="op")
    async def action_remove(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        ok = await self.push_screen_wait(ConfirmScreen(
            f"Remove {entry.name}?\n\nIts unit is stopped and its files deleted. "
            f"Its config in {paths.package_config_dir(entry.kind, entry.name)} is kept.",
            "Remove"))
        if not ok or not self._start():
            return
        assert self.manager is not None
        try:
            await self.manager.remove(entry.name)
        except PackageError as exc:
            self.log_line(f"remove failed: {exc}", error=True)
        finally:
            self._finish()

    @work(exclusive=True, group="op")
    async def action_toggle_enabled(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        if self.ctl is None:
            self.notify("pi-panel-ctl is not running", severity="error")
            return
        enabled = not (self.ctl.get(entry.name) or {}).get("enabled", False)
        assert self.manager is not None
        if await self.manager.set_enabled(entry.name, enabled):
            self.log_line(f"{'enabled' if enabled else 'disabled'} {entry.name}")
        self.action_refresh()

    def action_info(self) -> None:
        entry = self._selected()
        if entry is None:
            return
        src = entry.source
        self.log_line(
            f"[b]{entry.name}[/b] {entry.kind} {entry.version or ''}\n"
            f"  {entry.description or ''}\n"
            f"  path    {entry.path}\n"
            f"  config  {paths.package_config_dir(entry.kind, entry.name)}\n"
            f"  source  {src.type} {src.url}{'#' + src.ref if src.ref else ''}\n"
            f"  rev     {src.commit or src.sha256 or '-'}\n"
            f"  unit    pi-panel-{entry.kind}@{entry.name}.service"
        )

    @work(exclusive=True, group="op")
    async def action_new(self) -> None:
        form = await self.push_screen_wait(FormScreen("New package from the template", [
            Field("kind", "Kind", "app or plugin", "app"),
            Field("name", "Name", "lowercase, e.g. weather"),
            Field("dir", "Parent directory", "", str(Path.cwd())),
        ], submit_label="Create"))
        if not form:
            return
        if form["kind"] not in ("app", "plugin"):
            self.log_line("kind must be app or plugin", error=True)
            return
        try:
            target = scaffold.create(form["kind"], form["name"], Path(form["dir"]).expanduser())
        except (ValueError, FileExistsError, OSError) as exc:
            self.log_line(str(exc), error=True)
            return
        self.log_line(f"created {target} — install it with i and that path")

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_info()
