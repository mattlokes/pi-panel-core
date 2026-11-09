"""Move a panel from the old home-directory install to /opt + /persistent.

    python -m pi_panel_ctl.migrate [--dry-run] [--force]

Run it as the panel user, with the new core's venv
(/opt/pi-panel/pi-panel-core/pi-panel-core-ctl/.venv/bin/python), after root
has created /opt/pi-panel and /persistent/pi-panel/{config,state} and the new
core has been built in place. It does not touch the running panel or the old
install: the units still run the old one until install.sh is run (the last
step, which it prints).

1. config: ~/.config/pi-panel -> /persistent/pi-panel/config
   - ctl.toml becomes panel.toml (schema = 1), comments kept.
   - plugins/mqtt/mqtt.toml folds into [mqtt] / [mqtt.homeassistant], with
     enabled = true, and "mqtt" leaves `enabled` (it is no longer a package).
   - apps/* and any other plugin config are copied, modes kept.
   - /etc/pi-panel/compositor.env is carried over if it sets anything.
   Everything is validated (ctl's and the bridge's own checks) before a file
   is written.
2. the apps: each installed app is reinstalled with `pi-panel-pkg install
   --image` from its GitHub mirror (what panels install from: they may not
   reach Forgejo), which builds it in place under /opt.
3. immich's config.yaml becomes config.toml (the app's own --convert-yaml).
4. `pi-panel-ctl config check`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
from pi_panel_mqtt.config import ConfigError as MqttConfigError
from pi_panel_mqtt.config import validate as validate_mqtt

from . import paths
from .config import CtlConfig
from .model import ValidationError

# Package name -> repo, for the apps that ship with the panel. The public
# GitHub mirrors, not Forgejo: panels install from GitHub (jazz, on the
# 192.168.4 LAN, cannot reach the 10.x network Forgejo lives on).
GITHUB = "https://github.com/mattlokes"
REPOS = {
    "immich": f"{GITHUB}/pi-panel-immich.git",
    "now-playing": f"{GITHUB}/pi-panel-now-playing.git",
    "webrtc-client": f"{GITHUB}/pi-panel-webrtc-client.git",
}
OLD_MQTT_KEYS = {"host", "port", "username", "password", "base_topic", "device_id",
                 "keepalive", "tls"}


class MigrationError(Exception):
    pass


@dataclass
class Plan:
    """What the migration will write, worked out (and validated) up front."""
    panel_toml: str
    copies: list[tuple[Path, Path]] = field(default_factory=list)
    compositor_env: str | None = None
    notes: list[str] = field(default_factory=list)


# --- step 1: the config --------------------------------------------------------

def mqtt_table(old: dict[str, Any]) -> dict[str, Any]:
    """The plugin's mqtt.toml as panel.toml's [mqtt] table."""
    unknown = set(old) - {"mqtt", "homeassistant", "ctl", "log_level"}
    if unknown:
        raise MigrationError(f"mqtt.toml: unknown section(s): {', '.join(sorted(unknown))}")
    table: dict[str, Any] = {"enabled": True}
    if "log_level" in old:
        table["log_level"] = str(old["log_level"]).upper()
    for key, value in (old.get("mqtt") or {}).items():
        if key not in OLD_MQTT_KEYS:
            raise MigrationError(f"mqtt.toml: unknown key [mqtt] {key}")
        table[key] = value
    ha = old.get("homeassistant") or {}
    if ha:
        table["homeassistant"] = dict(ha)
    return table


def plan_config(old_home: Path, new_home: Path, etc_env: Path) -> tuple[Plan, CtlConfig]:
    ctl_toml = old_home / "ctl.toml"
    if not ctl_toml.exists():
        raise MigrationError(f"{ctl_toml} not found: nothing to migrate")
    doc = tomlkit.parse(ctl_toml.read_text(encoding="utf-8"))
    notes: list[str] = []

    # A fresh header; the old one said "Managed by pi-panel-ctl".
    body = tomlkit.dumps(doc)
    lines = body.splitlines(keepends=True)
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    doc = tomlkit.parse("".join(lines))
    new = tomlkit.document()
    new.add(tomlkit.comment("pi-panel's main configuration (migrated from ctl.toml)."))
    new.add(tomlkit.comment("Edit it by hand, then apply with `pi-panel-ctl reload`."))
    new.add(tomlkit.nl())
    new["schema"] = 1
    for key, value in doc.body:
        if key is None:
            new.add(value)          # comments and blank lines, kept in place
        else:
            new[key.key] = value

    enabled = list(new.get("enabled", []))
    if "mqtt" in enabled:
        new["enabled"] = [n for n in enabled if n != "mqtt"]
        notes.append('"mqtt" left `enabled`: the bridge is now [mqtt] in panel.toml')

    mqtt_path = old_home / "plugins" / "mqtt" / "mqtt.toml"
    if mqtt_path.exists():
        old = tomllib.loads(mqtt_path.read_text(encoding="utf-8"))
        if (old.get("ctl") or {}).get("socket_path"):
            notes.append("mqtt.toml's [ctl] socket_path is dropped: the bridge uses ctl's socket")
        table = mqtt_table(old)
        try:
            validate_mqtt(table)
        except MqttConfigError as exc:
            raise MigrationError(f"{mqtt_path}: {exc}") from None
        mqtt = tomlkit.table()
        for k, v in table.items():
            if k != "homeassistant":
                mqtt[k] = v
        if "homeassistant" in table:
            ha = tomlkit.table()
            ha.update(table["homeassistant"])
            mqtt["homeassistant"] = ha
        new["mqtt"] = mqtt
        notes.append(f"{mqtt_path} folded into [mqtt] (enabled = true)")

    text = tomlkit.dumps(new)
    try:
        cfg = CtlConfig.from_dict(tomllib.loads(text))
    except ValidationError as exc:
        raise MigrationError(f"the migrated panel.toml would not load: {exc}") from None

    plan = Plan(panel_toml=text, notes=notes)
    for kind in ("apps", "plugins"):
        for src in sorted((old_home / kind).glob("*")):
            if kind == "plugins" and src.name == "mqtt":
                continue
            plan.copies.append((src, new_home / kind / src.name))
    if etc_env.exists():
        settings = [ln for ln in etc_env.read_text().splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")]
        if settings:
            plan.compositor_env = etc_env.read_text()
            notes.append(f"{etc_env} carried over: {', '.join(s.split('=')[0] for s in settings)}")
        else:
            notes.append(f"{etc_env} sets nothing; the start wrapper now picks the renderer")
    return plan, cfg


def write_config(plan: Plan, new_home: Path) -> None:
    new_home.mkdir(parents=True, exist_ok=True)
    target = new_home / "panel.toml"
    target.write_text(plan.panel_toml, encoding="utf-8")
    target.chmod(0o600)                 # it holds the MQTT password, if any
    for src, dest in plan.copies:
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        shutil.copytree(src, dest, symlinks=True) if src.is_dir() else shutil.copy2(src, dest)
    if plan.compositor_env is not None:
        (new_home / "compositor.env").write_text(plan.compositor_env)


# --- steps 2-4: the apps, immich's YAML, the check ---------------------------------

def _run(argv: list[str], **kw: Any) -> None:
    print("$", " ".join(argv), flush=True)
    result = subprocess.run(argv, **kw)
    if result.returncode != 0:
        raise MigrationError(f"`{' '.join(argv)}` failed with exit status {result.returncode}")


def old_apps(old_registry: Path) -> list[str]:
    try:
        data = json.loads(old_registry.read_text())
    except FileNotFoundError:
        return []
    return sorted(n for n, e in (data.get("packages") or {}).items() if e.get("kind") == "app")


def install_apps(names: list[str], pkg: Path) -> None:
    for name in names:
        url = REPOS.get(name)
        if url is None:
            print(f"warning: no repo known for {name!r}; install it yourself", flush=True)
            continue
        _run([str(pkg), "install", "--image", "-y", url])


def convert_immich(new_home: Path, old_home: Path, app_dir: Path) -> str | None:
    yaml_path = old_home / "apps" / "immich" / "config.yaml"
    toml_path = new_home / "apps" / "immich" / "config.toml"
    if not yaml_path.exists():
        return None
    # The install copied the example template; the real settings replace it.
    toml_path.unlink(missing_ok=True)
    (new_home / "apps" / "immich" / "config.yaml").unlink(missing_ok=True)
    # --frozen --no-dev: leave the installed venv as its [build] made it.
    _run(["uv", "run", "--frozen", "--no-dev", "--project", str(app_dir), "--with", "pyyaml",
          "pi-panel-immich",
          "--convert-yaml", str(yaml_path), str(toml_path)])
    return f"immich: {yaml_path} converted to {toml_path}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pi_panel_ctl.migrate",
                                description="Move a panel from ~/ to /opt + /persistent.")
    p.add_argument("--from-config", type=Path, default=Path.home() / ".config" / "pi-panel")
    p.add_argument("--from-registry", type=Path,
                   default=Path.home() / ".local" / "share" / "pi-panel" / "packages.json")
    p.add_argument("--etc-env", type=Path, default=Path("/etc/pi-panel/compositor.env"))
    p.add_argument("--dry-run", action="store_true", help="show what would be done; write nothing")
    p.add_argument("--force", action="store_true", help="even if panel.toml already exists")
    p.add_argument("--config-only", action="store_true", help="step 1 only (no app installs)")
    args = p.parse_args(argv)

    new_home = paths.config_home()
    core = paths.home() / "pi-panel-core"
    pkg = core / "pi-panel-core-pluginman" / ".venv" / "bin" / "pi-panel-pkg"
    ctl = core / "pi-panel-core-ctl" / ".venv" / "bin" / "pi-panel-ctl"

    try:
        if (new_home / "panel.toml").exists() and not args.force:
            raise MigrationError(f"{new_home / 'panel.toml'} exists: already migrated? (--force)")
        plan, cfg = plan_config(args.from_config, new_home, args.etc_env)
        apps = old_apps(args.from_registry)

        print(f"config: {args.from_config} -> {new_home}")
        print(f"  panel.toml: enabled = {cfg.enabled}, mqtt = "
              f"{'on' if cfg.mqtt_enabled() else 'off'}")
        for src, dest in plan.copies:
            print(f"  copy {src} -> {dest}")
        for note in plan.notes:
            print(f"  note: {note}")
        if not args.config_only:
            print(f"apps, reinstalled into {paths.home()} as image packages: {', '.join(apps)}")
        if args.dry_run:
            print("(dry run: nothing written)")
            return 0

        write_config(plan, new_home)
        if not args.config_only:
            if not pkg.exists():
                raise MigrationError(f"{pkg} not found: build the new core first")
            install_apps(apps, pkg)
            if "immich" in apps:
                note = convert_immich(new_home, args.from_config,
                                      paths.home() / "pi-panel-apps" / "immich")
                if note:
                    print(note)
        if ctl.exists():
            _run([str(ctl), "config", "check"])
    except MigrationError as exc:
        print(f"migration stopped: {exc}", file=sys.stderr)
        return 1

    print(f"""
Migrated. The panel still runs the old install. To switch over (as root):

    {core}/deploy/install.sh && systemctl restart pi-panel.target

To go back: run the old ~/pi-panel-core/deploy/install.sh and restart.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
