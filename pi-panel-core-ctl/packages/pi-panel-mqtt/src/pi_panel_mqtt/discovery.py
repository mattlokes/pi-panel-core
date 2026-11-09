"""Home Assistant MQTT discovery: the panel as a device, with one connected
device per app and plugin.

The panel device holds what concerns the whole panel (what is showing, the
rotation, the clock overlay). Each package gets a device of its own, linked
with `via_device`, holding its enable switch, restart button and actions, so
the panel's page doesn't grow with every package.

The entity set depends on the panel's contents (one restart button and one
enable switch per app, one button per app action), so `messages()` builds the
full set from current state and the bridge republishes whenever that changes.
Entities that disappear are removed by publishing an empty retained config,
which is how Home Assistant deletes a discovered entity.

There is deliberately no way to stop the compositor from here: a mis-tap on a
dashboard should never blank the panel for good.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .schema import CLOCK_POSITIONS, OFF, ON, Topics, encode

_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _slug(value: str) -> str:
    return _SAFE.sub("_", value)


@dataclass(frozen=True, slots=True)
class DiscoveryMessage:
    topic: str
    payload: bytes
    retain: bool = True


class Discovery:
    def __init__(self, topics: Topics, prefix: str = "homeassistant") -> None:
        self.topics = topics
        self.prefix = prefix
        self.node_id = f"pi_panel_{_slug(topics.device_id)}"

    @property
    def _device(self) -> dict[str, Any]:
        return {
            "identifiers": [self.node_id],
            "name": f"Pi Panel ({self.topics.device_id})",
            "manufacturer": "pi-panel",
            "model": "pi-panel",
        }

    def _package_device(self, app: dict[str, Any]) -> dict[str, Any]:
        """"Pi Panel (jazz) - Immich", connected via the panel."""
        name = app["name"]
        device = {
            "identifiers": [f"{self.node_id}_{_slug(name)}"],
            "name": f"Pi Panel ({self.topics.device_id}) - {name[:1].upper()}{name[1:]}",
            "manufacturer": "pi-panel",
            "model": f"pi-panel {app['kind']}",
            "via_device": self.node_id,
        }
        if app.get("version"):
            device["sw_version"] = app["version"]
        return device

    def _availability(self, compositor: bool = False) -> list[dict[str, str]]:
        """Available only while the bridge and ctl are up: otherwise the state
        topics hold stale values and the entities must not lie. Display
        entities also need the compositor."""
        topics = [self.topics.availability, self.topics.ctl_availability]
        if compositor:
            topics.append(self.topics.compositor_availability)
        return [{"topic": t} for t in topics]

    def _entity(self, component: str, object_id: str, name: str, *,
                compositor: bool = False, device: dict[str, Any] | None = None,
                **cfg: Any) -> DiscoveryMessage:
        # The unique id (and so the entity id) doesn't depend on the device:
        # entities keep their ids when they move between devices.
        uid = f"{self.node_id}_{_slug(object_id)}"
        payload = {
            "name": name,
            "unique_id": uid,
            "object_id": uid,
            "device": device or self._device,
            "availability": self._availability(compositor),
            "availability_mode": "all",
            **cfg,
        }
        return DiscoveryMessage(f"{self.prefix}/{component}/{self.node_id}/{_slug(object_id)}/config",
                                encode(payload))

    def messages(self, apps: list[dict[str, Any]], rotations: list[str],
                 actions: dict[str, list[dict[str, str]]],
                 clock: bool = False) -> list[DiscoveryMessage]:
        """Every entity. |clock|: ctl has the clock overlay (its state says so)."""
        t = self.topics
        app_names = sorted(a["name"] for a in apps if a["kind"] == "app")
        out = [
            # HA reads a select's options from this config, not from a state
            # topic, and rejects an empty list.
            self._entity("select", "showing", "Showing", compositor=True,
                         command_topic=t.command("switch"), state_topic=t.state,
                         value_template="{{ value_json.showing or '' }}",
                         options=app_names or ["(none)"], icon="mdi:monitor-dashboard"),
            self._entity("sensor", "reason", "Showing because", state_topic=t.state,
                         value_template="{{ value_json.reason }}", icon="mdi:help-circle-outline"),
            self._entity("button", "next", "Next", compositor=True,
                         command_topic=t.command("next"), icon="mdi:skip-next"),
            self._entity("button", "previous", "Previous", compositor=True,
                         command_topic=t.command("previous"), icon="mdi:skip-previous"),
            self._entity("switch", "rotation_paused", "Rotation paused",
                         command_topic=t.command("rotation", "paused"), state_topic=t.state,
                         value_template=f"{{{{ '{ON}' if value_json.rotation_paused else '{OFF}' }}}}",
                         payload_on=ON, payload_off=OFF, icon="mdi:pause-circle-outline"),
            self._entity("select", "rotation", "Rotation",
                         command_topic=t.command("rotation", "select"), state_topic=t.state,
                         value_template="{{ value_json.rotation }}",
                         options=sorted(rotations) or ["default"], icon="mdi:playlist-play"),
            self._entity("binary_sensor", "display", "Display", state_topic=t.state,
                         value_template=f"{{{{ '{ON}' if value_json.output_power else '{OFF}' }}}}",
                         payload_on=ON, payload_off=OFF, device_class="power"),
            # Reports the compositor being down, so it must not itself go
            # unavailable when that happens: availability is bridge + ctl only.
            self._entity("binary_sensor", "compositor", "Compositor",
                         state_topic=t.compositor_availability,
                         payload_on="online", payload_off="offline", device_class="connectivity"),
        ]
        if clock:
            out += self._clock_entities()
        for app in sorted(apps, key=lambda a: a["name"]):
            name = app["name"]
            # Home Assistant puts the device name in front of these.
            device = self._package_device(app)
            match = f"{{% for a in value_json if a.name == '{name}' %}}"
            out.append(self._entity(
                "switch", f"{name}_enabled", "Enabled", device=device,
                command_topic=t.command("app", name, "enabled"), state_topic=t.apps,
                value_template=f"{match}{{{{ '{ON}' if a.enabled else '{OFF}' }}}}{{% endfor %}}",
                payload_on=ON, payload_off=OFF, icon="mdi:power-plug"))
            out.append(self._entity(
                "button", f"{name}_restart", "Restart", device=device,
                command_topic=t.command("app", name, "restart"), icon="mdi:restart"))
            for action in actions.get(name, []):
                out.append(self._entity(
                    "button", f"{name}_action_{action['name']}", action["label"],
                    compositor=True, device=device,
                    command_topic=t.command("app", name, "action", action["name"]),
                    icon="mdi:gesture-tap-button"))
        return out

    def _clock_entities(self) -> list[DiscoveryMessage]:
        """The clock overlay. Its settings live in ctl, so these do not need
        the compositor to be up: a change made meanwhile applies when it is."""
        t = self.topics
        return [
            self._entity("switch", "clock_overlay", "Clock overlay",
                         command_topic=t.command("clock", "enabled"), state_topic=t.state,
                         value_template=f"{{{{ '{ON}' if value_json.clock.enabled else '{OFF}' }}}}",
                         payload_on=ON, payload_off=OFF, icon="mdi:clock-digital"),
            self._entity("text", "clock_overlay_format", "Clock overlay format", entity_category="config",
                         command_topic=t.command("clock", "format"), state_topic=t.state,
                         value_template="{{ value_json.clock.format }}",
                         min=1, max=63, icon="mdi:clock-edit-outline"),
            self._entity("select", "clock_overlay_position", "Clock overlay position", entity_category="config",
                         command_topic=t.command("clock", "position"), state_topic=t.state,
                         value_template="{{ value_json.clock.position }}",
                         options=list(CLOCK_POSITIONS), icon="mdi:arrow-all"),
            self._entity("number", "clock_overlay_size", "Clock overlay size", entity_category="config",
                         command_topic=t.command("clock", "size"), state_topic=t.state,
                         value_template="{{ value_json.clock.size }}",
                         min=0, max=1000, step=1, mode="box", unit_of_measurement="px",
                         icon="mdi:format-size"),
        ]

    def removal(self, topic: str) -> DiscoveryMessage:
        """An empty retained config deletes the entity in Home Assistant."""
        return DiscoveryMessage(topic, b"", retain=True)
