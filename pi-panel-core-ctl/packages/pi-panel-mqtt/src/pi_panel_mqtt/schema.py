"""The MQTT contract: every topic and payload shape, in one place.

State topics are retained, and published only when they change:

    <base>/<device>/availability             online/offline  the bridge (MQTT last will)
    <base>/<device>/ctl/availability         online/offline  pi-panel-ctl reachable
    <base>/<device>/compositor/availability  online/offline  the display server, as ctl sees it
    <base>/<device>/state                    JSON io.pipanel.Ctl State
    <base>/<device>/apps                     JSON [App]
    <base>/<device>/rotations                JSON {"selected": ..., "rotations": [names]}
    <base>/<device>/app/<name>/actions       JSON [{name, label}] from the app's io.pipanel.App

Command topics are never retained (a retained command re-fires at every
broker reconnect):

    command/show                  app name, or JSON {"app", "seconds"?, "priority"?}: a hold
    command/switch                app name: show it now, as a person picks (the select)
    command/release               hold token
    command/next, command/previous
    command/rotation/paused       ON / OFF
    command/rotation/select       rotation name
    command/app/<name>/restart
    command/app/<name>/enabled    ON / OFF
    command/app/<name>/action/<action>
    command/clock                 JSON {"enabled"?, "format"?, "position"?, "size"?}
    command/clock/enabled         ON / OFF
    command/clock/format          strftime(3) format, e.g. %H:%M
    command/clock/position        top_left, top_right, bottom_left, bottom_right, center
    command/clock/size            text height in px; 0 = automatic

The clock overlay's settings are part of the state topic (`clock`).

    command/result                JSON outcome of the last command (not retained)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

ONLINE = "online"
OFFLINE = "offline"
ON = "ON"
OFF = "OFF"
CLOCK_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right", "center")
CLOCK_KEYS = ("enabled", "format", "position", "size")


@dataclass(frozen=True, slots=True)
class Topics:
    base: str = "pi-panel"
    device_id: str = "unknown"

    @property
    def prefix(self) -> str:
        return f"{self.base}/{self.device_id}"

    # --- state (retained) ------------------------------------------------
    @property
    def availability(self) -> str:
        return f"{self.prefix}/availability"

    @property
    def ctl_availability(self) -> str:
        return f"{self.prefix}/ctl/availability"

    @property
    def compositor_availability(self) -> str:
        return f"{self.prefix}/compositor/availability"

    @property
    def state(self) -> str:
        return f"{self.prefix}/state"

    @property
    def apps(self) -> str:
        return f"{self.prefix}/apps"

    @property
    def rotations(self) -> str:
        return f"{self.prefix}/rotations"

    def app_actions(self, app: str) -> str:
        return f"{self.prefix}/app/{app}/actions"

    # --- commands (never retained) ---------------------------------------
    @property
    def command_prefix(self) -> str:
        return f"{self.prefix}/command/"

    @property
    def command_wildcard(self) -> str:
        return f"{self.prefix}/command/#"

    def command(self, *parts: str) -> str:
        return self.command_prefix + "/".join(parts)

    @property
    def result(self) -> str:
        return self.command("result")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def result_payload(command: str, request: str, ok: bool, detail: Any = None) -> bytes:
    """Outcome of one command, including ctl's error name and parameters."""
    return encode({"command": command, "request": request, "ok": ok,
                   "detail": detail, "ts": now_iso()})
