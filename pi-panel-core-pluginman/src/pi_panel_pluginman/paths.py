"""The pi-panel file layout, as pluginman sees it.

Installed packages live beside the core, in the same tree the repos are
developed in:

    /opt/pi-panel/pi-panel-core/      pi-panel-core-compositor, -ctl, -pluginman
    /opt/pi-panel/pi-panel-apps/      one directory per app that ships with the OS
    /opt/pi-panel/pi-panel-plugin/    one directory per plugin that ships with the OS
    /opt/pi-panel/packages.json       the image registry (read-only on a panel)

Packages a user adds later go to the writable state directory, in the same
shape, with a pi-panel-core symlink beside them:

    /persistent/pi-panel/state/packages/pi-panel-apps/<name>
    /persistent/pi-panel/state/packages/pi-panel-core -> /opt/pi-panel/pi-panel-core
    /persistent/pi-panel/state/packages.json            the state registry

Keeping them siblings lets every package reach the shared libraries with one
relative path (`../../pi-panel-core/pi-panel-core-ctl/packages/...`) that works
on the Pi and on a development machine alike.

The defaults are the panel's real layout, so a CLI run over SSH (which never
sees a unit's Environment=) finds the same files as the units. pi-panel-ctl's
paths.py has the same defaults; a test there checks that they agree.
Environment overrides:

    PI_PANEL_HOME          /opt/pi-panel                 root of the tree above
    PI_PANEL_CONFIG_HOME   /persistent/pi-panel/config   panel.toml, apps/ and plugins/ config
    PI_PANEL_DATA_HOME     /persistent/pi-panel/state    packages.json (the registry), staging
    PI_PANEL_RUNTIME_DIR   /run/pi-panel                 sockets
"""

from __future__ import annotations

import os
from pathlib import Path

KIND_DIRS = {"app": "pi-panel-apps", "plugin": "pi-panel-plugin"}

HOME = "/opt/pi-panel"
CONFIG_HOME = "/persistent/pi-panel/config"
DATA_HOME = "/persistent/pi-panel/state"
RUNTIME_DIR = "/run/pi-panel"


def home() -> Path:
    return Path(os.environ.get("PI_PANEL_HOME") or HOME)


def config_home() -> Path:
    return Path(os.environ.get("PI_PANEL_CONFIG_HOME") or CONFIG_HOME)


def data_home() -> Path:
    return Path(os.environ.get("PI_PANEL_DATA_HOME") or DATA_HOME)


def runtime_dir() -> Path:
    return Path(os.environ.get("PI_PANEL_RUNTIME_DIR") or RUNTIME_DIR)


def registry() -> Path:
    """The state registry: packages installed on this panel."""
    return data_home() / "packages.json"


def image_registry() -> Path:
    """The image registry: packages that ship with the OS (read-only on a panel)."""
    return home() / "packages.json"


def extras_home() -> Path:
    """Where packages installed on the panel go (not part of the image)."""
    return data_home() / "packages"


def staging() -> Path:
    return data_home() / "staging"


def package_dir(kind: str, name: str, image: bool = False) -> Path:
    """Where a package is installed: beside the core in the image, or under
    extras_home() for one installed on the panel."""
    return (home() if image else extras_home()) / KIND_DIRS[kind] / name


def package_config_dir(kind: str, name: str) -> Path:
    return config_home() / f"{kind}s" / name


def app_socket(name: str) -> Path:
    return runtime_dir() / "apps" / name / "socket"


def ctl_socket() -> Path:
    return runtime_dir() / "ctl" / "io.pipanel.Ctl"
