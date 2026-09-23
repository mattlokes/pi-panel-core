"""pi-panel-ctl tui, headless, against a live in-process ctld."""

from __future__ import annotations

from textual.widgets import DataTable, Input, Static

from pi_panel_ctl.tui import CtlApp
from pi_panel_tui_kit import ConfirmScreen

from .test_daemon import APPS, Panel, until, workdir  # noqa: F401  (fixture)


async def test_live_table_and_status(workdir):  # noqa: F811
    async with Panel(workdir, APPS, enabled=["photos", "clock", "mqtt"]) as p:
        await until(lambda: "photos" in p.compositor.slots)
        p.compositor.map("photos")
        await until(lambda: p.compositor.active == "photos")
        app = CtlApp(p.ctl_path)
        async with app.run_test() as pilot:
            await until(lambda: app.online and app.state.get("showing") == "photos")
            await pilot.pause()
            table = app.query_one("#apps", DataTable)
            assert table.row_count == len(APPS)
            status = str(app.query_one("#statusbar", Static).render())
            assert "photos" in status and "rotation" in status


async def test_show_next_pause_and_disable(workdir):  # noqa: F811
    async with Panel(workdir, APPS, enabled=["photos", "clock", "cam"]) as p:
        await until(lambda: "cam" in p.compositor.slots)
        for name in ("photos", "clock", "cam"):
            p.compositor.map(name)
        app = CtlApp(p.ctl_path)
        async with app.run_test() as pilot:
            await until(lambda: app.online and len(app.apps) == len(APPS))
            await pilot.pause()
            # rows are sorted (kind, name): cam, clock, photos, mqtt
            await pilot.press("s")                       # pin "cam"
            await until(lambda: p.compositor.active == "cam")
            await pilot.press("space")
            await until(lambda: p.ctl.engine.paused)
            await pilot.press("space")
            await until(lambda: not p.ctl.engine.paused)

            await pilot.press("t")                       # timed show of cam
            await pilot.pause()
            app.screen.query_one("#field-seconds", Input).value = "0.3"
            app.screen.query_one("#field-priority", Input).value = "7"
            await pilot.press("enter")
            await until(lambda: any(h.priority == 7 for h in p.ctl.engine.holds.values()))

            await pilot.press("e")                       # disable cam: asks first
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.click("#confirm")
            await until(lambda: "cam" not in p.ctl.config.enabled)
