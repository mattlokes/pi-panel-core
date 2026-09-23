"""ctl's side of io.pipanel.Compositor: a mirrored view kept current by Subscribe.

The compositor is ctl's only client-facing display, and it can restart at any
moment (systemd restarts it on a crash). This link:

- holds one Subscribe call open, and mirrors the slots and status it reports;
- on every (re)connect re-registers the slots ctl wants, because a restarted
  compositor starts with none;
- reports "offline" while it cannot connect, and retries with backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Callable

from pi_panel_varlink import Client, ProtocolError, VarlinkError, VarlinkTimeout, VarlinkUnavailable

log = logging.getLogger("pi-panel-ctl")

IFACE = "io.pipanel.Compositor"
RETRY_MIN = 0.5
RETRY_MAX = 10.0

_LINK_ERRORS = (VarlinkUnavailable, VarlinkTimeout, ProtocolError, OSError)


@dataclass(frozen=True, slots=True)
class SlotWish:
    match_unit: str | None = None
    match_app_id: str | None = None


class CompositorLink:
    def __init__(self, address: str, on_change: Callable[[], None]) -> None:
        self.client = Client(address, timeout=5.0)
        self.on_change = on_change
        self.online = False
        self.slots: dict[str, dict[str, Any]] = {}
        self.status: dict[str, Any] = {}
        self._wishes: dict[str, SlotWish] = {}

    # --- mirrored state --------------------------------------------------

    @property
    def active(self) -> str | None:
        return self.status.get("active") if self.online else None

    @property
    def transitioning(self) -> bool:
        return bool(self.status.get("transitioning"))

    @property
    def output_power(self) -> bool:
        return bool(self.status.get("output_power", True))

    def mapped(self) -> set[str]:
        """Registered slots that currently have a mapped window."""
        return {n for n, s in self.slots.items() if s.get("registered") and s.get("mapped")}

    # --- subscription loop -----------------------------------------------

    async def run(self) -> None:
        delay = RETRY_MIN
        while True:
            try:
                async for reply in self.client.call_more(f"{IFACE}.Subscribe"):
                    self._apply(reply["event"])
                    delay = RETRY_MIN
            except _LINK_ERRORS as exc:
                log.debug("compositor link: %s", exc)
            except VarlinkError as exc:
                log.error("compositor refused Subscribe: %s", exc)
            if self.online:
                log.warning("compositor went offline")
                self.online = False
                self.slots.clear()
                self.status = {}
                self.on_change()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    def _apply(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        self.status = event.get("status") or {}
        if kind == "snapshot":
            self.slots = {s["name"]: s for s in event.get("slots") or []}
            if not self.online:
                log.info("compositor online (%d slots)", len(self.slots))
            self.online = True
            # A fresh (or restarted) compositor knows nothing of our slots.
            asyncio.get_running_loop().create_task(self._register_all())
        elif kind == "slot_removed":
            slot = event.get("slot") or {}
            self.slots.pop(slot.get("name"), None)
        elif event.get("slot"):
            slot = event["slot"]
            self.slots[slot["name"]] = slot
        self.on_change()

    async def _register_all(self) -> None:
        for name, wish in list(self._wishes.items()):
            with contextlib.suppress(*_LINK_ERRORS):
                await self._register(name, wish)

    # --- commands --------------------------------------------------------

    async def _register(self, name: str, wish: SlotWish) -> None:
        params: dict[str, Any] = {"name": name}
        if wish.match_unit:
            params["match_unit"] = wish.match_unit
        if wish.match_app_id:
            params["match_app_id"] = wish.match_app_id
        await self.client.call(f"{IFACE}.RegisterSlot", params)

    async def register(self, name: str, match_unit: str | None = None,
                       match_app_id: str | None = None) -> None:
        """Remember the slot, and register it now if the compositor is up."""
        wish = SlotWish(match_unit, match_app_id)
        self._wishes[name] = wish
        if self.online:
            with contextlib.suppress(*_LINK_ERRORS):
                await self._register(name, wish)

    async def unregister(self, name: str) -> None:
        self._wishes.pop(name, None)
        if self.online and name in self.slots:
            with contextlib.suppress(*_LINK_ERRORS, VarlinkError):
                await self.client.call(f"{IFACE}.UnregisterSlot", {"name": name})

    def wanted(self) -> set[str]:
        return set(self._wishes)

    async def switch(self, name: str, transition: str = "fade") -> bool:
        try:
            await self.client.call(f"{IFACE}.Switch", {"slot": name, "transition": transition})
            return True
        except VarlinkError as exc:
            log.debug("switch to %s refused: %s", name, exc)
        except _LINK_ERRORS as exc:
            log.debug("switch to %s failed: %s", name, exc)
        return False

    async def set_power(self, on: bool) -> bool:
        try:
            await self.client.call(f"{IFACE}.SetOutputPower", {"on": on})
            return True
        except (VarlinkError, *_LINK_ERRORS) as exc:
            log.warning("output power %s failed: %s", "on" if on else "off", exc)
            return False
