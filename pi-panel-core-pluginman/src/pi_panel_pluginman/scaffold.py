"""`pi-panel-pkg new app|plugin NAME`: a working package to start from.

The app template is the app contract in runnable form: a fullscreen pygame
window on Wayland that exits promptly on SIGTERM, pauses while hidden, and
serves io.pipanel.App with one action.
"""

from __future__ import annotations

from pathlib import Path

from .manifest import _NAME

APP_MANIFEST = '''\
[package]
name = "{name}"
kind = "app"
version = "0.1.0"
description = "A pi-panel app"

[run]
exec = "uv run --script app.py"
env = {{ SDL_APP_ID = "{name}" }}

[app]
varlink = true
'''

APP_SCRIPT = '''\
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pygame>=2.5", "pi-panel-varlink"]
#
# [tool.uv.sources]
# pi-panel-varlink = {{ path = "../../pi-panel-core/pi-panel-core-ctl/packages/pi-panel-varlink" }}
# ///
"""{name}: a pi-panel app.

The contract (see pi-panel-core-pluginman/docs/app-contract.md):
  - one fullscreen Wayland window (the compositor forces fullscreen anyway)
  - exit within 5 seconds of SIGTERM
  - optionally serve io.pipanel.App on $PI_PANEL_APP_SOCKET
"""

import signal
import threading
import time

import pygame
from pi_panel_varlink import AppService

stop = threading.Event()
visible = threading.Event()
visible.set()
colours = [(30, 60, 90), (90, 40, 40), (40, 90, 50)]
colour = [0]


def next_colour() -> None:
    colour[0] = (colour[0] + 1) % len(colours)


def on_visible(is_visible: bool) -> None:
    (visible.set if is_visible else visible.clear)()


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    service = AppService(on_visible=on_visible,
                         actions={{"next": ("Next colour", next_colour)}})
    service.start()   # does nothing when run outside pi-panel

    pygame.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    font = pygame.font.Font(None, screen.get_height() // 6)
    clock = pygame.time.Clock()

    while not stop.is_set():
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                stop.set()
        if not visible.is_set():
            time.sleep(0.2)   # hidden: do nothing expensive
            continue
        screen.fill(colours[colour[0]])
        text = font.render(time.strftime("%H:%M:%S"), True, (255, 255, 255))
        screen.blit(text, text.get_rect(center=screen.get_rect().center))
        pygame.display.flip()
        clock.tick(10)

    service.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
'''

PLUGIN_MANIFEST = '''\
[package]
name = "{name}"
kind = "plugin"
version = "0.1.0"
description = "A pi-panel plugin"

[run]
exec = "uv run --script plugin.py"
'''

PLUGIN_SCRIPT = '''\
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pi-panel-varlink"]
#
# [tool.uv.sources]
# pi-panel-varlink = {{ path = "../../pi-panel-core/pi-panel-core-ctl/packages/pi-panel-varlink" }}
# ///
"""{name}: a pi-panel plugin. Follows the panel's state through io.pipanel.Ctl."""

import asyncio
import os

from pi_panel_varlink import Client, VarlinkUnavailable

CTL = os.environ.get("PI_PANEL_CTL_SOCKET", "/run/pi-panel/ctl/io.pipanel.Ctl")


async def main() -> None:
    while True:
        try:
            async for reply in Client(CTL).call_more("io.pipanel.Ctl.Subscribe"):
                event = reply["event"]
                print(f"{{event['kind']}}: showing {{event['state']['showing']}}", flush=True)
        except VarlinkUnavailable:
            pass
        await asyncio.sleep(2)   # ctl restarted or not up yet


if __name__ == "__main__":
    asyncio.run(main())
'''

GITIGNORE = "__pycache__/\n*.pyc\n.venv/\n"

README = '''\
# {name}

A pi-panel {kind}.

```bash
pi-panel-pkg install {path}      # or a git URL once it is pushed
pi-panel-ctl enable {name}
```
'''


def create(kind: str, name: str, parent: Path) -> Path:
    if not _NAME.match(name):
        raise ValueError(f"{name!r}: use lowercase letters, digits, '-' or '_'")
    target = parent / f"pi-panel-{'app' if kind == 'app' else 'plugin'}-{name}"
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    target.mkdir(parents=True)
    fmt = {"name": name, "kind": kind, "path": str(target)}
    if kind == "app":
        (target / "pi-panel.toml").write_text(APP_MANIFEST.format(**fmt))
        script = target / "app.py"
        script.write_text(APP_SCRIPT.format(**fmt))
    else:
        (target / "pi-panel.toml").write_text(PLUGIN_MANIFEST.format(**fmt))
        script = target / "plugin.py"
        script.write_text(PLUGIN_SCRIPT.format(**fmt))
    script.chmod(0o755)
    (target / ".gitignore").write_text(GITIGNORE)
    (target / "README.md").write_text(README.format(**fmt))
    return target
