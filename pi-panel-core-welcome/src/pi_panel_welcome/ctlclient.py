"""Follow pi-panel-ctl over io.pipanel.Ctl, on a thread of its own (GTK owns
the main one): the installed apps, and the config check's results.

ctl publishes no event for a hand edit of an app's config.toml, so while the
welcome screen is what shows (nothing else is) the check is also re-run every
RECHECK seconds. Hidden, it only re-checks on ctl's own events.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from typing import Any, Callable, Coroutine

from pi_panel_varlink import (
    Client,
    ProtocolError,
    VarlinkError,
    VarlinkTimeout,
    VarlinkUnavailable,
)

log = logging.getLogger("pi-panel-welcome")

CTL = "io.pipanel.Ctl"
CTL_SOCKET = "/run/pi-panel/ctl/io.pipanel.Ctl"
RECHECK = 30.0
CHECK_TIMEOUT = 300.0        # every enabled package's check, in turn
RETRY_MIN, RETRY_MAX = 1.0, 10.0
_UNREACHABLE = (VarlinkUnavailable, VarlinkTimeout, ProtocolError, OSError)

# (ctl_online, apps or None, check results or None)
OnUpdate = Callable[[bool, "list[dict[str, Any]] | None", "list[dict[str, Any]] | None"], None]


def ctl_socket() -> str:
    if "PI_PANEL_CTL_SOCKET" in os.environ:
        return os.environ["PI_PANEL_CTL_SOCKET"]
    runtime = os.environ.get("PI_PANEL_RUNTIME_DIR")
    return f"{runtime}/ctl/{CTL}" if runtime else CTL_SOCKET


class CtlWatcher:
    def __init__(self, on_update: OnUpdate, address: str | None = None) -> None:
        self.on_update = on_update
        self.address = address or ctl_socket()
        self.online = False
        self.apps: list[dict[str, Any]] | None = None
        self.checks: list[dict[str, Any]] | None = None
        self.shown = False            # nothing else is showing: we are
        self._recheck = asyncio.Event()

    def _report(self) -> None:
        self.on_update(self.online, self.apps, self.checks)

    async def run(self) -> None:
        checker = asyncio.create_task(self._check_loop())
        try:
            await self._subscribe_loop()
        finally:
            checker.cancel()

    async def _subscribe_loop(self) -> None:
        delay = RETRY_MIN
        while True:
            try:
                async for reply in Client(self.address).call_more(f"{CTL}.Subscribe"):
                    delay = RETRY_MIN
                    if not self.online:
                        log.info("connected to pi-panel-ctl")
                        self.online = True
                    self._apply(reply.get("event", {}))
            except _UNREACHABLE as exc:
                log.debug("ctl: %s", exc)
            except VarlinkError as exc:
                log.error("ctl refused Subscribe: %s", exc)
            if self.online:
                log.warning("lost pi-panel-ctl; retrying")
                self.online = False
                self._report()
            elif self.apps is None:
                self._report()        # first start: say so rather than show nothing
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if event.get("apps") is not None:
            self.apps = event["apps"]
        self.shown = event.get("state", {}).get("showing") is None
        if kind in ("snapshot", "apps_changed", "config_changed"):
            self._recheck.set()
        self._report()

    async def _check_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._recheck.wait(), RECHECK)
            except asyncio.TimeoutError:
                if not (self.online and self.shown):
                    continue
            self._recheck.clear()
            if not self.online:
                continue
            try:
                reply = await Client(self.address, timeout=CHECK_TIMEOUT).call(f"{CTL}.CheckConfig")
                self.checks = reply.get("results", [])
            except VarlinkError as exc:
                log.warning("CheckConfig: %s", exc)     # e.g. an older ctl without it
                self.checks = None
            except _UNREACHABLE as exc:
                log.debug("CheckConfig: %s", exc)
                continue
            self._report()


class NetThread:
    """One asyncio loop on a daemon thread (as pi-panel-now-playing's)."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="pi-panel-welcome-net", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 2.0) -> None:
        async def cancel_all() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if not self._thread.is_alive():
            return
        try:
            self.submit(cancel_all()).result(timeout)
        except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout)
