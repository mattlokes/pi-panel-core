"""Client/server round trips, plus interop with systemd's own varlinkctl."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile

import pytest

from pi_panel_varlink import (
    Broadcaster,
    Client,
    Connection,
    Interface,
    Server,
    SubscriberOverflow,
    VarlinkError,
    VarlinkUnavailable,
)
from pi_panel_varlink.errors import (
    EXPECTED_MORE,
    INTERFACE_NOT_FOUND,
    INTERNAL_ERROR,
    INVALID_PARAMETER,
    METHOD_NOT_FOUND,
)
from pi_panel_varlink.protocol import encode

DESCRIPTION = """\
# A test interface.
interface io.pipanel.Test

method Echo(text: string) -> (text: string)
method Fail() -> ()
method Crash() -> ()
method Count(n: int) -> (i: int)
method Subscribe() -> (event: string)
error Nope (why: string)
"""


@pytest.fixture
def sock_dir():
    # AF_UNIX paths are capped near 108 bytes; pytest's tmp_path can be longer.
    d = tempfile.mkdtemp(prefix="pv", dir="/tmp")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
async def service(sock_dir):
    events = Broadcaster(maxsize=4)
    iface = Interface(DESCRIPTION)

    @iface.method("Echo")
    async def echo(call):
        return {"text": call.param("text", str)}

    @iface.method("Fail")
    async def fail(call):
        raise VarlinkError("io.pipanel.Test.Nope", {"why": "because"})

    @iface.method("Crash")
    async def crash(call):
        raise RuntimeError("boom")

    @iface.method("Count")
    async def count(call):
        call.require_more()
        n = call.param("n", int)
        for i in range(n - 1):
            await call.reply({"i": i})
        return {"i": n - 1}

    @iface.method("Subscribe")
    async def subscribe(call):
        call.require_more()
        with events.subscribe() as sub:
            await call.reply({"event": "snapshot"})
            async for event in sub:
                await call.reply({"event": event})

    server = Server(product="test", version="1.2.3", url="https://example.invalid")
    server.add_interface(iface)
    path = os.path.join(sock_dir, "io.pipanel.Test")
    await server.start(path)
    yield path, events
    await server.close()


async def test_call_and_reply(service):
    path, _ = service
    assert await Client(path).call("io.pipanel.Test.Echo", {"text": "hi"}) == {"text": "hi"}


async def test_unix_address_form(service):
    path, _ = service
    reply = await Client(f"unix:{path}").call("io.pipanel.Test.Echo", {"text": "x"})
    assert reply == {"text": "x"}


async def test_error_reply_raises(service):
    path, _ = service
    with pytest.raises(VarlinkError) as exc:
        await Client(path).call("io.pipanel.Test.Fail")
    assert exc.value.error == "io.pipanel.Test.Nope"
    assert exc.value.parameters == {"why": "because"}


async def test_missing_parameter_is_invalid_parameter(service):
    path, _ = service
    with pytest.raises(VarlinkError) as exc:
        await Client(path).call("io.pipanel.Test.Echo")
    assert exc.value.error == INVALID_PARAMETER
    assert exc.value.parameters == {"parameter": "text"}


async def test_handler_crash_is_reported_and_server_survives(service):
    path, _ = service
    with pytest.raises(VarlinkError) as exc:
        await Client(path).call("io.pipanel.Test.Crash")
    assert exc.value.error == INTERNAL_ERROR
    assert "boom" in exc.value.parameters["message"]
    assert await Client(path).call("io.pipanel.Test.Echo", {"text": "ok"}) == {"text": "ok"}


async def test_unknown_interface_and_method(service):
    path, _ = service
    with pytest.raises(VarlinkError) as exc:
        await Client(path).call("io.pipanel.Nope.X")
    assert exc.value.error == INTERFACE_NOT_FOUND
    with pytest.raises(VarlinkError) as exc:
        await Client(path).call("io.pipanel.Test.Nope")
    assert exc.value.error == METHOD_NOT_FOUND


async def test_streaming(service):
    path, _ = service
    got = [r["i"] async for r in Client(path).call_more("io.pipanel.Test.Count", {"n": 3})]
    assert got == [0, 1, 2]


async def test_streaming_method_without_more(service):
    path, _ = service
    with pytest.raises(VarlinkError) as exc:
        await Client(path).call("io.pipanel.Test.Count", {"n": 2})
    assert exc.value.error == EXPECTED_MORE


async def test_several_calls_on_one_connection(service):
    path, _ = service
    async with await Connection.open(path) as conn:
        assert (await conn.call("io.pipanel.Test.Echo", {"text": "a"}))["text"] == "a"
        assert (await conn.call("io.pipanel.Test.Echo", {"text": "b"}))["text"] == "b"


async def test_oneway_gets_no_reply(service):
    path, _ = service
    async with await Connection.open(path) as conn:
        await conn.call_oneway("io.pipanel.Test.Echo", {"text": "ignored"})
        # The next reply on the connection belongs to the next call.
        assert (await conn.call("io.pipanel.Test.Echo", {"text": "b"}))["text"] == "b"


async def test_subscription_delivers_events(service):
    path, events = service
    stream = Client(path).call_more("io.pipanel.Test.Subscribe")
    assert (await anext(stream))["event"] == "snapshot"
    events.publish("one")
    events.publish("two")
    assert (await anext(stream))["event"] == "one"
    assert (await anext(stream))["event"] == "two"
    await stream.aclose()


async def test_subscriber_hangup_is_noticed_while_idle(service):
    path, events = service
    stream = Client(path).call_more("io.pipanel.Test.Subscribe")
    await anext(stream)
    assert len(events) == 1
    await stream.aclose()
    for _ in range(50):
        if len(events) == 0:
            break
        await asyncio.sleep(0.01)
    assert len(events) == 0, "server kept an idle dead subscriber"


async def test_slow_subscriber_is_dropped_not_blocking():
    events = Broadcaster(maxsize=2)
    with events.subscribe() as sub:
        for i in range(10):
            events.publish(i)  # must never block or raise
        with pytest.raises(SubscriberOverflow):
            async for _ in sub:
                pass


async def test_get_info_and_description(service):
    path, _ = service
    client = Client(path)
    info = await client.get_info()
    assert info["product"] == "test" and info["version"] == "1.2.3"
    assert info["interfaces"] == ["io.pipanel.Test", "org.varlink.service"]
    assert await client.get_interface_description("io.pipanel.Test") == DESCRIPTION


async def test_garbage_drops_only_that_client(service):
    path, _ = service
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b"this is not json\0")
    await writer.drain()
    assert await reader.read() == b""  # closed on us
    writer.close()
    assert await Client(path).call("io.pipanel.Test.Echo", {"text": "ok"}) == {"text": "ok"}


async def test_absent_socket_is_unavailable(sock_dir):
    client = Client(os.path.join(sock_dir, "absent"))
    with pytest.raises(VarlinkUnavailable):
        await client.call("io.pipanel.Test.Echo", {"text": "x"})
    assert await client.is_alive() is False


async def test_stale_socket_file_is_replaced(sock_dir):
    path = os.path.join(sock_dir, "s")
    first = Server(product="a", version="1")
    await first.start(path)
    # Simulate a crash: the file stays behind but nobody listens.
    first._server.close()
    await first._server.wait_closed()
    second = Server(product="b", version="1")
    await second.start(path)
    assert (await Client(path).get_info())["product"] == "b"
    await second.close()


async def test_live_socket_is_not_stolen(service):
    path, _ = service
    with pytest.raises(RuntimeError, match="in use"):
        await Server(product="x", version="1").start(path)


# --- interop with systemd's varlinkctl -----------------------------------

varlinkctl = shutil.which("varlinkctl")
needs_varlinkctl = pytest.mark.skipif(varlinkctl is None, reason="varlinkctl not installed")


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(proc.communicate(), 10)
    return proc.returncode, out.decode(), err.decode()


@needs_varlinkctl
async def test_varlinkctl_info(service):
    path, _ = service
    rc, out, err = await _run(varlinkctl, "info", path)
    assert rc == 0, err
    assert "io.pipanel.Test" in out


@needs_varlinkctl
async def test_varlinkctl_introspect_parses_our_description(service):
    path, _ = service
    rc, out, err = await _run(varlinkctl, "introspect", path, "io.pipanel.Test")
    assert rc == 0, err
    assert "method Echo" in out


@needs_varlinkctl
async def test_varlinkctl_call(service):
    path, _ = service
    rc, out, err = await _run(
        varlinkctl, "call", "-j", path, "io.pipanel.Test.Echo", json.dumps({"text": "hey"})
    )
    assert rc == 0, err
    assert json.loads(out) == {"text": "hey"}


@needs_varlinkctl
async def test_varlinkctl_call_more(service):
    path, _ = service
    rc, out, err = await _run(
        varlinkctl, "call", "--more", "-j", path, "io.pipanel.Test.Count", '{"n": 3}'
    )
    assert rc == 0, err
    assert [json.loads(line)["i"] for line in out.split() if line.strip()] == [0, 1, 2]


def test_encode_is_nul_terminated():
    assert encode({"a": 1}) == b'{"a":1}\x00'


# --- AppService ------------------------------------------------------------

def test_app_service_serves_io_pipanel_app(sock_dir):
    """Runs on its own thread, so it works from a plain (non-async) app."""
    from pi_panel_varlink import AppService

    seen = []
    nexts = []
    path = os.path.join(sock_dir, "app")
    service = AppService(product="demo", address=path, on_visible=seen.append,
                         actions={"next": ("Next photo", lambda: nexts.append(1))})
    assert service.start()

    async def drive():
        c = Client(path)
        await c.call("io.pipanel.App.SetVisible", {"visible": True})
        actions = (await c.call("io.pipanel.App.ListActions"))["actions"]
        await c.call("io.pipanel.App.InvokeAction", {"name": "next"})
        with pytest.raises(VarlinkError) as exc:
            await c.call("io.pipanel.App.InvokeAction", {"name": "nope"})
        return actions, exc.value.error, (await c.get_info())["interfaces"]

    try:
        actions, err, ifaces = asyncio.run(drive())
    finally:
        service.stop()
    assert seen == [True] and nexts == [1]
    assert actions == [{"name": "next", "label": "Next photo"}]
    assert err == "io.pipanel.App.NoSuchAction"
    assert ifaces == ["io.pipanel.App", "org.varlink.service"]


def test_app_service_is_a_noop_outside_pi_panel(monkeypatch):
    from pi_panel_varlink import AppService

    monkeypatch.delenv("PI_PANEL_APP_SOCKET", raising=False)
    assert AppService(product="x").start() is False
