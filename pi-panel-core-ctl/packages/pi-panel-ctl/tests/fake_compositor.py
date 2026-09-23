"""An in-process io.pipanel.Compositor, close enough to the C one for ctl tests.

It keeps slots, answers the same errors, and streams the same event kinds.
Tests drive the "windows" directly: `map("a")` is an app's window appearing.
"""

from __future__ import annotations

from typing import Any

from pi_panel_varlink import Broadcaster, Call, Interface, Server, VarlinkError

IFACE = "io.pipanel.Compositor"

# Enough of the real description for varlink introspection to be meaningful.
DESCRIPTION = """\
interface io.pipanel.Compositor
method RegisterSlot(name: string, match_unit: ?string, match_app_id: ?string) -> (slot: object)
method UnregisterSlot(name: string) -> ()
method ListSlots() -> (slots: []object)
method GetStatus() -> (status: object)
method Switch(slot: string, transition: ?string) -> ()
method SetOutputPower(on: bool) -> ()
method SetClock(enabled: bool, format: ?string, position: ?string, size: ?int) -> (clock: object)
method Subscribe() -> (event: object)
error NoSuchSlot (slot: string)
error SlotNotMapped (slot: string)
error ClockUnavailable (reason: string)
"""

DEFAULT_CLOCK = {"enabled": False, "format": "%H:%M", "position": "bottom_right", "size": 0}


class FakeCompositor:
    def __init__(self, path: str) -> None:
        self.path = path
        self.slots: dict[str, dict[str, Any]] = {}
        self.active: str | None = None
        self.power = True
        self.switches: list[tuple[str, str]] = []
        self.clock = dict(DEFAULT_CLOCK)
        self.clock_calls: list[dict[str, Any]] = []
        self.clock_unavailable = False   # SetClock(enabled) fails, as with no font
        self.events = Broadcaster()
        self.server: Server | None = None
        self._next_id = 1

    # --- state helpers ---------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "transitioning": False, "output_power": self.power,
                "output": {"name": "HEADLESS-1", "width": 1280, "height": 720, "refresh": 0},
                "clock": dict(self.clock)}

    def _emit(self, kind: str, slot: dict[str, Any] | None = None) -> None:
        event: dict[str, Any] = {"kind": kind, "status": self.status()}
        if slot is not None:
            event["slot"] = dict(slot)
        self.events.publish(event)

    def map(self, name: str) -> None:
        """An app window appears in slot |name|."""
        slot = self.slots[name]
        slot["mapped"] = True
        self._emit("slot_mapped", slot)
        if self.active is None:
            self._activate(name)

    def unmap(self, name: str) -> None:
        slot = self.slots[name]
        slot["mapped"] = False
        self._emit("slot_unmapped", slot)
        if self.active == name:
            slot["active"] = False
            self.active = None
            self._emit("active_changed")

    def _activate(self, name: str) -> None:
        for s in self.slots.values():
            s["active"] = s["name"] == name
        self.active = name
        self._emit("active_changed")

    # --- server ----------------------------------------------------------

    async def start(self) -> None:
        iface = Interface(DESCRIPTION)
        iface.add_method("RegisterSlot", self._register)
        iface.add_method("UnregisterSlot", self._unregister)
        iface.add_method("ListSlots", self._list)
        iface.add_method("GetStatus", self._get_status)
        iface.add_method("Switch", self._switch)
        iface.add_method("SetOutputPower", self._power)
        iface.add_method("SetClock", self._set_clock)
        iface.add_method("Subscribe", self._subscribe)
        self.server = Server(product="fake-compositor", version="0")
        self.server.add_interface(iface)
        await self.server.start(self.path)

    async def stop(self) -> None:
        if self.server:
            await self.server.close()
            self.server = None

    async def _register(self, call: Call) -> dict[str, Any]:
        name = call.param("name", str)
        slot = self.slots.get(name)
        if slot is None:
            slot = {"id": self._next_id, "name": name, "registered": True,
                    "match_unit": call.param("match_unit", str, f"pi-panel-app@{name}.service"),
                    "match_app_id": call.param("match_app_id", str, None),
                    "mapped": False, "active": False, "app_id": None, "title": None, "unit": None}
            self._next_id += 1
            self.slots[name] = slot
            self._emit("slot_added", slot)
        return {"slot": slot}

    async def _unregister(self, call: Call) -> None:
        name = call.param("name", str)
        slot = self.slots.pop(name, None)
        if slot is None:
            raise VarlinkError(f"{IFACE}.NoSuchSlot", {"slot": name})
        self._emit("slot_removed", slot)

    async def _list(self, call: Call) -> dict[str, Any]:
        return {"slots": list(self.slots.values())}

    async def _get_status(self, call: Call) -> dict[str, Any]:
        return {"status": self.status()}

    async def _switch(self, call: Call) -> None:
        name = call.param("slot", str)
        slot = self.slots.get(name)
        if slot is None:
            raise VarlinkError(f"{IFACE}.NoSuchSlot", {"slot": name})
        if not slot["mapped"]:
            raise VarlinkError(f"{IFACE}.SlotNotMapped", {"slot": name})
        self.switches.append((name, call.param("transition", str, "fade")))
        self._activate(name)

    async def _power(self, call: Call) -> None:
        self.power = call.param("on", bool)
        self._emit("output_power_changed")

    async def _set_clock(self, call: Call) -> dict[str, Any]:
        enabled = call.param("enabled", bool)
        self.clock_calls.append(dict(call.parameters))
        if enabled and self.clock_unavailable:
            raise VarlinkError(f"{IFACE}.ClockUnavailable", {"reason": "no font"})
        self.clock["enabled"] = enabled
        for key in ("format", "position", "size"):
            if call.parameters.get(key) is not None:
                self.clock[key] = call.parameters[key]
        self._emit("clock_changed")
        return {"clock": dict(self.clock)}

    def reset_clock(self) -> None:
        """What a compositor restart does to the clock."""
        self.clock = dict(DEFAULT_CLOCK)
        self._emit("clock_changed")

    async def _subscribe(self, call: Call) -> None:
        call.require_more()
        with self.events.subscribe() as sub:
            await call.reply({"event": {"kind": "snapshot", "status": self.status(),
                                        "slots": list(self.slots.values())}})
            async for event in sub:
                await call.reply({"event": event})
