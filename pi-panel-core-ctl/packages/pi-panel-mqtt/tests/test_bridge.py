"""The bridge against a fake io.pipanel.Ctl (real Varlink) and a recording
MQTT publisher: no broker, no Pi."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from pi_panel_varlink import Broadcaster, Call, Interface, Server, VarlinkError, load_interface

from pi_panel_mqtt.bridge import Bridge
from pi_panel_mqtt.config import MqttSettings, Settings

CTL_DESCRIPTION = """\
interface io.pipanel.Ctl
method GetState() -> (state: object)
method ListApps() -> (apps: []object)
method GetRotations() -> (rotations: []object, selected: string)
method Show(app: string, seconds: ?float, priority: ?int) -> (token: string)
method Release(token: string) -> ()
method Next() -> ()
method Previous() -> ()
method SwitchTo(app: string) -> ()
method PauseRotation() -> ()
method ResumeRotation() -> ()
method SelectRotation(name: string) -> ()
method EnablePackage(name: string, enabled: bool) -> ()
method RestartPackage(name: string) -> ()
method SetClock(enabled: ?bool, format: ?string, position: ?string, size: ?int) -> (clock: object)
method Subscribe() -> (event: object)
error NoSuchApp (app: string)
"""


def state(**kw: Any) -> dict[str, Any]:
    return {"showing": "photos", "reason": "rotation", "output_power": True, "rotation": "default",
            "rotation_paused": False, "holds": [], "active_schedules": [],
            "compositor_online": True,
            "clock": {"enabled": False, "format": "%H:%M", "position": "bottom_right", "size": 0}} | kw


def app(name: str, kind: str = "app", **kw: Any) -> dict[str, Any]:
    return {"name": name, "kind": kind, "version": "1", "description": None, "enabled": True,
            "unit": f"pi-panel-{kind}@{name}.service", "unit_state": "active", "mapped": True,
            "visible": False, "socket": None, "interfaces": []} | kw


class FakeCtl:
    def __init__(self, path: str, apps: list[dict[str, Any]]) -> None:
        self.path = path
        self.apps = apps
        self.state = state()
        self.calls: list[tuple[str, dict]] = []
        self.events = Broadcaster()
        self.server: Server | None = None

    async def start(self) -> None:
        iface = Interface(CTL_DESCRIPTION)
        for name in ("Show", "Release", "Next", "Previous", "SwitchTo", "PauseRotation",
                     "ResumeRotation", "SelectRotation", "EnablePackage", "RestartPackage", "SetClock"):
            iface.add_method(name, self._recorder(name))
        iface.add_method("GetState", self._get_state)
        iface.add_method("ListApps", self._list_apps)
        iface.add_method("GetRotations", self._rotations)
        iface.add_method("Subscribe", self._subscribe)
        self.server = Server(product="fake-ctl", version="0")
        self.server.add_interface(iface)
        await self.server.start(self.path)

    async def stop(self) -> None:
        await self.server.close()

    def _recorder(self, name: str):
        async def handler(call: Call):
            self.calls.append((name, dict(call.parameters)))
            if name == "Show":
                if call.param("app", str) not in {a["name"] for a in self.apps}:
                    raise VarlinkError("io.pipanel.Ctl.NoSuchApp", {"app": call.parameters["app"]})
                return {"token": "tok1"}
            return None
        return handler

    async def _get_state(self, call):
        return {"state": self.state}

    async def _list_apps(self, call):
        return {"apps": self.apps}

    async def _rotations(self, call):
        return {"rotations": [{"name": "default", "entries": []}, {"name": "night", "entries": []}],
                "selected": "default"}

    async def _subscribe(self, call):
        call.require_more()
        with self.events.subscribe() as sub:
            await call.reply({"event": {"kind": "snapshot", "state": self.state, "apps": self.apps}})
            async for event in sub:
                await call.reply({"event": event})

    def emit(self, kind: str, **extra: Any) -> None:
        self.events.publish({"kind": kind, "state": self.state, **extra})


class RecordingClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, bool]] = []

    async def publish(self, topic, payload=None, qos=0, retain=False):
        if isinstance(payload, str):
            payload = payload.encode()
        self.published.append((topic, payload, retain))

    def last(self, topic: str) -> bytes | None:
        for t, p, _ in reversed(self.published):
            if t == topic:
                return p
        return None

    def topics(self) -> list[str]:
        return [t for t, _, _ in self.published]


async def until(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture
async def rig():
    d = Path(tempfile.mkdtemp(prefix="pq", dir="/tmp"))
    # An app with its own io.pipanel.App socket and two actions.
    app_sock = str(d / "photos.sock")
    invoked: list[str] = []
    iface = Interface(load_interface("io.pipanel.App"))

    async def list_actions(call):
        return {"actions": [{"name": "next", "label": "Next photo"},
                            {"name": "pause", "label": "Pause"}]}

    async def invoke(call):
        invoked.append(call.param("name", str))

    async def set_visible(call):
        return None

    iface.add_method("ListActions", list_actions)
    iface.add_method("InvokeAction", invoke)
    iface.add_method("SetVisible", set_visible)
    app_server = Server(product="photos", version="1")
    app_server.add_interface(iface)
    await app_server.start(app_sock)

    ctl = FakeCtl(str(d / "ctl"), [
        app("photos", socket=app_sock, interfaces=["io.pipanel.App"]),
        app("clock"),
        app("mqtt", kind="plugin"),
    ])
    await ctl.start()
    bridge = Bridge(Settings(mqtt=MqttSettings(device_id="jazz")), ctl_address=ctl.path)
    client = RecordingClient()
    bridge.client = client
    task = asyncio.create_task(bridge.ctl_loop())
    await until(lambda: client.last("pi-panel/jazz/state") is not None)
    yield bridge, ctl, client, invoked
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await ctl.stop()
    await app_server.close()
    shutil.rmtree(d, ignore_errors=True)


def discovery_config(client: RecordingClient, suffix: str) -> dict | None:
    for t, p, _ in reversed(client.published):
        if t.endswith(suffix) and t.startswith("homeassistant/"):
            return json.loads(p) if p else None
    return None


async def test_snapshot_publishes_retained_state_and_discovery(rig):
    bridge, ctl, client, _ = rig
    assert client.last("pi-panel/jazz/ctl/availability") == b"online"
    assert client.last("pi-panel/jazz/compositor/availability") == b"online"
    assert json.loads(client.last("pi-panel/jazz/state"))["showing"] == "photos"
    assert json.loads(client.last("pi-panel/jazz/rotations")) == {
        "selected": "default", "rotations": ["default", "night"]}
    assert json.loads(client.last("pi-panel/jazz/app/photos/actions"))[0]["name"] == "next"
    assert all(retain for _, _, retain in client.published)

    select = discovery_config(client, "/showing/config")
    assert select["options"] == ["clock", "photos"]           # apps only, not the plugin
    assert select["command_topic"] == "pi-panel/jazz/command/switch"
    assert select["device"]["identifiers"] == ["pi_panel_jazz"]
    assert "via_device" not in select["device"]
    # Each package is a device of its own, connected via the panel.
    button = discovery_config(client, "/photos_action_next/config")
    assert button["name"] == "Next photo"           # HA puts the device name in front
    assert button["command_topic"] == "pi-panel/jazz/command/app/photos/action/next"
    assert button["unique_id"] == "pi_panel_jazz_photos_action_next"     # unchanged by the move
    assert button["device"] == {
        "identifiers": ["pi_panel_jazz_photos"], "name": "Pi Panel (jazz) - Photos",
        "manufacturer": "pi-panel", "model": "pi-panel app", "via_device": "pi_panel_jazz",
        "sw_version": "1"}
    restart = discovery_config(client, "/mqtt_restart/config")
    assert restart["name"] == "Restart"
    assert (restart["device"]["name"], restart["device"]["model"]) == (
        "Pi Panel (jazz) - Mqtt", "pi-panel plugin")


async def test_only_changes_are_republished(rig):
    bridge, ctl, client, _ = rig
    before = len(client.published)
    await bridge.sync()
    assert len(client.published) == before
    ctl.state = state(showing="clock")
    ctl.emit("state_changed")
    await until(lambda: json.loads(client.last("pi-panel/jazz/state"))["showing"] == "clock")
    new = client.topics()[before:]
    assert new == ["pi-panel/jazz/state"]


async def test_removed_app_removes_its_entities(rig):
    bridge, ctl, client, _ = rig
    ctl.apps = [a for a in ctl.apps if a["name"] != "clock"]
    ctl.emit("apps_changed", apps=ctl.apps)
    await until(lambda: discovery_config(client, "/clock_restart/config") is None)
    # an empty retained payload is how HA deletes an entity
    assert ("homeassistant/button/pi_panel_jazz/clock_restart/config", b"", True) in client.published


async def test_ctl_going_away_marks_it_offline(rig):
    bridge, ctl, client, _ = rig
    await ctl.stop()
    await until(lambda: client.last("pi-panel/jazz/ctl/availability") == b"offline")
    assert client.last("pi-panel/jazz/compositor/availability") == b"offline"
    await ctl.start()
    await until(lambda: client.last("pi-panel/jazz/ctl/availability") == b"online", timeout=5)


@pytest.mark.parametrize("path,payload,expected", [
    ("show", "clock", ("Show", {"app": "clock"})),
    ("show", '{"app": "clock", "seconds": 30, "priority": 5}',
     ("Show", {"app": "clock", "seconds": 30, "priority": 5})),
    ("switch", "clock", ("SwitchTo", {"app": "clock"})),
    ("release", "tok1", ("Release", {"token": "tok1"})),
    ("next", "", ("Next", {})),
    ("previous", "", ("Previous", {})),
    ("rotation/paused", "ON", ("PauseRotation", {})),
    ("rotation/paused", "OFF", ("ResumeRotation", {})),
    ("rotation/select", "night", ("SelectRotation", {"name": "night"})),
    ("app/clock/restart", "", ("RestartPackage", {"name": "clock"})),
    ("app/clock/enabled", "OFF", ("EnablePackage", {"name": "clock", "enabled": False})),
    ("clock/enabled", "ON", ("SetClock", {"enabled": True})),
    ("clock/format", "%a %H:%M", ("SetClock", {"format": "%a %H:%M"})),
    ("clock/position", "top_left", ("SetClock", {"position": "top_left"})),
    ("clock/size", "48.0", ("SetClock", {"size": 48})),
    ("clock", '{"enabled": true, "size": 0}', ("SetClock", {"enabled": True, "size": 0})),
])
async def test_commands_map_to_ctl_calls(rig, path, payload, expected):
    bridge, ctl, client, _ = rig
    await bridge.handle(f"pi-panel/jazz/command/{path}", payload.encode())
    assert ctl.calls[-1] == expected
    result = json.loads(client.last("pi-panel/jazz/command/result"))
    assert result["ok"] is True and result["command"] == path
    assert client.published[-1][2] is False, "results must not be retained"


async def test_app_action_goes_to_the_app_socket(rig):
    bridge, ctl, client, invoked = rig
    await bridge.handle("pi-panel/jazz/command/app/photos/action/next", b"")
    assert invoked == ["next"]


async def test_errors_are_reported_on_result(rig):
    bridge, ctl, client, _ = rig
    await bridge.handle("pi-panel/jazz/command/show", b"ghost")
    result = json.loads(client.last("pi-panel/jazz/command/result"))
    assert result["ok"] is False
    assert result["detail"]["error"] == "io.pipanel.Ctl.NoSuchApp"
    await bridge.handle("pi-panel/jazz/command/rotation/paused", b"maybe")
    assert json.loads(client.last("pi-panel/jazz/command/result"))["detail"]["error"] == "invalid"
    await bridge.handle("pi-panel/jazz/command/frobnicate", b"")
    assert json.loads(client.last("pi-panel/jazz/command/result"))["ok"] is False
    await bridge.handle("pi-panel/jazz/command/app/clock/action/next", b"")  # clock has no socket
    assert json.loads(client.last("pi-panel/jazz/command/result"))["ok"] is False


async def test_own_result_messages_are_ignored(rig):
    bridge, ctl, client, _ = rig
    before = len(client.published)
    await bridge.handle("pi-panel/jazz/command/result", b'{"ok": true}')
    assert len(client.published) == before


async def test_visibility_only_changes_do_not_requery_actions(rig):
    bridge, ctl, client, _ = rig
    calls = []
    original = bridge._refresh_actions

    async def counting():
        calls.append(1)
        await original()

    bridge._refresh_actions = counting
    ctl.apps = [dict(a, visible=(a["name"] == "clock")) for a in ctl.apps]
    ctl.emit("apps_changed", apps=ctl.apps)
    await until(lambda: json.loads(client.last("pi-panel/jazz/apps"))[1]["visible"] is True)
    assert calls == []
    ctl.apps = [dict(a, unit_state="inactive") if a["name"] == "photos" else a for a in ctl.apps]
    ctl.emit("apps_changed", apps=ctl.apps)
    await until(lambda: calls == [1])


async def test_clock_entities(rig):
    bridge, ctl, client, _ = rig
    switch = discovery_config(client, "/clock_overlay/config")
    assert switch["command_topic"] == "pi-panel/jazz/command/clock/enabled"
    assert "value_json.clock.enabled" in switch["value_template"]
    assert discovery_config(client, "/clock_overlay_position/config")["options"][0] == "top_left"
    size = discovery_config(client, "/clock_overlay_size/config")
    assert (size["min"], size["max"], size["entity_category"]) == (0, 1000, "config")
    assert discovery_config(client, "/clock_overlay_format/config")["max"] == 63
    # Display entities need the compositor; the clock's settings live in ctl.
    assert len(switch["availability"]) == 2


async def test_no_clock_entities_from_a_ctl_without_one(rig):
    bridge, ctl, client, _ = rig
    ctl.state = {k: v for k, v in state().items() if k != "clock"}
    ctl.emit("state_changed")
    await until(lambda: discovery_config(client, "/clock_overlay/config") is None)


async def test_bad_clock_commands(rig):
    bridge, ctl, client, _ = rig
    before = len(ctl.calls)
    for path, payload in (("clock/size", b"big"), ("clock/size", b"4.5"), ("clock/enabled", b"maybe"),
                          ("clock", b"on"), ("clock", b'{"colour": "red"}'), ("clock/frob", b"1")):
        await bridge.handle(f"pi-panel/jazz/command/{path}", payload)
        result = json.loads(client.last("pi-panel/jazz/command/result"))
        assert result["ok"] is False and result["detail"]["error"] == "invalid", (path, payload)
    assert len(ctl.calls) == before


def test_legacy_topics_are_this_devices_old_ones():
    bridge = Bridge(Settings(mqtt=MqttSettings(device_id="jazz")), ctl_address="/nonexistent")
    topics = bridge.legacy_topics()
    assert "pi-panel/jazz/views" in topics
    assert "homeassistant/select/pi_panel_jazz/active_view/config" in topics
    assert all("jazz" in t for t in topics)
