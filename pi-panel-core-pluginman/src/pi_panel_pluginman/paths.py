"""The pi-panel file layout, as pluginman sees it.

Installed packages live beside the core, in the same tree the repos are
developed in:

    ~/pi-panel-core/      pi-panel-core-compositor, -ctl, -pluginman (installed by hand)
    ~/pi-panel-apps/      one directory per installed app
    ~/pi-panel-plugin/    one directory per installed plugin

Keeping them siblings lets every package reach the shared libraries with one
relative path (`../../pi-panel-core/pi-panel-core-ctl/packages/...`) that works
on the Pi and on a development machine alike.

Environment overrides (shared with pi-panel-ctl):

    PI_PANEL_HOME          ~                       root of the tree above
    PI_PANEL_CONFIG_HOME   ~/.config/pi-panel      per-package config under apps/ and plugins/
    PI_PANEL_DATA_HOME     ~/.local/share/pi-panel packages.json (the registry)
    PI_PANEL_RUNTIME_DIR   /run/pi-panel           sockets
"""

from __future__ import annotations

import os
from pathlib import Path

KIND_DIRS = {"app": "pi-panel-apps", "plugin": "pi-panel-plugin"}


def home() -> Path:
    return Path(os.environ.get("PI_PANEL_HOME") or Path.home())


def config_home() -> Path:
    return Path(os.environ.get("PI_PANEL_CONFIG_HOME") or Path.home() / ".config" / "pi-panel")


def data_home() -> Path:
    return Path(os.environ.get("PI_PANEL_DATA_HOME") or Path.home() / ".local" / "share" / "pi-panel")


def runtime_dir() -> Path:
    return Path(os.environ.get("PI_PANEL_RUNTIME_DIR") or "/run/pi-panel")


def registry() -> Path:
    return data_home() / "packages.json"


def staging() -> Path:
    return data_home() / "staging"


def package_dir(kind: str, name: str) -> Path:
    return home() / KIND_DIRS[kind] / name


def package_config_dir(kind: str, name: str) -> Path:
    return config_home() / f"{kind}s" / name


def app_socket(name: str) -> Path:
    return runtime_dir() / "apps" / name / "socket"


def ctl_socket() -> Path:
    return runtime_dir() / "ctl" / "io.pipanel.Ctl"
