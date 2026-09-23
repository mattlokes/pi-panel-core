"""Varlink server.

A handler is `async def handler(call: Call) -> dict | None`. Whatever it
returns becomes the reply's parameters. Raising `VarlinkError` sends an error
reply instead.

A streaming handler (for calls made with `more`) sends intermediate replies
with `await call.reply({...})` and then returns the final one:

    @iface.method("Subscribe")
    async def subscribe(call):
        call.require_more()
        with events.subscribe() as sub:          # subscribe *before* the snapshot,
            await call.reply({"event": snap()})  # so nothing falls in between
            async for event in sub:
                await call.reply({"event": event})

If the client hangs up while a streaming handler is waiting, the handler is
cancelled, so it must not rely on anything after its loop running.

`org.varlink.service` (GetInfo, GetInterfaceDescription) is built in, which is
what makes `varlinkctl info` and `varlinkctl introspect` work.

Parameters are not checked against the interface description: handlers read
them with `call.param()`, which raises InvalidParameter on a missing or
mistyped value.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import stat
from typing import Any, Awaitable, Callable, Iterator

from .errors import (
    EXPECTED_MORE,
    INTERFACE_NOT_FOUND,
    INTERNAL_ERROR,
    METHOD_NOT_FOUND,
    ProtocolError,
    VarlinkError,
)
from .protocol import MAX_MESSAGE, encode, read_message, socket_path

log = logging.getLogger("pi_panel_varlink")

Handler = Callable[["Call"], Awaitable[dict[str, Any] | None]]

_MISSING = object()

# The standard service interface, served verbatim by GetInterfaceDescription.
SERVICE_INTERFACE = """\
# The Varlink Service Interface is provided by every varlink service. It
# describes the service and the interfaces it implements.
interface org.varlink.service

# Get a list of all the interfaces a service provides and information
# about the implementation.
method GetInfo() -> (
  vendor: string,
  product: string,
  version: string,
  url: string,
  interfaces: []string
)

# Get the description of an interface that is implemented by this service.
method GetInterfaceDescription(interface: string) -> (description: string)

# The requested interface was not found.
error InterfaceNotFound (interface: string)

# The requested method was not found
error MethodNotFound (method: string)

# The interface defines the requested method, but the service does not
# implement it.
error MethodNotImplemented (method: string)

# One of the passed parameters is invalid.
error InvalidParameter (parameter: string)

# Client is denied access
error PermissionDenied ()

# Method is expected to be called with 'more' set to true, but wasn't
error ExpectedMore ()
"""


def interface_name(description: str) -> str:
    """The name from an interface description's `interface <name>` line."""
    for line in description.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, _, rest = stripped.partition(" ")
        if keyword == "interface" and rest.strip():
            return rest.strip()
        break
    raise ValueError("interface description does not start with 'interface <name>'")


