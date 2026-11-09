"""The bridge: io.pipanel.Ctl events in, MQTT state out; MQTT commands in, Ctl calls out.

Two independent loops, because either side can drop out without the other:

    ctl_loop    holds one Subscribe on io.pipanel.Ctl and keeps the latest
                state, apps, rotations and app actions in memory
    mqtt_loop   keeps a broker session, and turns command topics into calls

Both end in `sync()`, which works out every retained topic from memory and
publishes only what changed. After a broker reconnect the cache is cleared and
everything is republished, since retained state may have been lost if the
broker restarted without persistence. Nothing here polls.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Protocol

import aiomqtt
from pi_panel_varlink import Client, ProtocolError, VarlinkError, VarlinkTimeout, VarlinkUnavailable

from .config import Settings
from .discovery import Discovery
from .schema import CLOCK_KEYS, OFF, OFFLINE, ON, ONLINE, Topics, encode, result_payload

log = logging.getLogger("pi-panel-mqtt")

CTL = "io.pipanel.Ctl"
RETRY_MIN = 1.0
RETRY_MAX = 30.0
_UNREACHABLE = (VarlinkUnavailable, VarlinkTimeout, ProtocolError, OSError)


class Publisher(Protocol):
    async def publish(self, topic: str, payload: Any = None, qos: int = 0,
                      retain: bool = False) -> None: ...


class Bridge:
    def __init__(self, settings: Settings, ctl_address: str | None = None) -> None:
        self.settings = settings
        self.topics = Topics(base=settings.mqtt.base_topic,
                             device_id=settings.mqtt.resolved_device_id())
        self.discovery = Discovery(self.topics, settings.homeassistant.discovery_prefix)
        self.ctl = Client(ctl_address or settings.ctl.resolved_socket(), timeout=5.0)

        # What ctl last told us.
        self.ctl_online = False
        self.state: dict[str, Any] | None = None
        self.apps: list[dict[str, Any]] = []
        self.rotations: dict[str, Any] = {"selected": "default", "rotations": []}
        self.actions: dict[str, list[dict[str, str]]] = {}

        # What the broker last got from us.
        self.client: Publisher | None = None
        self._published: dict[str, bytes] = {}
        self._discovery_topics: set[str] = set()
        self._sync_lock = asyncio.Lock()
        self._action_sources: object = None

    # --- publishing ------------------------------------------------------

    def desired(self) -> dict[str, bytes]:
        """Every retained topic, as it should read right now."""
        t = self.topics
        out: dict[str, bytes] = {
            t.ctl_availability: (ONLINE if self.ctl_online else OFFLINE).encode(),
            t.compositor_availability: (
                ONLINE if self.ctl_online and self.state and self.state.get("compositor_online")
                else OFFLINE).encode(),
        }
        if self.state is not None:
            out[t.state] = encode(self.state)
            out[t.apps] = encode(self.apps)
            out[t.rotations] = encode(self.rotations)
            for app in self.apps:
                if app["kind"] == "app":
                    out[t.app_actions(app["name"])] = encode(self.actions.get(app["name"], []))
        if self.settings.homeassistant.enabled and self.state is not None:
            for msg in self.discovery.messages(self.apps, self.rotations["rotations"], self.actions,
                                               clock="clock" in self.state):
                out[msg.topic] = msg.payload
        return out

    async def sync(self) -> None:
        """Publish whatever differs from what the broker has."""
        async with self._sync_lock:
            client = self.client
            if client is None:
                return
            want = self.desired()
            discovery_now = {k for k in want if k.startswith(self.discovery.prefix + "/")}
            # Entities that no longer exist: an empty retained config deletes them.
            for topic in self._discovery_topics - discovery_now:
                want[topic] = b""
            for topic, payload in want.items():
                if self._published.get(topic) != payload:
                    await client.publish(topic, payload, qos=1, retain=True)
                    self._published[topic] = payload
            self._discovery_topics = discovery_now

    # --- ctl side --------------------------------------------------------

    async def _refresh_rotations(self) -> None:
        with contextlib.suppress(*_UNREACHABLE, VarlinkError):
            reply = await self.ctl.call(f"{CTL}.GetRotations")
            self.rotations = {"selected": reply["selected"],
                              "rotations": sorted(r["name"] for r in reply["rotations"])}

    async def _refresh_actions(self) -> None:
        actions: dict[str, list[dict[str, str]]] = {}
        for app in self.apps:
            if app["kind"] != "app" or not app.get("socket") or app.get("unit_state") != "active":
                continue
            if "io.pipanel.App" not in app.get("interfaces", []):
                continue   # not up yet: ctl sends apps_changed once it has probed it
            try:
                reply = await Client(app["socket"], timeout=2.0).call("io.pipanel.App.ListActions")
                actions[app["name"]] = [{"name": a["name"], "label": a["label"]}
                                        for a in reply.get("actions", [])]
            except (*_UNREACHABLE, VarlinkError) as exc:
                log.debug("ListActions on %s: %s", app["name"], exc)
        self.actions = actions

    async def apply(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        self.state = event.get("state") or self.state
        if "apps" in event and event["apps"] is not None:
            self.apps = event["apps"]
        # apps_changed also fires when only `visible` moves (every switch);
        # ask apps for their actions only when something that affects the
        # answer changed.
        sources = sorted((a["name"], a.get("socket"), a.get("unit_state"),
                          tuple(a.get("interfaces", []))) for a in self.apps)
        if kind == "snapshot" or sources != self._action_sources:
            self._action_sources = sources
            await self._refresh_actions()
        if kind in ("snapshot", "config_changed"):
            await self._refresh_rotations()
        await self.sync()

    async def ctl_loop(self) -> None:
        delay = RETRY_MIN
        while True:
            try:
                async for reply in self.ctl.call_more(f"{CTL}.Subscribe"):
                    if not self.ctl_online:
                        log.info("connected to pi-panel-ctl")
                    self.ctl_online = True
                    delay = RETRY_MIN
                    await self.apply(reply["event"])
            except _UNREACHABLE as exc:
                log.debug("ctl: %s", exc)
            except VarlinkError as exc:
                log.error("ctl refused Subscribe: %s", exc)
            if self.ctl_online:
                log.warning("lost pi-panel-ctl; retrying")
                self.ctl_online = False
                await self.sync()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    # --- commands --------------------------------------------------------

    async def _ctl_call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return await self.ctl.call(f"{CTL}.{method}", params)

    async def execute(self, path: str, payload: str) -> Any:
        """Run one command (`path` is the topic after command/). Returns the
        reply; raises VarlinkError / ValueError for the result topic."""
        text = payload.strip()
        parts = path.split("/")
        if path == "show":
            if text.startswith("{"):
                data = json.loads(text)
                if not isinstance(data, dict) or "app" not in data:
                    raise ValueError('show expects an app name or {"app": ...}')
                params = {k: data[k] for k in ("app", "seconds", "priority") if k in data}
            else:
                params = {"app": text}
            return await self._ctl_call("Show", params)
        if path == "switch":
            return await self._ctl_call("SwitchTo", {"app": text})
        if path == "release":
            return await self._ctl_call("Release", {"token": text})
        if path == "next":
            return await self._ctl_call("Next")
        if path == "previous":
            return await self._ctl_call("Previous")
        if path == "rotation/paused":
            return await self._ctl_call("PauseRotation" if _on(text) else "ResumeRotation")
        if path == "rotation/select":
            return await self._ctl_call("SelectRotation", {"name": text})
        if parts[0] == "clock":
            return await self._ctl_call("SetClock", _clock_params(parts[1:], payload=text))
        if len(parts) >= 3 and parts[0] == "app":
            name, verb = parts[1], parts[2]
            if verb == "restart" and len(parts) == 3:
                return await self._ctl_call("RestartPackage", {"name": name})
            if verb == "enabled" and len(parts) == 3:
                return await self._ctl_call("EnablePackage", {"name": name, "enabled": _on(text)})
            if verb == "action" and len(parts) == 4:
                app = next((a for a in self.apps if a["name"] == name), None)
                if app is None or not app.get("socket"):
                    raise ValueError(f"{name} has no Varlink interface")
                return await Client(app["socket"], timeout=5.0).call(
                    "io.pipanel.App.InvokeAction", {"name": parts[3]})
        raise ValueError(f"unknown command {path!r}")

    async def handle(self, topic: str, payload: bytes | str) -> None:
        if not topic.startswith(self.topics.command_prefix):
            return
        path = topic[len(self.topics.command_prefix):]
        if path == "result":
            return   # our own replies come back through the wildcard
        text = payload.decode(errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        log.info("command %s %r", path, text)
        try:
            reply = await self.execute(path, text)
            result = result_payload(path, text, True, reply or None)
        except VarlinkError as exc:
            log.warning("command %s refused: %s", path, exc)
            result = result_payload(path, text, False, {"error": exc.error, "parameters": exc.parameters})
        except (ValueError, json.JSONDecodeError) as exc:
            result = result_payload(path, text, False, {"error": "invalid", "message": str(exc)})
        except _UNREACHABLE as exc:
            result = result_payload(path, text, False, {"error": "unreachable", "message": str(exc)})
        if self.client is not None:
            await self.client.publish(self.topics.result, result, qos=1, retain=False)

    # --- MQTT side -------------------------------------------------------

    def legacy_topics(self) -> list[str]:
        """Retained topics of the pre-plugin bridge (pi-panel-compositor-mqtt
        0.1) for this device. Cleared at every connect: harmless when already
        empty, and it removes the old bridge's Home Assistant entities."""
        p, node = self.topics.prefix, self.discovery.node_id
        prefix = self.discovery.prefix
        old_entities = ["select/active_view", "sensor/active_view_name", "sensor/view_count",
                        "sensor/output_resolution", "sensor/refresh_rate",
                        "binary_sensor/transitioning", "binary_sensor/compositor_reachable",
                        "button/restart_active_view"]
        topics = [f"{p}/bridge/availability", f"{p}/status", f"{p}/views", f"{p}/compositor/version"]
        if self.settings.homeassistant.enabled:
            for entity in old_entities:
                component, object_id = entity.split("/")
                topics.append(f"{prefix}/{component}/{node}/{object_id}/config")
        return topics

    async def session(self, client: aiomqtt.Client) -> None:
        self.client = client
        self._published.clear()   # the broker may have lost retained state
        for topic in self.legacy_topics():
            await client.publish(topic, b"", qos=1, retain=True)
        await client.publish(self.topics.availability, ONLINE, qos=1, retain=True)
        await client.subscribe(self.topics.command_wildcard, qos=1)
        log.info("bridge online: %s", self.topics.prefix)
        await self.sync()
        try:
            async for message in client.messages:
                await self.handle(str(message.topic), message.payload)
        finally:
            self.client = None
            # A clean disconnect does NOT fire the last will; say it ourselves.
            with contextlib.suppress(Exception):
                await client.publish(self.topics.availability, OFFLINE, qos=1, retain=True)

    async def mqtt_loop(self) -> None:
        mqtt = self.settings.mqtt
        will = aiomqtt.Will(topic=self.topics.availability, payload=OFFLINE, qos=1, retain=True)
        delay = RETRY_MIN
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=mqtt.host, port=mqtt.port,
                    username=mqtt.username or None, password=mqtt.password or None,
                    keepalive=mqtt.keepalive, will=will,
                    tls_params=aiomqtt.TLSParameters() if mqtt.tls else None,
                    identifier=f"pi-panel-mqtt-{self.topics.device_id}",
                ) as client:
                    delay = RETRY_MIN
                    await self.session(client)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT: %s; reconnecting in %.0fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.ctl_loop())
            tg.create_task(self.mqtt_loop())


