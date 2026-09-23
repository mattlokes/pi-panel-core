"""Varlink client.

Two layers:

- `Connection` is one open socket. Calls on it run one at a time, in order.
- `Client` is an address. Every `call()` opens a fresh connection and closes it
  afterwards. That makes the client heal itself: a service restarted by
  systemd is simply there again on the next call, with no reconnect logic and
  no stale-socket detection. On a local Unix socket the extra connect() costs
  nothing.

Streaming calls (`more`) keep their connection open for as long as the caller
keeps iterating.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator

from .errors import (
    ProtocolError,
    VarlinkError,
    VarlinkTimeout,
    VarlinkUnavailable,
)
from .protocol import MAX_MESSAGE, encode, read_message, socket_path

DEFAULT_TIMEOUT = 5.0


class Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def open(cls, address: str) -> "Connection":
        try:
            reader, writer = await asyncio.open_unix_connection(
                socket_path(address), limit=MAX_MESSAGE
            )
        except OSError as exc:
            # FileNotFoundError, ConnectionRefusedError, PermissionError, ...
            raise VarlinkUnavailable(f"{address}: {exc}") from exc
        return cls(reader, writer)

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(OSError):
            await self._writer.wait_closed()

    async def __aenter__(self) -> "Connection":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --- sending ---------------------------------------------------------

    async def _send(self, message: dict[str, Any]) -> None:
        try:
            self._writer.write(encode(message))
            await self._writer.drain()
        except OSError as exc:
            raise VarlinkUnavailable(f"send failed: {exc}") from exc

    async def _receive(self, method: str) -> dict[str, Any]:
        try:
            reply = await read_message(self._reader)
        except OSError as exc:
            raise VarlinkUnavailable(f"receive failed during {method}: {exc}") from exc
        if reply is None:
            raise VarlinkUnavailable(f"connection closed during {method}")
        if "error" in reply:
            raise VarlinkError(str(reply["error"]), reply.get("parameters") or {})
        return reply

    # --- calls -----------------------------------------------------------

    async def call(self, method: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """One call, one reply. Raises VarlinkError on an error reply."""
        await self._send({"method": method, "parameters": parameters or {}})
        reply = await self._receive(method)
        if reply.get("continues"):
            raise ProtocolError(f"{method} streamed a reply to a call without 'more'")
        return reply.get("parameters") or {}

    async def call_more(
        self, method: str, parameters: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming call: yields each reply's parameters until the last one."""
        await self._send({"method": method, "parameters": parameters or {}, "more": True})
        while True:
            reply = await self._receive(method)
            yield reply.get("parameters") or {}
            if not reply.get("continues"):
                return

    async def call_oneway(self, method: str, parameters: dict[str, Any] | None = None) -> None:
        """Fire and forget: the server sends no reply."""
        await self._send({"method": method, "parameters": parameters or {}, "oneway": True})


class Client:
    """A Varlink service at one address, with a connection per call."""

    def __init__(self, address: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.address = address
        self.timeout = timeout

    async def _open(self) -> Connection:
        try:
            return await asyncio.wait_for(Connection.open(self.address), self.timeout)
        except asyncio.TimeoutError as exc:
            raise VarlinkTimeout(f"connecting to {self.address} timed out") from exc

    async def call(self, method: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        async def exchange() -> dict[str, Any]:
            async with await Connection.open(self.address) as conn:
                return await conn.call(method, parameters)

        try:
            return await asyncio.wait_for(exchange(), self.timeout)
        except asyncio.TimeoutError as exc:
            raise VarlinkTimeout(f"no reply to {method} within {self.timeout}s") from exc

    async def call_more(
        self, method: str, parameters: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming call. Only the connect is time-limited: a subscription is
        expected to sit idle for as long as nothing changes."""
        conn = await self._open()
        try:
            async for reply in conn.call_more(method, parameters):
                yield reply
        finally:
            await conn.close()

    async def call_oneway(self, method: str, parameters: dict[str, Any] | None = None) -> None:
        conn = await self._open()
        try:
            await conn.call_oneway(method, parameters)
        finally:
            await conn.close()

    # --- org.varlink.service ---------------------------------------------

    async def get_info(self) -> dict[str, Any]:
        return await self.call("org.varlink.service.GetInfo")

    async def get_interface_description(self, interface: str) -> str:
        reply = await self.call(
            "org.varlink.service.GetInterfaceDescription", {"interface": interface}
        )
        return str(reply.get("description", ""))

    async def is_alive(self) -> bool:
        try:
            await self.get_info()
            return True
        except (VarlinkError, VarlinkUnavailable, VarlinkTimeout, ProtocolError):
            return False
