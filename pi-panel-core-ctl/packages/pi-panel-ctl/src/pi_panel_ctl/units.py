"""Start, stop and watch systemd units over D-Bus.

ctl runs as the unprivileged user `pi` but manages *system* units
(pi-panel-app@*.service, pi-panel-plugin@*.service). The polkit rule shipped
in deploy/polkit/ grants exactly that, and nothing else.

Unit state is pushed, not polled: after Manager.Subscribe(), systemd emits
PropertiesChanged for every unit whose ActiveState changes, and we keep a
cache of the units we care about.

For development and tests, `SystemdUnits(user=True)` drives the user manager
instead (no polkit involved), and `FakeUnits` does it all in memory.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Protocol

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus

log = logging.getLogger("pi-panel-ctl")

SYSTEMD = "org.freedesktop.systemd1"
MANAGER_PATH = "/org/freedesktop/systemd1"
MANAGER = "org.freedesktop.systemd1.Manager"
UNIT = "org.freedesktop.systemd1.Unit"
PROPERTIES = "org.freedesktop.DBus.Properties"

StateCallback = Callable[[str, str], None]


class UnitError(Exception):
    def __init__(self, unit: str, message: str) -> None:
        super().__init__(f"{unit}: {message}")
        self.unit = unit
        self.message = message


class Units(Protocol):
    on_change: StateCallback | None

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def watch(self, unit: str) -> str: ...
    def state(self, unit: str) -> str: ...
    async def start(self, unit: str) -> None: ...
    async def stop(self, unit: str) -> None: ...
    async def restart(self, unit: str) -> None: ...


class SystemdUnits:
    def __init__(self, user: bool = False, on_change: StateCallback | None = None) -> None:
        self.user = user
        self.on_change = on_change
        self._bus: MessageBus | None = None
        self._paths: dict[str, str] = {}   # object path -> unit name
        self._states: dict[str, str] = {}  # unit name -> ActiveState

    async def connect(self) -> None:
        bus_type = BusType.SESSION if self.user else BusType.SYSTEM
        self._bus = await MessageBus(bus_type=bus_type).connect()
        self._bus.add_message_handler(self._on_message)
        # Without Subscribe, systemd does not emit unit change signals at all.
        await self._call(MANAGER_PATH, MANAGER, "Subscribe")
        await self._bus_call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "AddMatch", "s",
            [f"type='signal',sender='{SYSTEMD}',interface='{PROPERTIES}',"
             f"member='PropertiesChanged',path_namespace='{MANAGER_PATH}/unit'"],
        )
        # Re-resolve everything we were watching (a reconnect after dbus restarts).
        for unit in list(self._states):
            await self.watch(unit)

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def wait_closed(self) -> None:
        if self._bus is not None:
            await self._bus.wait_for_disconnect()

    # --- low level -------------------------------------------------------

    async def _bus_call(self, dest: str, path: str, iface: str, member: str,
                        signature: str = "", body: list | None = None) -> list:
        if self._bus is None:
            raise UnitError("-", "not connected to D-Bus")
        reply = await self._bus.call(Message(
            destination=dest, path=path, interface=iface, member=member,
            signature=signature, body=body or [],
        ))
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else ""
            raise UnitError("-", f"{reply.error_name}: {detail}")
        return reply.body

    async def _call(self, path: str, iface: str, member: str,
                    signature: str = "", body: list | None = None) -> list:
        return await self._bus_call(SYSTEMD, path, iface, member, signature, body)

    def _on_message(self, msg: Message) -> None:
        if (msg.message_type != MessageType.SIGNAL or msg.member != "PropertiesChanged"
                or msg.interface != PROPERTIES):
            return
        unit = self._paths.get(msg.path)
        if unit is None or not msg.body or msg.body[0] != UNIT:
            return
        changed = msg.body[1]
        if "ActiveState" in changed:
            state = changed["ActiveState"].value
            if self._states.get(unit) != state:
                self._states[unit] = state
                log.debug("unit %s is %s", unit, state)
                if self.on_change:
                    self.on_change(unit, state)

    # --- API -------------------------------------------------------------

    async def watch(self, unit: str) -> str:
        """Track a unit's ActiveState. Returns the current state."""
        try:
            (path,) = await self._call(MANAGER_PATH, MANAGER, "LoadUnit", "s", [unit])
            self._paths[path] = unit
            (variant,) = await self._call(path, PROPERTIES, "Get", "ss", [UNIT, "ActiveState"])
            state = variant.value
        except UnitError as exc:
            log.warning("cannot watch %s: %s", unit, exc.message)
            state = "unknown"
        self._states[unit] = state
        return state

    def state(self, unit: str) -> str:
        return self._states.get(unit, "unknown")

    async def _job(self, method: str, unit: str) -> None:
        try:
            await self._call(MANAGER_PATH, MANAGER, method, "ss", [unit, "replace"])
        except UnitError as exc:
            raise UnitError(unit, exc.message) from None

    async def start(self, unit: str) -> None:
        await self._job("StartUnit", unit)

    async def stop(self, unit: str) -> None:
        await self._job("StopUnit", unit)

    async def restart(self, unit: str) -> None:
        await self._job("RestartUnit", unit)


class FakeUnits:
    """In-memory units for tests: jobs complete instantly."""

    def __init__(self, on_change: StateCallback | None = None) -> None:
        self.on_change = on_change
        self.states: dict[str, str] = {}
        self.jobs: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def watch(self, unit: str) -> str:
        return self.states.setdefault(unit, "inactive")

    def state(self, unit: str) -> str:
        return self.states.get(unit, "unknown")

    def set_state(self, unit: str, state: str) -> None:
        self.states[unit] = state
        if self.on_change:
            self.on_change(unit, state)

    async def _job(self, method: str, unit: str, state: str) -> None:
        self.jobs.append((method, unit))
        if unit in self.fail:
            raise UnitError(unit, "Unit not found.")
        await asyncio.sleep(0)
        self.set_state(unit, state)

    async def start(self, unit: str) -> None:
        await self._job("start", unit, "active")

    async def stop(self, unit: str) -> None:
        await self._job("stop", unit, "inactive")

    async def restart(self, unit: str) -> None:
        await self._job("restart", unit, "active")
