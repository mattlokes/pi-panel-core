"""pi-panel-welcome: the screen pi-panel shows when no app is.

Run by pi-panel-welcome.service (started by pi-panel-ctl, in the slot
"welcome"). It reads facts about the device itself (info.py) and follows ctl
for the rest (ctlclient.py); status.py turns that into what to say.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from . import VERSION, info, status
from .ctlclient import CtlWatcher, NetThread

log = logging.getLogger("pi-panel-welcome")

INFO_REFRESH = 30   # seconds: addresses come and go (DHCP, Wi-Fi)

DEMO_APPS = [
    {"name": "immich", "kind": "app", "enabled": True, "unit_state": "failed", "mapped": False},
    {"name": "now-playing", "kind": "app", "enabled": True, "unit_state": "activating",
     "mapped": False},
    {"name": "webrtc-client", "kind": "app", "enabled": False, "unit_state": "inactive",
     "mapped": False},
]
DEMO_CHECKS = [
    {"name": "panel.toml", "status": "ok", "detail": ""},
    {"name": "immich", "status": "failed",
     "detail": "config error: /persistent/pi-panel/config/apps/immich/config.toml: "
               "server.url must start with http:// or https://"},
    {"name": "now-playing", "status": "warning", "detail": "enabled but not installed"},
]


def parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = (int(v) for v in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("expected WxH, e.g. 1920x1080") from None
    return w, h


def main() -> int:
    p = argparse.ArgumentParser(prog="pi-panel-welcome")
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--log-level", default="INFO")
    dev = p.add_argument_group("for working on the screen")
    dev.add_argument("--demo", action="store_true",
                     help="pretend apps and config errors instead of asking pi-panel-ctl")
    dev.add_argument("--size", type=parse_size, metavar="WxH",
                     help="lay out for this screen size instead of asking the display")
    dev.add_argument("--screenshot", type=Path, metavar="PNG",
                     help="save the window as a PNG after a few seconds, and exit")
    args = p.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(levelname)-7s %(message)s", stream=sys.stderr)
    # The compositor's Wayland socket is in the panel user's runtime dir.
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

    from . import ui  # noqa: F401 - pins the GTK versions before anything imports them
    from gi.repository import GLib, Gtk

    from .ui.window import WelcomeWindow, screen_size

    GLib.set_prgname("welcome")
    if not Gtk.init_check():
        print("cannot open the display (is WAYLAND_DISPLAY set?)", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    window = WelcomeWindow(args.size or screen_size())
    current: dict[str, Any] = {"info": info.gather(), "ctl": (False, None, None)}

    def render() -> None:
        online, apps, checks = current["ctl"]
        first = current["info"].addresses[0].address if current["info"].addresses else None
        window.set_info(current["info"])
        window.set_status(status.summarize(ctl_online=online, apps=apps, checks=checks,
                                           user=current["info"].user, address=first))

    def refresh_info() -> bool:
        current["info"] = info.gather()
        render()
        return True

    def on_ctl(online: bool, apps: Any, checks: Any) -> None:
        def apply() -> bool:
            current["ctl"] = (online, apps, checks)
            render()
            return False
        GLib.idle_add(apply)

    net: NetThread | None = None
    if args.demo:
        current["ctl"] = (True, DEMO_APPS, DEMO_CHECKS)
    else:
        net = NetThread()
        net.start()
        net.submit(CtlWatcher(on_ctl).run())
    render()
    GLib.timeout_add_seconds(INFO_REFRESH, refresh_info)

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda: loop.quit() or False)
    window.connect("close-request", lambda _w: loop.quit() or False)
    window.present()

    if args.screenshot:
        from .ui.screenshot import save
        GLib.timeout_add(3000, lambda: (save(window, args.screenshot), loop.quit(), False)[-1])

    loop.run()
    log.info("stopping")
    if net:
        net.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
