"""ctld end to end: fake compositor + fake systemd, real Varlink in between."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

import pytest

from pi_panel_varlink import Broadcaster, Client, Interface, Server, VarlinkError, load_interface

from pi_panel_ctl.config import CtlConfig
from pi_panel_ctl.daemon import Ctl
from pi_panel_ctl.units import FakeUnits

from .fake_compositor import FakeCompositor

CTL = "io.pipanel.Ctl"


async def until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def write_registry(path: Path, packages: dict[str, dict[str, Any]]) -> None:
    path.write_text(json.dumps({"version": 1, "packages": {
        name: {"name": name, **entry} for name, entry in packages.items()}}))


@pytest.fixture
def workdir(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="pc", dir="/tmp"))
    monkeypatch.setenv("PI_PANEL_RUNTIME_DIR", str(d / "run"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Panel:
    """ctld wired to a fake compositor and fake units, serving its socket."""

    def __init__(self, workdir: Path, packages: dict[str, dict[str, Any]], enabled: list[str],
                 **config: Any) -> None:
        self.dir = workdir
        self.registry = workdir / "packages.json"
        self.config_path = workdir / "ctl.toml"
        write_registry(self.registry, packages)
        CtlConfig(enabled=enabled, **config).save(self.config_path)
        self.comp_path = str(workdir / "comp.varlink")
        self.ctl_path = str(workdir / "ctl.varlink")
        self.compositor = FakeCompositor(self.comp_path)
        self.units = FakeUnits()
        self.ctl: Ctl | None = None
        self.runner: asyncio.Task | None = None

    async def __aenter__(self) -> "Panel":
        await self.compositor.start()
        self.ctl = Ctl(config_path=self.config_path, registry_path=self.registry,
                       units=self.units, compositor_address=self.comp_path)
        await self.ctl.serve(self.ctl_path)
        self.runner = asyncio.create_task(self.ctl.run())
        await until(lambda: self.ctl.compositor.online)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.runner
        await self.ctl.close()
        await self.compositor.stop()

    @property
    def client(self) -> Client:
        return Client(self.ctl_path)

    async def call(self, method: str, params: dict | None = None) -> dict:
        return await self.client.call(f"{CTL}.{method}", params)


APPS = {
    "photos": {"kind": "app", "version": "1.0"},
    "clock": {"kind": "app"},
    "cam": {"kind": "app"},
    "mqtt": {"kind": "plugin"},
}


async def test_startup_starts_enabled_units_and_registers_app_slots(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "clock", "mqtt"]) as p:
        await until(lambda: set(p.compositor.slots) == {"photos", "clock"})
        assert p.units.state("pi-panel-app@photos.service") == "active"
        assert p.units.state("pi-panel-plugin@mqtt.service") == "active"
        assert p.units.state("pi-panel-app@cam.service") != "active"
        assert p.compositor.slots["photos"]["match_unit"] == "pi-panel-app@photos.service"


async def test_rotation_shows_first_mapped_app(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "clock"]) as p:
        await until(lambda: "clock" in p.compositor.slots)
        p.compositor.map("clock")
        p.compositor.map("photos")
        # implicit default rotation: enabled apps in order -> photos first
        await until(lambda: p.compositor.active == "photos")
        state = (await p.call("GetState"))["state"]
        assert state["showing"] == "photos" and state["reason"] == "rotation"


async def test_timed_show_then_back(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "cam"]) as p:
        await until(lambda: "cam" in p.compositor.slots)
        p.compositor.map("photos")
        p.compositor.map("cam")
        await until(lambda: p.compositor.active == "photos")
        token = (await p.call("Show", {"app": "cam", "seconds": 0.3, "priority": 5}))["token"]
        await until(lambda: p.compositor.active == "cam")
        state = (await p.call("GetState"))["state"]
        assert state["reason"] == "hold" and state["holds"][0]["token"] == token
        await until(lambda: p.compositor.active == "photos")


async def test_hold_waits_for_window(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "cam"]) as p:
        await until(lambda: "cam" in p.compositor.slots)
        p.compositor.map("photos")
        await until(lambda: p.compositor.active == "photos")
        await p.call("Show", {"app": "cam"})
        await asyncio.sleep(0.1)
        assert p.compositor.active == "photos"     # not mapped yet: no switch attempted
        p.compositor.map("cam")
        await until(lambda: p.compositor.active == "cam")


async def test_errors(workdir):
    async with Panel(workdir, APPS, enabled=["photos"]) as p:
        with pytest.raises(VarlinkError) as exc:
            await p.call("Show", {"app": "nope"})
        assert exc.value.error == f"{CTL}.NoSuchApp"
        with pytest.raises(VarlinkError) as exc:
            await p.call("Release", {"token": "zzz"})
        assert exc.value.error == f"{CTL}.NoSuchHold"
        with pytest.raises(VarlinkError) as exc:
            await p.call("SetRotation", {"rotation": {"name": "r", "entries": [
                {"app": "ghost", "seconds": 5}]}})
        assert exc.value.error == f"{CTL}.InvalidRotation"
        with pytest.raises(VarlinkError) as exc:
            await p.call("SetSchedule", {"schedule": {
                "name": "x", "days": "daily", "start": "07:00", "end": "08:00",
                "action": "show", "app": "ghost", "priority": 0}})
        assert exc.value.error == f"{CTL}.InvalidSchedule"
        with pytest.raises(VarlinkError) as exc:
            await p.call("Show", {"app": "photos", "seconds": -1})
        assert exc.value.error == "org.varlink.service.InvalidParameter"


async def test_rotation_and_schedule_config_is_persisted(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "clock"]) as p:
        await p.call("SetRotation", {"rotation": {"name": "evening", "entries": [
            {"app": "clock", "seconds": 30}]}})
        await p.call("SelectRotation", {"name": "evening"})
        await p.call("SetSchedule", {"schedule": {
            "name": "night", "days": "daily", "start": "23:00", "end": "06:30",
            "action": "power_off", "priority": 10}})
        rots = await p.call("GetRotations")
        assert rots["selected"] == "evening"
        assert {r["name"] for r in rots["rotations"]} == {"evening", "default"}
    saved = CtlConfig.load(workdir / "ctl.toml")
    assert saved.selected_rotation == "evening"
    assert "evening" in saved.rotations and "default" not in saved.rotations  # implicit one not saved
    assert [s.name for s in saved.schedules] == ["night"]


async def test_enable_and_disable_package(workdir):
    async with Panel(workdir, APPS, enabled=["photos"]) as p:
        await p.call("EnablePackage", {"name": "cam", "enabled": True})
        await until(lambda: "cam" in p.compositor.slots)
        assert p.units.state("pi-panel-app@cam.service") == "active"
        await p.call("EnablePackage", {"name": "cam", "enabled": False})
        await until(lambda: "cam" not in p.compositor.slots)
        assert p.units.state("pi-panel-app@cam.service") == "inactive"
    assert CtlConfig.load(workdir / "ctl.toml").enabled == ["photos"]


async def test_restart_package_reports_unit_failure(workdir):
    async with Panel(workdir, APPS, enabled=["photos"]) as p:
        await p.call("RestartPackage", {"name": "photos"})
        assert ("restart", "pi-panel-app@photos.service") in p.units.jobs
        p.units.fail.add("pi-panel-app@clock.service")
        with pytest.raises(VarlinkError) as exc:
            await p.call("RestartPackage", {"name": "clock"})
        assert exc.value.error == f"{CTL}.UnitFailed"


async def test_rescan_picks_up_new_packages(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "new"]) as p:
        assert "new" not in p.compositor.slots
        write_registry(p.registry, {**APPS, "new": {"kind": "app"}})
        apps = (await p.call("Rescan"))["apps"]
        assert "new" in {a["name"] for a in apps}
        await until(lambda: "new" in p.compositor.slots)


async def test_compositor_restart_reregisters_slots(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "clock"]) as p:
        await until(lambda: set(p.compositor.slots) == {"photos", "clock"})
        await p.compositor.stop()
        await until(lambda: not p.ctl.compositor.online)
        assert (await p.call("GetState"))["state"]["compositor_online"] is False

        p.compositor = FakeCompositor(p.comp_path)   # a fresh compositor knows nothing
        await p.compositor.start()
        await until(lambda: set(p.compositor.slots) == {"photos", "clock"}, timeout=5)


async def test_subscribe_streams_state_changes(workdir):
    async with Panel(workdir, APPS, enabled=["photos", "clock"]) as p:
        stream = p.client.call_more(f"{CTL}.Subscribe")
        snap = (await anext(stream))["event"]
        assert snap["kind"] == "snapshot" and {a["name"] for a in snap["apps"]} == set(APPS)
        await until(lambda: "photos" in p.compositor.slots)
        p.compositor.map("photos")
        for _ in range(20):
            event = (await asyncio.wait_for(anext(stream), 3))["event"]
            if event["state"]["showing"] == "photos":
                break
        else:
            pytest.fail("never saw photos on screen")
        await stream.aclose()


async def test_app_visibility_is_pushed_to_app_socket(workdir):
    """An app with `varlink = true` hears SetVisible as it is shown/hidden."""
    packages = {**APPS, "photos": {"kind": "app", "varlink": True}}
    seen: list[bool] = []
    iface = Interface(load_interface("io.pipanel.App"))

    async def set_visible(call):
        seen.append(call.param("visible", bool))

    async def list_actions(call):
        return {"actions": [{"name": "next", "label": "Next photo"}]}

    iface.add_method("SetVisible", set_visible)
    iface.add_method("ListActions", list_actions)
    app_server = Server(product="photos", version="1")
    app_server.add_interface(iface)
    sock = Path(os.environ["PI_PANEL_RUNTIME_DIR"]) / "apps" / "photos" / "socket"
    sock.parent.mkdir(parents=True)
    await app_server.start(str(sock))
    try:
        async with Panel(workdir, packages, enabled=["photos", "clock"]) as p:
            await until(lambda: "clock" in p.compositor.slots)
            p.compositor.map("photos")
            p.compositor.map("clock")
            await until(lambda: seen[-1:] == [True])
            await p.call("Show", {"app": "clock"})
            await until(lambda: seen[-1:] == [False])
            await until(lambda: any(a["name"] == "photos" and a["interfaces"] == ["io.pipanel.App"]
                                    for a in p.ctl.apps_list()))
            photos = next(a for a in (await p.call("ListApps"))["apps"] if a["name"] == "photos")
            assert photos["socket"] == str(sock)
    finally:
        await app_server.close()


async def test_power_follows_schedule(workdir):
    """A power_off rule that is active all day blanks the display."""
    async with Panel(workdir, APPS, enabled=["photos"]) as p:
        await p.call("SetSchedule", {"schedule": {
            "name": "always", "days": "daily", "start": "00:00", "end": "23:59",
            "action": "power_off", "priority": 1}})
        await until(lambda: p.compositor.power is False)
        token = (await p.call("Show", {"app": "photos", "priority": 5}))["token"]
        await until(lambda: p.compositor.power is True)
        await p.call("Release", {"token": token})
        await until(lambda: p.compositor.power is False)


def test_interface_files_parse_with_varlinkctl(tmp_path):
    """The .varlink files are served verbatim, so they must be valid IDL."""
    varlinkctl = shutil.which("varlinkctl")
    if not varlinkctl:
        pytest.skip("varlinkctl not installed")
    import subprocess
    from importlib.resources import files

    for text in (files("pi_panel_ctl.interfaces").joinpath("io.pipanel.Ctl.varlink").read_text(),
                 load_interface("io.pipanel.App")):
        f = tmp_path / "x.varlink"
        f.write_text(text)
        result = subprocess.run([varlinkctl, "validate-idl", str(f)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
