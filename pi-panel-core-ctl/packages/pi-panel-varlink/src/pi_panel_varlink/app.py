"""io.pipanel.App for apps that have their own main loop.

Most apps are a blocking render loop (pygame, SDL, a GUI toolkit), not
asyncio. `AppService` runs the Varlink server on a background thread with its
own event loop, and calls back into the app:

    service = AppService(
        product="immich",
        on_visible=lambda visible: state.set_visible(visible),
        actions={"next": ("Next photo", state.request_next)},
    )
    service.start()        # no-op (returns False) outside pi-panel

Callbacks run on the service thread. Keep them tiny and thread-safe: set a
flag or post to a queue that the main loop reads, as a signal handler would.

An app can serve more of its own interfaces beside io.pipanel.App by passing
`interfaces=[Interface(...)]`; pi-panel-ctl lists them and `pi-panel-ctl app
<name> <Method>` calls them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Callable, Iterable

from .errors import VarlinkError
from .interfaces import load as load_interface
from .server import Call, Interface, Server

log = logging.getLogger("pi_panel_varlink")

ActionHandler = Callable[[], None]


class AppService:
    def __init__(
        self,
        *,
        product: str | None = None,
        version: str = "0",
        on_visible: Callable[[bool], None] | None = None,
        actions: dict[str, tuple[str, ActionHandler]] | None = None,
        interfaces: Iterable[Interface] = (),
        address: str | None = None,
    ) -> None:
        self.address = address or os.environ.get("PI_PANEL_APP_SOCKET")
        self.product = product or os.environ.get("PI_PANEL_NAME", "pi-panel-app")
        self.version = version
        self.on_visible = on_visible
        self.actions = dict(actions or {})
        self.extra = list(interfaces)
        self.visible: bool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Server | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def _interface(self) -> Interface:
        iface = Interface(load_interface("io.pipanel.App"))

        @iface.method("SetVisible")
        async def set_visible(call: Call) -> None:
            visible = call.param("visible", bool)
            self.visible = visible
            if self.on_visible:
                self.on_visible(visible)

        @iface.method("ListActions")
        async def list_actions(call: Call) -> dict:
            return {"actions": [{"name": n, "label": label}
                                for n, (label, _) in self.actions.items()]}

        @iface.method("InvokeAction")
        async def invoke_action(call: Call) -> None:
            name = call.param("name", str)
            if name not in self.actions:
                raise VarlinkError("io.pipanel.App.NoSuchAction", {"name": name})
            self.actions[name][1]()

        return iface

    def start(self) -> bool:
        """Serve in the background. False (and nothing started) when the app
        is not running under pi-panel, i.e. there is no socket to serve on."""
        if not self.address:
            return False
        self._thread = threading.Thread(target=self._run, name="pi-panel-app-service", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self._error:
            raise self._error
        return True

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._serve())
            self._loop.run_forever()
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        try:
            self._server = Server(product=self.product, version=self.version)
            self._server.add_interface(self._interface())
            for iface in self.extra:
                self._server.add_interface(iface)
            await self._server.start(self.address)  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self._error = exc
            log.error("cannot serve %s: %s", self.address, exc)
        finally:
            self._ready.set()

    def stop(self) -> None:
        if self._loop is None or self._server is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._server.close(), self._loop)
        try:
            future.result(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2)
