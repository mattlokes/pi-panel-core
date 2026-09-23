"""pi-panel-ctld: the central control daemon.

One asyncio loop ties it together:

    compositor events ─┐
    unit state changes ├─▶ kick ─▶ decide() ─▶ Switch / SetOutputPower / SetVisible
    Varlink requests  ─┤                  └──▶ publish state/apps to subscribers
    engine timer      ─┘

Every input just sets `kick`; one reconcile pass then compares what the engine
wants with what the compositor reports and closes the gap. That keeps the
logic in one place and makes it idempotent: a missed or duplicated event
cannot leave the panel wrong for longer than one pass.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

from pi_panel_varlink import (
    Broadcaster,
    Call,
    Client,
    Interface,
    Server,
    SubscriberOverflow,
    VarlinkError,
    sd_notify,
)

from . import paths
from .compositor import CompositorLink
from .config import CtlConfig
from .engine import Clock, Engine, SystemClock
from .model import Rotation, RotationEntry, Schedule, ValidationError
from .packages import Package, load_registry
from .units import SystemdUnits, UnitError, Units

log = logging.getLogger("pi-panel-ctl")

IFACE = "io.pipanel.Ctl"
VERSION = "0.1.0"
APP_CALL_TIMEOUT = 2.0


def _err(name: str, **params: Any) -> VarlinkError:
    return VarlinkError(f"{IFACE}.{name}", params)


class Ctl:
    def __init__(
        self,
        *,
        config_path: Path,
        registry_path: Path,
        units: Units,
        compositor_address: str,
        clock: Clock | None = None,
    ) -> None:
        self.config_path = config_path
        self.registry_path = registry_path
        self.config = CtlConfig.load(config_path)
        self.packages: dict[str, Package] = load_registry(registry_path)
        self.units = units
        self.units.on_change = lambda unit, state: self.kick()
        self.compositor = CompositorLink(compositor_address, on_change=self.kick)
        self.engine = Engine(
            rotations={},
            selected=self.config.selected_rotation,
            schedules=list(self.config.schedules),
            clock=clock or SystemClock(),
        )
        self._sync_rotations()

        self.events = Broadcaster(maxsize=256)
        self._kick = asyncio.Event()
        self._last_state: dict[str, Any] | None = None
        self._last_apps: list[dict[str, Any]] | None = None
        self._visible: dict[str, bool] = {}          # last SetVisible sent per app
        self._interfaces: dict[str, list[str]] = {}  # app -> interfaces from GetInfo
        self._tasks: set[asyncio.Task[Any]] = set()
        self.server: Server | None = None

    # --- plumbing --------------------------------------------------------

    def kick(self) -> None:
        self._kick.set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _save(self) -> None:
        self.config.selected_rotation = self.engine.selected
        self.config.schedules = list(self.engine.schedules)
        try:
            self.config.save(self.config_path)
        except OSError as exc:
            log.error("cannot save %s: %s", self.config_path, exc)

    def _apps(self) -> list[Package]:
        return [p for p in self.packages.values() if p.kind == "app"]

    def _enabled_apps(self) -> list[str]:
        return [n for n in self.config.enabled
                if n in self.packages and self.packages[n].kind == "app"]

    def _sync_rotations(self) -> None:
        """Configured rotations, plus the implicit 'default' when none is set:
        every enabled app in turn, `default_seconds` each."""
        rotations = dict(self.config.rotations)
        if "default" not in rotations:
            rotations["default"] = Rotation("default", tuple(
                RotationEntry(n, self.config.default_seconds) for n in self._enabled_apps()
            ))
        self.engine.rotations = rotations

    # --- packages ----------------------------------------------------------

    async def reconcile_packages(self) -> None:
        """Make units and slots match `enabled`: start and register what is
        enabled, stop and unregister what is not (or is no longer installed)."""
        for pkg in self.packages.values():
            unit = paths.unit_for(pkg.kind, pkg.name)
            state = await self.units.watch(unit)
            if pkg.name in self.config.enabled:
                if pkg.kind == "app":
                    await self.compositor.register(pkg.name, match_unit=unit)
                if state not in ("active", "activating", "reloading"):
                    try:
                        await self.units.start(unit)
                    except UnitError as exc:
                        log.error("cannot start %s: %s", unit, exc.message)
            else:
                if state in ("active", "activating", "reloading"):
                    with contextlib.suppress(UnitError):
                        await self.units.stop(unit)
                if pkg.kind == "app":
                    await self.compositor.unregister(pkg.name)
                    self.engine.release_app(pkg.name)
        for name in self.compositor.wanted() - set(self.packages):
            await self.compositor.unregister(name)   # uninstalled
        self._sync_rotations()
        self.kick()

    # --- views of the state ------------------------------------------------

    def state_dict(self, decision: Any = None) -> dict[str, Any]:
        decision = decision or self.engine.decide()
        mono = self.engine.clock.monotonic()
        return {
            "showing": self.compositor.active,
            "reason": decision.reason,
            "output_power": self.compositor.output_power if self.compositor.online else decision.power,
            "rotation": decision.rotation,
            "rotation_paused": self.engine.paused,
            "holds": [
                {"token": h.token, "app": h.app, "priority": h.priority,
                 "expires_in": None if h.expires_at is None else round(h.expires_in(mono), 1)}
                for h in sorted(self.engine.holds.values(), key=lambda h: h.seq)
            ],
            "active_schedules": list(decision.active_schedules),
            "compositor_online": self.compositor.online,
        }

    def apps_list(self) -> list[dict[str, Any]]:
        slots = self.compositor.slots
        visible = self.compositor.active if self.compositor.output_power else None
        out = []
        for pkg in sorted(self.packages.values(), key=lambda p: (p.kind, p.name)):
            unit = paths.unit_for(pkg.kind, pkg.name)
            slot = slots.get(pkg.name) if pkg.kind == "app" else None
            has_socket = pkg.kind == "app" and pkg.varlink
            out.append({
                "name": pkg.name,
                "kind": pkg.kind,
                "version": pkg.version,
                "description": pkg.description,
                "enabled": pkg.name in self.config.enabled,
                "unit": unit,
                "unit_state": self.units.state(unit),
                "mapped": bool(slot and slot.get("mapped")),
                "visible": pkg.kind == "app" and visible == pkg.name,
                "socket": str(paths.app_socket(pkg.name)) if has_socket else None,
                "interfaces": self._interfaces.get(pkg.name, []),
            })
        return out

    # --- the reconcile loop ------------------------------------------------

    async def run(self) -> None:
        self._spawn(self.compositor.run())
        await self.reconcile_packages()
        while True:
            self._kick.clear()
            try:
                await self._pass()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("reconcile pass failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._kick.wait(), self.engine.next_wakeup())

    async def _pass(self) -> None:
        comp = self.compositor
        available = comp.mapped() & set(self._enabled_apps())
        self.engine.set_available(available)
        decision = self.engine.decide()

        if comp.online:
            if decision.power != comp.output_power:
                await comp.set_power(decision.power)
            # Never pile switches onto a fade in progress: its end is an event
            # that kicks another pass, which re-evaluates from the new state.
            if (decision.power and decision.showing and decision.showing in available
                    and decision.showing != comp.active and not comp.transitioning):
                await comp.switch(decision.showing, self.config.transition)

        self._update_visibility()
        self._publish(decision)

    def _update_visibility(self) -> None:
        comp = self.compositor
        on_screen = comp.active if comp.online and comp.output_power else None
        for pkg in self._apps():
            if not pkg.varlink or pkg.name not in self.config.enabled:
                continue
            running = self.units.state(paths.unit_for("app", pkg.name)) == "active"
            if not running:
                self._visible.pop(pkg.name, None)
                self._interfaces.pop(pkg.name, None)
                continue
            if pkg.name not in self._interfaces:
                self._interfaces[pkg.name] = []
                self._spawn(self._probe_app(pkg.name))
            visible = on_screen == pkg.name
            if self._visible.get(pkg.name) != visible:
                self._visible[pkg.name] = visible
                self._spawn(self._set_visible(pkg.name, visible))

    async def _probe_app(self, name: str) -> None:
        """Learn an app's interfaces. It may still be starting, so retry briefly."""
        client = Client(str(paths.app_socket(name)), timeout=APP_CALL_TIMEOUT)
        for _ in range(10):
            try:
                info = await client.get_info()
                self._interfaces[name] = [i for i in info.get("interfaces", [])
                                          if i != "org.varlink.service"]
                self.kick()
                return
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1.0)
        self._interfaces.pop(name, None)  # try again on a later pass

    async def _set_visible(self, name: str, visible: bool) -> None:
        client = Client(str(paths.app_socket(name)), timeout=APP_CALL_TIMEOUT)
        try:
            await client.call("io.pipanel.App.SetVisible", {"visible": visible})
        except Exception as exc:  # noqa: BLE001 - optional for apps
            log.debug("SetVisible(%s) on %s: %s", visible, name, exc)
            self._visible.pop(name, None)   # retry on a later pass

    def _publish(self, decision: Any) -> None:
        state = self.state_dict(decision)
        apps = self.apps_list()
        # Countdowns change every pass; they are not a reason to wake subscribers.
        comparable = {**state, "holds": [{**h, "expires_in": None} for h in state["holds"]]}
        last = self._last_state
        last_cmp = None if last is None else {
            **last, "holds": [{**h, "expires_in": None} for h in last["holds"]]}
        if apps != self._last_apps:
            self._last_apps = apps
            self._last_state = state
            self.events.publish({"kind": "apps_changed", "state": state, "apps": apps})
        elif comparable != last_cmp:
            self._last_state = state
            self.events.publish({"kind": "state_changed", "state": state})

    def config_changed(self) -> None:
        self._save()
        self.events.publish({"kind": "config_changed", "state": self.state_dict()})
        self.kick()

    # --- Varlink ---------------------------------------------------------

    def interface(self) -> Interface:
        description = files("pi_panel_ctl.interfaces").joinpath(
            "io.pipanel.Ctl.varlink").read_text(encoding="utf-8")
        iface = Interface(description)
        for name in ("GetState", "ListApps", "Show", "Release", "Next", "Previous",
                     "PauseRotation", "ResumeRotation", "GetRotations", "SetRotation",
                     "RemoveRotation", "SelectRotation", "GetSchedules", "SetSchedule",
                     "RemoveSchedule", "EnablePackage", "RestartPackage", "Rescan",
                     "Subscribe"):
            iface.add_method(name, getattr(self, f"v_{name}"))
        return iface

    def _require_app(self, name: str) -> Package:
        pkg = self.packages.get(name)
        if pkg is None or pkg.kind != "app":
            raise _err("NoSuchApp", app=name)
        return pkg

    async def v_GetState(self, call: Call) -> dict[str, Any]:
        return {"state": self.state_dict()}

    async def v_ListApps(self, call: Call) -> dict[str, Any]:
        return {"apps": self.apps_list()}

    async def v_Show(self, call: Call) -> dict[str, Any]:
        app = call.param("app", str)
        self._require_app(app)
        seconds = call.param("seconds", (int, float), None)
        if seconds is not None and (isinstance(seconds, bool) or seconds <= 0):
            raise VarlinkError.invalid_parameter("seconds")
        priority = call.param("priority", int, 0)
        token = self.engine.show(app, float(seconds) if seconds is not None else None, priority)
        log.info("hold %s: %s for %s at priority %d", token, app,
                 f"{seconds}s" if seconds else "until released", priority)
        self.kick()
        return {"token": token}

    async def v_Release(self, call: Call) -> None:
        token = call.param("token", str)
        if not self.engine.release(token):
            raise _err("NoSuchHold", token=token)
        self.kick()

    async def v_Next(self, call: Call) -> None:
        self.engine.step(+1)
        self.kick()

    async def v_Previous(self, call: Call) -> None:
        self.engine.step(-1)
        self.kick()

    async def v_PauseRotation(self, call: Call) -> None:
        self.engine.pause()
        self.kick()

    async def v_ResumeRotation(self, call: Call) -> None:
        self.engine.resume()
        self.kick()

    async def v_GetRotations(self, call: Call) -> dict[str, Any]:
        return {"rotations": [r.to_dict() for r in self.engine.rotations.values()],
                "selected": self.engine.selected}

    async def v_SetRotation(self, call: Call) -> None:
        raw = call.param("rotation", dict)
        try:
            rotation = Rotation.from_dict(raw)
        except ValidationError as exc:
            raise _err("InvalidRotation", rotation=str(raw.get("name")), reason=str(exc))
        unknown = [e.app for e in rotation.entries if e.app not in self.packages
                   or self.packages[e.app].kind != "app"]
        if unknown:
            raise _err("InvalidRotation", rotation=rotation.name,
                       reason=f"unknown app(s): {', '.join(unknown)}")
        self.config.rotations[rotation.name] = rotation
        self.engine.set_rotation(rotation)
        self.config_changed()

    async def v_RemoveRotation(self, call: Call) -> None:
        name = call.param("name", str)
        if name not in self.config.rotations:
            raise _err("NoSuchRotation", rotation=name)
        del self.config.rotations[name]
        self.engine.remove_rotation(name)
        if self.engine.selected == name:
            self.engine.select_rotation("default")
        self._sync_rotations()
        self.config_changed()

    async def v_SelectRotation(self, call: Call) -> None:
        name = call.param("name", str)
        if name not in self.engine.rotations:
            raise _err("NoSuchRotation", rotation=name)
        self.engine.select_rotation(name)
        self.config_changed()

    async def v_GetSchedules(self, call: Call) -> dict[str, Any]:
        now = self.engine.clock.now()
        return {"schedules": [s.to_dict(now) for s in self.engine.schedules]}

    async def v_SetSchedule(self, call: Call) -> None:
        raw = dict(call.param("schedule", dict))
        raw.pop("active", None)
        name = str(raw.get("name"))
        try:
            schedule = Schedule.from_dict(raw)
        except ValidationError as exc:
            raise _err("InvalidSchedule", schedule=name, reason=str(exc))
        if schedule.action == "show":
            if schedule.app not in self.packages or self.packages[schedule.app].kind != "app":
                raise _err("InvalidSchedule", schedule=name, reason=f"unknown app {schedule.app!r}")
        if schedule.action == "rotation" and schedule.rotation not in self.engine.rotations:
            raise _err("InvalidSchedule", schedule=name,
                       reason=f"unknown rotation {schedule.rotation!r}")
        rules = [s for s in self.engine.schedules if s.name != schedule.name]
        rules.append(schedule)
        self.engine.schedules = rules
        self.config_changed()

    async def v_RemoveSchedule(self, call: Call) -> None:
        name = call.param("name", str)
        rules = [s for s in self.engine.schedules if s.name != name]
        if len(rules) == len(self.engine.schedules):
            raise _err("NoSuchSchedule", schedule=name)
        self.engine.schedules = rules
        self.config_changed()

    async def v_EnablePackage(self, call: Call) -> None:
        name = call.param("name", str)
        enabled = call.param("enabled", bool)
        if name not in self.packages:
            raise _err("NoSuchApp", app=name)
        if enabled and name not in self.config.enabled:
            self.config.enabled.append(name)
        elif not enabled and name in self.config.enabled:
            self.config.enabled.remove(name)
        self._save()
        await self.reconcile_packages()
        self.events.publish({"kind": "config_changed", "state": self.state_dict()})

    async def v_RestartPackage(self, call: Call) -> None:
        name = call.param("name", str)
        pkg = self.packages.get(name)
        if pkg is None:
            raise _err("NoSuchApp", app=name)
        unit = paths.unit_for(pkg.kind, pkg.name)
        try:
            await self.units.restart(unit)
        except UnitError as exc:
            raise _err("UnitFailed", unit=unit, message=exc.message)

    async def v_Rescan(self, call: Call) -> dict[str, Any]:
        self.packages = load_registry(self.registry_path)
        log.info("rescanned: %d package(s) installed", len(self.packages))
        await self.reconcile_packages()
        return {"apps": self.apps_list()}

    async def v_Subscribe(self, call: Call) -> None:
        call.require_more()
        with self.events.subscribe() as sub:
            await call.reply({"event": {"kind": "snapshot", "state": self.state_dict(),
                                        "apps": self.apps_list()}})
            try:
                async for event in sub:
                    await call.reply({"event": event})
            except SubscriberOverflow:
                log.warning("dropping a subscriber that fell behind")

    # --- lifecycle -------------------------------------------------------

    async def serve(self, address: str) -> None:
        self.server = Server(product="pi-panel-ctl", version=VERSION,
                             url="https://github.com/mattlokes/pi-panel-core")
        self.server.add_interface(self.interface())
        Path(address).parent.mkdir(parents=True, exist_ok=True)
        await self.server.start(address)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self.server:
            await self.server.close()
        await self.units.close()