class Interface:
    """One interface: its description text and its method handlers."""

    def __init__(self, description: str) -> None:
        self.description = description
        self.name = interface_name(description)
        self.methods: dict[str, Handler] = {}

    def method(self, name: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.methods[name] = handler
            return handler

        return register

    def add_method(self, name: str, handler: Handler) -> None:
        self.methods[name] = handler


class Call:
    """One incoming method call, handed to the handler."""

    def __init__(
        self,
        method: str,
        parameters: dict[str, Any],
        more: bool,
        oneway: bool,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.method = method
        self.parameters = parameters
        self.more = more
        self.oneway = oneway
        self._writer = writer

    def param(self, name: str, kind: type | tuple[type, ...] | None = None, default: Any = _MISSING) -> Any:
        """Read one parameter.

        Missing and no default, or present but not `kind`, raises
        InvalidParameter. A JSON null counts as missing. Note `bool` is a
        subclass of `int`, so ask for `(int, float)` knowing that True passes.
        """
        value = self.parameters.get(name)
        if value is None:
            if default is _MISSING:
                raise VarlinkError.invalid_parameter(name)
            return default
        if kind is not None and not isinstance(value, kind):
            raise VarlinkError.invalid_parameter(name)
        return value

    def require_more(self) -> None:
        if not self.more:
            raise VarlinkError(EXPECTED_MORE)

    async def reply(self, parameters: dict[str, Any] | None = None) -> None:
        """Send an intermediate reply (`continues: true`) on a streaming call."""
        if not self.more:
            raise VarlinkError(EXPECTED_MORE)
        await _write(self._writer, {"parameters": parameters or {}, "continues": True})


async def _write(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    writer.write(encode(message))
    await writer.drain()


class Server:
    def __init__(
        self,
        *,
        vendor: str = "pi-panel",
        product: str,
        version: str,
        url: str = "",
    ) -> None:
        self.info = {"vendor": vendor, "product": product, "version": version, "url": url}
        self._interfaces: dict[str, Interface] = {}
        self._server: asyncio.AbstractServer | None = None
        self._path: str | None = None
        self._connections: set[asyncio.Task[None]] = set()

        service = Interface(SERVICE_INTERFACE)
        service.add_method("GetInfo", self._get_info)
        service.add_method("GetInterfaceDescription", self._get_interface_description)
        self.add_interface(service)

    def add_interface(self, interface: Interface) -> None:
        self._interfaces[interface.name] = interface

    # --- org.varlink.service ---------------------------------------------

    async def _get_info(self, call: Call) -> dict[str, Any]:
        return {**self.info, "interfaces": sorted(self._interfaces)}

    async def _get_interface_description(self, call: Call) -> dict[str, Any]:
        name = call.param("interface", str)
        iface = self._interfaces.get(name)
        if iface is None:
            raise VarlinkError(INTERFACE_NOT_FOUND, {"interface": name})
        return {"description": iface.description}

    # --- lifecycle -------------------------------------------------------

    async def start(self, address: str, mode: int = 0o660) -> None:
        path = socket_path(address)
        if not path.startswith("\0"):
            _clear_stale_socket(path)
        self._server = await asyncio.start_unix_server(
            self._on_connection, path, limit=MAX_MESSAGE
        )
        self._path = path
        if not path.startswith("\0"):
            os.chmod(path, mode)
        log.info("varlink: listening on %s", address)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._connections):
            task.cancel()
        for task in list(self._connections):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self._path and not self._path.startswith("\0"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._path)
        self._path = None

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("start() the server first")
        await self._server.serve_forever()

    # --- connections -----------------------------------------------------

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._connections.add(task)
        try:
            pending: dict[str, Any] | None = None
            while True:
                message = pending if pending is not None else await read_message(reader)
                pending = None
                if message is None:
                    return
                pending, keep_open = await self._dispatch(message, reader, writer)
                if not keep_open:
                    return
        except ProtocolError as exc:
            log.warning("varlink: dropping client: %s", exc)
        except (ConnectionError, OSError):
            pass  # the client went away; nothing to tell anyone
        finally:
            self._connections.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(
        self,
        message: dict[str, Any],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Run one call. Returns (a pipelined message read meanwhile, keep_open)."""
        method = message.get("method")
        if not isinstance(method, str) or "." not in method:
            raise ProtocolError(f"call has no qualified method: {message!r}")
        parameters = message.get("parameters") or {}
        if not isinstance(parameters, dict):
            raise ProtocolError("call parameters are not an object")
        call = Call(
            method,
            parameters,
            more=bool(message.get("more")),
            oneway=bool(message.get("oneway")),
            writer=writer,
        )

        iface_name, _, method_name = method.rpartition(".")
        iface = self._interfaces.get(iface_name)
        handler = iface.methods.get(method_name) if iface else None
        if handler is None:
            if not call.oneway:
                if iface is None:
                    error = VarlinkError(INTERFACE_NOT_FOUND, {"interface": iface_name})
                else:
                    error = VarlinkError(METHOD_NOT_FOUND, {"method": method})
                await _write(writer, {"error": error.error, "parameters": error.parameters})
            return None, True

        if not call.more:
            await self._invoke(call, handler)
            return None, True
        return await self._invoke_streaming(call, handler, reader)

    async def _invoke(self, call: Call, handler: Handler) -> None:
        try:
            result = await handler(call)
        except VarlinkError as exc:
            if not call.oneway:
                await _write(call._writer, {"error": exc.error, "parameters": exc.parameters})
            return
        except (ConnectionError, OSError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 - one bad call must not kill the server
            log.exception("varlink: %s failed", call.method)
            if not call.oneway:
                await _write(
                    call._writer,
                    {"error": INTERNAL_ERROR, "parameters": {"message": str(exc)}},
                )
            return
        if not call.oneway:
            await _write(call._writer, {"parameters": result or {}})

    async def _invoke_streaming(
        self, call: Call, handler: Handler, reader: asyncio.StreamReader
    ) -> tuple[dict[str, Any] | None, bool]:
        """Run a `more` call while watching for the client hanging up.

        A subscription can sit idle indefinitely, so a vanished client would
        otherwise only be noticed at the next write. Reading concurrently
        catches the EOF straight away. If the client instead pipelines another
        call, that message is handed back to be run once this one finishes.
        """
        work = asyncio.create_task(self._invoke(call, handler))
        watch = asyncio.create_task(read_message(reader))
        try:
            done, _ = await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                watch.cancel()
                with contextlib.suppress(asyncio.CancelledError, ProtocolError):
                    await watch
                work.result()
                return None, True

            try:
                pipelined = watch.result()
            except ProtocolError:
                pipelined = None
            if pipelined is None:
                # EOF: the subscriber is gone. Stop its handler.
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError, OSError):
                    await work
                return None, False
            await work
            return pipelined, True
        finally:
            for task in (work, watch):
                if not task.done():
                    task.cancel()


def _clear_stale_socket(path: str) -> None:
    """Remove a leftover socket file, but refuse to steal a live one."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(f"{path} exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
    except OSError:
        os.unlink(path)  # nobody listening: stale
        return
    finally:
        probe.close()
    raise RuntimeError(f"{path} is in use by another process")


class SubscriberOverflow(Exception):
    """A subscriber fell too far behind and was dropped."""


class Subscription:
    def __init__(self, broadcaster: "Broadcaster", maxsize: int) -> None:
        self._broadcaster = broadcaster
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize)
        self.overflowed = False

    def _offer(self, event: Any) -> None:
        if self.overflowed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Never block the publisher for one slow reader: drop *this*
            # subscriber instead, and let it resubscribe for a fresh snapshot.
            # Make room for the marker so the reader learns it was dropped.
            self.overflowed = True
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._queue.put_nowait(_OVERFLOW)

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> Any:
        event = await self._queue.get()
        if event is _OVERFLOW:
            raise SubscriberOverflow("subscriber fell behind and was dropped")
        return event

    def close(self) -> None:
        self._broadcaster._subscribers.discard(self)


_OVERFLOW = object()


class Broadcaster:
    """Fan one event stream out to any number of streaming handlers."""

    def __init__(self, maxsize: int = 256) -> None:
        self._subscribers: set[Subscription] = set()
        self._maxsize = maxsize

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[Subscription]:
        sub = Subscription(self, self._maxsize)
        self._subscribers.add(sub)
        try:
            yield sub
        finally:
            sub.close()

    def publish(self, event: Any) -> None:
        for sub in list(self._subscribers):
            sub._offer(event)

    def __len__(self) -> int:
        return len(self._subscribers)
