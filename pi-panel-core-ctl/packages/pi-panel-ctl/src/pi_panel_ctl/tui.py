"""pi-panel-ctl tui: the panel, live, over io.pipanel.Ctl.

Same layout and conventions as `pi-panel-pkg tui` (both from
pi-panel-tui-kit). Everything on screen comes from one Subscribe, so it
reflects changes made anywhere (MQTT, schedules, the CLI) as they happen.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from textual import on, work
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import DataTable

from pi_panel_tui_kit import ConfirmScreen, Field, FormScreen, PiPanelApp, dot
from pi_panel_varlink import Client, ProtocolError, VarlinkError, VarlinkTimeout, VarlinkUnavailable

from .model import CLOCK_POSITIONS

CTL = "io.pipanel.Ctl"
_UNREACHABLE = (VarlinkUnavailable, VarlinkTimeout, ProtocolError, OSError)


class CtlApp(PiPanelApp):
    TITLE = "pi-panel"
    BINDINGS = [
        *PiPanelApp.BINDINGS,
        Binding("s", "show", "Show"),
        Binding("t", "show_timed", "Show for…"),
        Binding("x", "release_all", "Release holds"),
        Binding("n", "next", "Next"),
        Binding("p", "previous", "Prev"),
        Binding("space", "toggle_pause", "Pause/resume"),
        Binding("o", "rotation", "Rotation"),
        Binding("e", "toggle_enabled", "Enable/disable"),
        Binding("r", "restart", "Restart"),
        Binding("a", "action", "Action"),
        Binding("c", "clock", "Clock"),
    ]

    def __init__(self, address: str) -> None:
        super().__init__()
        self.address = address
        self.ctl = Client(address, timeout=5.0)
        self.online = False
        self.state: dict[str, Any] = {}
        self.apps: list[dict[str, Any]] = []

    def body(self) -> Iterable[Widget]:
        yield DataTable(id="apps", classes="main-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.query_one("#apps", DataTable).add_columns(
            "", "name", "kind", "enabled", "unit", "window", "version", "description")
        self.sub_title = self.address
        self.subscribe()

    # --- live state --------------------------------------------------------

    @work(exclusive=True, group="subscribe")
    async def subscribe(self) -> None:
        while True:
            try:
                async for reply in self.ctl.call_more(f"{CTL}.Subscribe"):
                    event = reply["event"]
                    if not self.online:
                        self.log_line("[green]connected to pi-panel-ctl[/green]")
                    self.online = True
                    self._apply(event)
            except _UNREACHABLE as exc:
                if self.online:
                    self.log_line(f"lost pi-panel-ctl: {exc}", error=True)
            self.online = False
            self._render_status()
            await asyncio.sleep(2)

    def _apply(self, event: dict[str, Any]) -> None:
        before = self.state
        self.state = event.get("state") or self.state
        if event.get("apps") is not None:
            self.apps = event["apps"]
            self._render_table()
        if (before.get("showing"), before.get("reason")) != (self.state.get("showing"),
                                                             self.state.get("reason")):
            self.log_line(f"showing [b]{self.state.get('showing') or '-'}[/b] "
                          f"({self.state.get('reason')})")
        if before.get("output_power") != self.state.get("output_power") and before:
            self.log_line(f"display {'on' if self.state.get('output_power') else 'off'}")
        self._render_status()
        self._render_table()

    def _render_status(self) -> None:
        s = self.state
        if not self.online or not s:
            self.set_status(f"ctl {dot(False)}   waiting for pi-panel-ctl…")
            return
        holds = len(s.get("holds", []))
        sched = ", ".join(s.get("active_schedules", []))
        self.set_status(
            f"ctl {dot(True)} compositor {dot(s.get('compositor_online', False))}   "
            f"showing [b]{s.get('showing') or '-'}[/b] ({s.get('reason')})   "
            f"display {'on' if s.get('output_power') else '[yellow]off[/yellow]'}   "
            f"rotation {s.get('rotation')}{' [yellow]paused[/yellow]' if s.get('rotation_paused') else ''}"
            + (f"   holds {holds}" if holds else "")
            + (f"   schedule {sched}" if sched else "")
            + ("   clock" if (s.get("clock") or {}).get("enabled") else "")
        )

    def _render_table(self) -> None:
        table = self.query_one("#apps", DataTable)
        row = table.cursor_row
        table.clear()
        showing = self.state.get("showing")
        for a in self.apps:
            table.add_row(
                "▶" if a["kind"] == "app" and a["name"] == showing else " ",
                a["name"], a["kind"], "yes" if a["enabled"] else "no", a["unit_state"],
                ("mapped" if a["mapped"] else "-") if a["kind"] == "app" else "",
                a.get("version") or "", (a.get("description") or "")[:40],
                key=a["name"],
            )
        if row is not None and row < table.row_count:
            table.move_cursor(row=row)

    def _selected(self) -> dict[str, Any] | None:
        table = self.query_one("#apps", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self.apps):
            return None
        return self.apps[table.cursor_row]

    # --- calls -----------------------------------------------------------

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict | None:
        try:
            return await self.ctl.call(f"{CTL}.{method}", params)
        except VarlinkError as exc:
            detail = " ".join(f"{k}={v}" for k, v in exc.parameters.items())
            self.log_line(f"{method}: {exc.error.rsplit('.', 1)[-1]} {detail}", error=True)
        except _UNREACHABLE as exc:
            self.log_line(f"{method}: pi-panel-ctl unreachable ({exc})", error=True)
        return None

    def _selected_app(self) -> dict[str, Any] | None:
        a = self._selected()
        if a is None or a["kind"] != "app":
            self.notify("select an app (not a plugin)", severity="warning")
            return None
        return a

    @work(group="call")
    async def action_show(self) -> None:
        a = self._selected_app()
        if a and await self._call("Show", {"app": a["name"]}) is not None:
            self.log_line(f"pinned {a['name']} (until next/prev or release)")

    @work(group="call")
    async def action_show_timed(self) -> None:
        a = self._selected_app()
        if a is None:
            return
        form = await self.push_screen_wait(FormScreen(f"Show {a['name']}", [
            Field("seconds", "Seconds", "e.g. 30", "30"),
            Field("priority", "Priority", "higher beats schedules and other holds", "0"),
        ], submit_label="Show"))
        if not form:
            return
        try:
            params = {"app": a["name"], "seconds": float(form["seconds"]),
                      "priority": int(form["priority"])}
        except ValueError:
            self.log_line("seconds and priority must be numbers", error=True)
            return
        if await self._call("Show", params) is not None:
            self.log_line(f"showing {a['name']} for {form['seconds']}s at priority {form['priority']}")

    @work(group="call")
    async def action_release_all(self) -> None:
        for hold in list(self.state.get("holds", [])):
            await self._call("Release", {"token": hold["token"]})

    @work(group="call")
    async def action_next(self) -> None:
        await self._call("Next")

    @work(group="call")
    async def action_previous(self) -> None:
        await self._call("Previous")

    @work(group="call")
    async def action_toggle_pause(self) -> None:
        await self._call("ResumeRotation" if self.state.get("rotation_paused") else "PauseRotation")

    @work(group="call")
    async def action_rotation(self) -> None:
        reply = await self._call("GetRotations")
        if reply is None:
            return
        names = [r["name"] for r in reply["rotations"]]
        form = await self.push_screen_wait(FormScreen("Select rotation", [
            Field("name", f"One of: {', '.join(names)}", "", reply["selected"]),
        ], submit_label="Select"))
        if form and await self._call("SelectRotation", {"name": form["name"]}) is not None:
            self.log_line(f"rotation {form['name']}")

    @work(group="call")
    async def action_toggle_enabled(self) -> None:
        a = self._selected()
        if a is None:
            return
        enabled = not a["enabled"]
        if not enabled:
            ok = await self.push_screen_wait(ConfirmScreen(
                f"Disable {a['name']}?\n\nIts unit is stopped until you enable it again.", "Disable"))
            if not ok:
                return
        if await self._call("EnablePackage", {"name": a["name"], "enabled": enabled}) is not None:
            self.log_line(f"{'enabled' if enabled else 'disabled'} {a['name']}")

    @work(group="call")
    async def action_restart(self) -> None:
        a = self._selected()
        if a is None:
            return
        if await self.push_screen_wait(ConfirmScreen(f"Restart {a['name']}?", "Restart")):
            if await self._call("RestartPackage", {"name": a["name"]}) is not None:
                self.log_line(f"restarted {a['name']}")

    @work(group="call")
    async def action_action(self) -> None:
        a = self._selected_app()
        if a is None:
            return
        if not a.get("socket"):
            self.notify(f"{a['name']} serves no Varlink interface", severity="warning")
            return
        client = Client(a["socket"], timeout=5.0)
        try:
            actions = (await client.call("io.pipanel.App.ListActions"))["actions"]
        except (VarlinkError, *_UNREACHABLE) as exc:
            self.log_line(f"{a['name']}: {exc}", error=True)
            return
        if not actions:
            self.notify(f"{a['name']} has no actions")
            return
        listing = ", ".join(f"{x['name']} ({x['label']})" for x in actions)
        form = await self.push_screen_wait(FormScreen(f"{a['name']} action", [
            Field("name", listing, "", actions[0]["name"]),
        ], submit_label="Invoke"))
        if not form:
            return
        try:
            await client.call("io.pipanel.App.InvokeAction", {"name": form["name"]})
            self.log_line(f"{a['name']}: {form['name']}")
        except (VarlinkError, *_UNREACHABLE) as exc:
            self.log_line(f"{a['name']}: {exc}", error=True)

    @work(group="call")
    async def action_clock(self) -> None:
        clock = self.state.get("clock")
        if clock is None:
            self.notify("this pi-panel-ctl has no clock overlay", severity="warning")
            return
        form = await self.push_screen_wait(FormScreen("Clock overlay", [
            Field("enabled", "Shown (yes/no)", "", "yes" if clock["enabled"] else "no"),
            Field("format", "Format, strftime(3)", "e.g. %H:%M or %a %d %b %H:%M", clock["format"]),
            Field("position", ", ".join(CLOCK_POSITIONS), "", clock["position"]),
            Field("size", "Text height in px (0 = automatic)", "", str(clock["size"])),
        ], submit_label="Apply"))
        if not form:
            return
        enabled = form["enabled"].strip().lower()
        if enabled not in ("yes", "no", "on", "off", "true", "false"):
            self.log_line("shown must be yes or no", error=True)
            return
        try:
            size = int(form["size"])
        except ValueError:
            self.log_line("size must be a whole number", error=True)
            return
        params = {"enabled": enabled in ("yes", "on", "true"), "format": form["format"],
                  "position": form["position"].strip(), "size": size}
        if await self._call("SetClock", params) is not None:
            self.log_line(f"clock {'on' if params['enabled'] else 'off'}")

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_show()


async def run_tui(address: str) -> None:
    await CtlApp(address).run_async()