# --- entry point ---------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pi-panel-ctld", description="pi-panel control daemon")
    p.add_argument("--config", type=Path, default=None, help=f"default: {paths.ctl_config()}")
    p.add_argument("--registry", type=Path, default=None, help=f"default: {paths.registry()}")
    p.add_argument("--socket", default=None, help=f"default: {paths.ctl_socket()}")
    p.add_argument("--compositor-socket", default=None,
                   help=f"default: {paths.compositor_socket()}")
    p.add_argument("--user-units", action="store_true",
                   help="manage units on the user manager (development)")
    p.add_argument("--log-level", default="INFO")
    return p


async def _amain(args: argparse.Namespace) -> None:
    units = SystemdUnits(user=args.user_units)
    await units.connect()
    ctl = Ctl(
        config_path=args.config or paths.ctl_config(),
        registry_path=args.registry or paths.registry(),
        units=units,
        compositor_address=args.compositor_socket or str(paths.compositor_socket()),
    )
    await ctl.serve(args.socket or str(paths.ctl_socket()))
    sd_notify("READY=1")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    runner = loop.create_task(ctl.run())
    await stop.wait()
    log.info("shutting down (apps keep running; the compositor keeps its slots)")
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    await ctl.close()


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s" if sys.stderr.isatty()
        else "%(levelname)-7s %(message)s",
    )
    try:
        asyncio.run(_amain(args))
    except ValidationError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
