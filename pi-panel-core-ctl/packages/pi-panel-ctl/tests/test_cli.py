"""The CLI against a live in-process ctld."""

from __future__ import annotations

import pytest

from pi_panel_ctl.cli import build_parser, run

from .test_daemon import APPS, Panel, until, workdir  # noqa: F401  (fixture)


async def cli(panel: Panel, *argv: str) -> int:
    return await run(build_parser().parse_args(["--socket", panel.ctl_path, *argv]))


async def test_status_and_apps(workdir, capsys):  # noqa: F811
    async with Panel(workdir, APPS, enabled=["photos", "mqtt"]) as p:
        await until(lambda: "photos" in p.compositor.slots)
        p.compositor.map("photos")
        await until(lambda: p.compositor.active == "photos")
        assert await cli(p, "status") == 0
        out = capsys.readouterr().out
        assert "showing:    photos  (rotation)" in out
        assert await cli(p, "apps") == 0
        out = capsys.readouterr().out
        assert "photos" in out and "mqtt" in out and "plugin" in out


async def test_show_release_and_rotation_set(workdir, capsys):  # noqa: F811
    async with Panel(workdir, APPS, enabled=["photos", "clock"]) as p:
        assert await cli(p, "show", "clock", "--priority", "3") == 0
        token = capsys.readouterr().out.strip()
        assert p.ctl.engine.holds[token].priority == 3
        assert await cli(p, "release", token) == 0
        assert p.ctl.engine.holds == {}
        assert await cli(p, "rotation", "set", "day", "photos:120", "clock:30") == 0
        assert await cli(p, "rotation", "select", "day") == 0
        assert await cli(p, "rotation", "list") == 0
        out = capsys.readouterr().out
        assert "* day          photos:120  clock:30" in out


async def test_schedule_set_and_list(workdir, capsys):  # noqa: F811
    async with Panel(workdir, APPS, enabled=["photos"]) as p:
        assert await cli(p, "schedule", "set", "night", "--start", "23:00", "--end", "06:30",
                         "--action", "power_off", "--priority", "10") == 0
        assert await cli(p, "schedule", "list") == 0
        assert "night" in capsys.readouterr().out


async def test_clock(workdir, capsys):  # noqa: F811
    async with Panel(workdir, APPS, enabled=["photos"]) as p:
        assert await cli(p, "clock") == 0
        assert capsys.readouterr().out.strip() == "off"
        assert await cli(p, "clock", "on", "--format", "%H:%M:%S", "--position", "center") == 0
        assert capsys.readouterr().out.strip() == "on  '%H:%M:%S' center auto size"
        await until(lambda: p.compositor.clock["position"] == "center")
        assert await cli(p, "status") == 0
        assert "clock:      on" in capsys.readouterr().out
        assert await cli(p, "clock", "off") == 0
        await until(lambda: p.compositor.clock["enabled"] is False)


def test_bad_rotation_entry_is_rejected_by_argparse():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["rotation", "set", "x", "no-seconds"])
