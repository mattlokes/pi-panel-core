"""pi-panel-run KIND NAME: the ExecStart of pi-panel-app@ and pi-panel-plugin@.

It turns a package's manifest into a process: sets the environment, changes
into the package directory, and exec()s the command, so systemd supervises
the real program (signals, exit status, logs) with nothing in between.

Because of this there are no per-package unit files: installing a package
never needs root or a daemon-reload.

Environment given to every package:

    PI_PANEL_NAME, PI_PANEL_KIND   who it is
    PI_PANEL_PACKAGE_DIR           where it is installed (also the working dir)
    PI_PANEL_CONFIG_DIR            its own config directory (created if missing)
    PI_PANEL_CTL_SOCKET            io.pipanel.Ctl, the panel's API
    PI_PANEL_APP_SOCKET            apps with [app] varlink = true: where to serve io.pipanel.App
    XDG_RUNTIME_DIR                /run/user/<uid> if the unit did not set it
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import paths
from .manifest import Manifest, ManifestError
from .registry import Registry


def build(kind: str, name: str) -> tuple[Path, list[str], dict[str, str]]:
    entry = Registry(paths.registry()).get(name)
    if entry is None:
        raise SystemExit(f"pi-panel-run: {name!r} is not installed (pi-panel-pkg list)")
    if entry.kind != kind:
        raise SystemExit(f"pi-panel-run: {name!r} is a {entry.kind}, not a {kind}")
    package_dir = Path(entry.path)
    try:
        manifest = Manifest.load(package_dir)
    except ManifestError as exc:
        raise SystemExit(f"pi-panel-run: {name}: {exc}") from None

    config_dir = paths.package_config_dir(kind, name)
    config_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '/usr/bin:/bin')}"
    env.update(manifest.env)
    # Set last: a manifest must not be able to redirect these.
    env.update({
        "PI_PANEL_NAME": name,
        "PI_PANEL_KIND": kind,
        "PI_PANEL_PACKAGE_DIR": str(package_dir),
        "PI_PANEL_CONFIG_DIR": str(config_dir),
        "PI_PANEL_CTL_SOCKET": str(paths.ctl_socket()),
    })
    if kind == "app" and manifest.varlink:
        env["PI_PANEL_APP_SOCKET"] = str(paths.app_socket(name))

    argv = manifest.argv({
        "config_dir": str(config_dir),
        "package_dir": str(package_dir),
        "name": name,
        "runtime_dir": str(paths.runtime_dir()),
    })
    return package_dir, argv, env


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("app", "plugin"):
        print("usage: pi-panel-run app|plugin NAME", file=sys.stderr)
        return 2
    package_dir, argv, env = build(sys.argv[1], sys.argv[2])
    os.chdir(package_dir)
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        print(f"pi-panel-run: cannot run {argv[0]}: {exc}", file=sys.stderr)
        return 127
    return 0  # not reached


if __name__ == "__main__":
    raise SystemExit(main())
