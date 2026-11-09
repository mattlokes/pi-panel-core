"""CtlWatcher against a small in-process io.pipanel.Ctl."""

import asyncio

from pi_panel_varlink import Broadcaster, Interface, Server

from pi_panel_welcome import ctlclient

DESCRIPTION = """\
interface io.pipanel.Ctl
method Subscribe() -> (event: object)
method CheckConfig() -> (results: []object)
"""


class FakeCtl:
    def __init__(self, path):
        self.path = path
        self.events = Broadcaster()
        self.checks = 0
        self.state = {"showing": None}
        self.apps = [{"name": "immich", "kind": "app", "enabled": False}]

    async def start(self):
        iface = Interface(DESCRIPTION)
        iface.add_method("Subscribe", self._subscribe)
        iface.add_method("CheckConfig", self._check)
        self.server = Server(product="fake-ctl", version="0")
        self.server.add_interface(iface)
        await self.server.start(self.path)

    async def _subscribe(self, call):
        call.require_more()
        with self.events.subscribe() as sub:
            await call.reply({"event": {"kind": "snapshot", "state": self.state,
                                        "apps": self.apps}})
            async for event in sub:
                await call.reply({"event": event})

    async def _check(self, call):
        self.checks += 1
        return {"results": [{"name": "immich", "status": "failed", "detail": "bad"}]}


async def until(predicate, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


def test_follows_ctl_and_checks_config(tmp_path):
    async def scenario():
        updates = []
        fake = FakeCtl(str(tmp_path / "ctl"))
        watcher = ctlclient.CtlWatcher(lambda *u: updates.append(u), fake.path)
        task = asyncio.create_task(watcher.run())
        await until(lambda: updates)                     # ctl not there yet
        assert updates[-1] == (False, None, None)
        await fake.start()
        await until(lambda: updates[-1][2] is not None)
        online, apps, checks = updates[-1]
        assert online and apps[0]["name"] == "immich" and checks[0]["detail"] == "bad"

        fake.events.publish({"kind": "apps_changed", "state": {"showing": "immich"},
                             "apps": [{"name": "immich", "kind": "app", "enabled": True}]})
        await until(lambda: updates[-1][1][0]["enabled"] is True)
        assert watcher.shown is False
        await until(lambda: fake.checks == 2)             # an apps change re-checks

        await fake.server.close()
        await until(lambda: updates[-1][0] is False)
        task.cancel()
    asyncio.run(scenario())
