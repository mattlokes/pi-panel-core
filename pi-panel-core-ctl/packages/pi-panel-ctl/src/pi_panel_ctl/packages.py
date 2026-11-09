"""Installed packages, as recorded by pi-panel-pkg (pluginman).

There are two registries in the same format, merged here: the image's
(`/opt/pi-panel/packages.json`, what ships with the OS) and the panel's state
(`/persistent/pi-panel/state/packages.json`, what was added on it). On a name
clash the image's entry wins. Both are written by pluginman:

    {
      "version": 1,
      "packages": {
        "immich": {
          "name": "immich", "kind": "app", "version": "0.3.0",
          "description": "...", "path": "/opt/pi-panel/pi-panel-apps/immich",
          "varlink": true, "rotate": true,
          "source": {...}, "installed_at": "..."      # pluginman's own bookkeeping
        }
      }
    }

ctl only reads it. A missing or unreadable registry means "nothing installed",
never a crash: ctl must come up even on a fresh machine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pi-panel-ctl")

KINDS = ("app", "plugin")


@dataclass(frozen=True, slots=True)
class Package:
    name: str
    kind: str
    version: str | None = None
    description: str | None = None
    path: str | None = None
    varlink: bool = False
    rotate: bool = True     # in the implicit default rotation (a camera app says no)
    origin: str = "state"   # "image" or "state": which registry it came from
    check: str | None = None  # [config] check, with ${config_dir} etc. unresolved


def load_registries(image: Path | None, state: Path) -> dict[str, Package]:
    """Both registries merged; the image's entry wins a name clash."""
    image_pkgs = load_registry(image, "image") if image else {}
    state_pkgs = load_registry(state, "state")
    for name in image_pkgs.keys() & state_pkgs.keys():
        log.warning("%s is in both package registries; using the image's", name)
    return {**state_pkgs, **image_pkgs}


def load_registry(path: Path, origin: str = "state") -> dict[str, Package]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.error("cannot read package registry %s: %s", path, exc)
        return {}

    out: dict[str, Package] = {}
    for name, entry in (data.get("packages") or {}).items():
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            log.warning("skipping malformed registry entry %r", name)
            continue
        out[name] = Package(
            name=name,
            kind=entry["kind"],
            version=entry.get("version"),
            description=entry.get("description"),
            path=entry.get("path"),
            varlink=bool(entry.get("varlink", False)),
            rotate=bool(entry.get("rotate", True)),
            origin=origin,
            check=entry.get("check") if isinstance(entry.get("check"), str) else None,
        )
    return out
