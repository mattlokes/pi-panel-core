"""Where pi-panel keeps things. The same layout is used by pluginman.

Each root can be moved with an environment variable, which is how development
and the tests run a complete panel as an unprivileged user:

    PI_PANEL_CONFIG_HOME   ~/.config/pi-panel          ctl.toml, per-package config
    PI_PANEL_DATA_HOME     ~/.local/share/pi-panel     installed packages + registry
    PI_PANEL_RUNTIME_DIR   /run/pi-panel               Varlink sockets

(Deliberately not PI_PANEL_CONFIG_DIR: pi-panel-run gives each package that
variable, pointing at the package's *own* config directory.)
"""

from __future__ import annotations

import os
from pathlib import Path


def config_home() -> Path:
    return Path(os.environ.get("PI_PANEL_CONFIG_HOME") or Path.home() / ".config" / "pi-panel")


def data_home() -> Path:
    return Path(os.environ.get("PI_PANEL_DATA_HOME") or Path.home() / ".local" / "share" / "pi-panel")


def runtime_dir() -> Path:
    return Path(os.environ.get("PI_PANEL_RUNTIME_DIR") or "/run/pi-panel")


def ctl_config() -> Path:
    return config_home() / "ctl.toml"


def registry() -> Path:
    return data_home() / "packages.json"


def compositor_socket() -> Path:
    return runtime_dir() / "compositor" / "io.pipanel.Compositor"


def ctl_socket() -> Path:
    return runtime_dir() / "ctl" / "io.pipanel.Ctl"


def app_socket(name: str) -> Path:
    return runtime_dir() / "apps" / name / "socket"


def unit_for(kind: str, name: str) -> str:
    return f"pi-panel-{kind}@{name}.service"
