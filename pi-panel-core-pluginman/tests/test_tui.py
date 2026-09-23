"""pi-panel-pkg tui, headless via Textual's pilot, against a fake manager."""

from __future__ import annotations

import pytest
from textual.widgets import DataTable, Input, RichLog, Static

from pi_panel_pluginman.manifest import Manifest
from pi_panel_pluginman.registry import Entry, Source
from pi_panel_pluginman.tui import PluginManagerApp
from pi_panel_tui_kit import ConfirmScreen


def entry(name: str, kind: str = "app") -> Entry:
    return Entry(name=name, kind=kind, path=f"/p/{name}", version="1.0",
                 source=Source("git", f"https://example.com/{name}.git", commit="a" * 40))


class FakeManager:
    def __init__(self, output, ctl_online: bool = True) -> None:
        self.output = output
        self.entries = {"photos": entry("photos"), "mqtt": entry("mqtt", "plugin")}
        self.ctl_online = ctl_online
        self.enabled = {"photos"}
        self.calls: list[tuple] = []

    def list(self):
        return sorted(self.entries.values(), key=lambda e: (e.kind, e.name))

    async def ctl_apps(self):
        if not self.ctl_online:
            return None
        return {n: {"enabled": n in self.enabled, "unit_state": "active" if n in self.enabled
                    else "inactive"} for n in self.entries}

    async def install(self, source, confirm=None):
        manifest = Manifest(name="weather", kind="app", exec="true", version="0.1")
        self.output("cloning…")
        self.output("building…")
        if confirm and not await confirm(manifest):
            from pi_panel_pluginman.manager import PackageError
            raise PackageError("cancelled")
        self.calls.append(("install", source))
        self.entries["weather"] = entry("weather")
        return self.entries["weather"]

    async def remove(self, name, purge=False):
        self.calls.append(("remove", name))
        del self.entries[name]

    async def update(self, name):
        self.calls.append(("update", name))
        return self.entries[name]

    async def set_enabled(self, name, enabled):
        self.calls.append(("enable", name, enabled))
        (self.enabled.add if enabled else self.enabled.discard)(name)
        return True


def make_app(**kw) -> tuple[PluginManagerApp, list[FakeManager]]:
    made: list[FakeManager] = []

    def factory(output):
        m = FakeManager(output, **kw)
        made.append(m)
        return m

    return PluginManagerApp(factory), made


def log_text(app) -> str:
    return "\n".join(str(line.text) for line in app.query_one("#log", RichLog).lines)


async def test_lists_packages_with_ctl_state():
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#packages", DataTable)
        assert table.row_count == 2
        rows = [table.get_row_at(i) for i in range(2)]
        assert rows[0][0] == "photos" and rows[0][4] == "yes" and rows[0][5] == "active"
        assert rows[1][0] == "mqtt" and rows[1][4] == "no"


async def test_ctl_offline_is_shown():
    app, _ = make_app(ctl_online=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#statusbar", Static).render())
        assert "ctl offline" in status


async def test_install_flow_streams_log_and_asks_first():
    app, made = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one("#field-source", Input).value = "https://example.com/weather.git"
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert "weather" in app.screen.question
        await pilot.click("#confirm")
        await pilot.pause(0.2)
        assert ("install", "https://example.com/weather.git") in made[0].calls
        assert "building…" in log_text(app)
        assert app.query_one("#packages", DataTable).row_count == 3


async def test_install_cancelled_at_confirm():
    app, made = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
        app.screen.query_one("#field-source", Input).value = "x"
        await pilot.press("enter")
        await pilot.pause()
        await pilot.click("#cancel")
        await pilot.pause(0.2)
        assert not any(c[0] == "install" for c in made[0].calls)
        assert "cancelled" in log_text(app)


async def test_remove_needs_confirmation():
    app, made = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")            # cursor on photos
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert made[0].calls == []
        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#confirm")
        await pilot.pause(0.2)
        assert ("remove", "photos") in made[0].calls


async def test_toggle_enable():
    app, made = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down", "e")     # mqtt: disabled -> enabled
        await pilot.pause(0.2)
        assert ("enable", "mqtt", True) in made[0].calls


@pytest.mark.parametrize("key", ["u"])
async def test_update_selected(key):
    app, made = make_app()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(key)
        await pilot.pause(0.2)
        assert ("update", "photos") in made[0].calls
