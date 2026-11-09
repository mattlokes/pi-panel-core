"""Where pi-panel keeps things. pluginman's paths.py uses the same defaults
(a test in this package checks that they agree).

    PI_PANEL_CONFIG_HOME   /persistent/pi-panel/config   panel.toml, per-package config
    PI_PANEL_DATA_HOME     /persistent/pi-panel/state    the state registry, extra packages
    PI_PANEL_RUNTIME_DIR   /run/pi-panel                 Varlink sockets

The defaults are the panel's real layout, so a CLI run over SSH (which never
sees a unit's Environment=) finds the same files as the units. Each root can
be moved with its environment variable, which is how development and the
tests run a complete panel as an unprivileged user.

(Deliberately not PI_PANEL_CONFIG_DIR: pi-panel-run gives each package that
variable, pointing at the package's *own* config directory.)
"""

from __future__ import annotations

import os
from pathlib import Path


CONFIG_HOME = "/persistent/pi-panel/config"
DATA_HOME = "/persistent/pi-panel/state"
RUNTIME_DIR = "/run/pi-panel"


def config_home() -> Path:
    return Path(os.environ.get("PI_PANEL_CONFIG_HOME") or CONFIG_HOME)


def data_home() -> Path:
    return Path(os.environ.get("PI_PANEL_DATA_HOME") or DATA_HOME)


def runtime_dir() -> Path:
    return Path(os.environ.get("PI_PANEL_RUNTIME_DIR") or RUNTIME_DIR)


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