def _on(text: str) -> bool:
    value = text.strip().upper()
    if value in (ON, "TRUE", "1", "YES"):
        return True
    if value in (OFF, "FALSE", "0", "NO"):
        return False
    raise ValueError(f"expected ON or OFF, got {text!r}")


def _clock_params(parts: list[str], payload: str) -> dict[str, Any]:
    """command/clock (JSON) or command/clock/<field> (one value) -> SetClock
    parameters. ctl validates the values; this only gets their types right."""
    if not parts:
        data = json.loads(payload) if payload.startswith("{") else None
        if not isinstance(data, dict) or not data or set(data) - set(CLOCK_KEYS):
            raise ValueError(f"clock expects a JSON object with any of {', '.join(CLOCK_KEYS)}")
        return data
    if len(parts) != 1 or parts[0] not in CLOCK_KEYS:
        raise ValueError(f"unknown command clock/{'/'.join(parts)}")
    key = parts[0]
    if key == "enabled":
        return {"enabled": _on(payload)}
    if key == "size":
        try:
            size = float(payload)   # Home Assistant's number entity may send "48.0"
        except ValueError:
            raise ValueError(f"size must be a number, got {payload!r}") from None
        if not size.is_integer():
            raise ValueError(f"size must be a whole number, got {payload!r}")
        return {"size": int(size)}
    return {key: payload}
