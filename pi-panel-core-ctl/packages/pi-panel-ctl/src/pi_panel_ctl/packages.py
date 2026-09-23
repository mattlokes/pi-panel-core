"""Installed packages, as recorded by pi-panel-pkg (pluginman).

The registry is `<data_home>/packages.json`, written by pluginman:

    {
      "version": 1,
      "packages": {
        "immich": {
          "name": "immich", "kind": "app", "version": "0.3.0",
          "description": "...", "path": "/home/pi/.local/share/pi-panel/packages/immich",
          "varlink": true,
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


def load_registry(path: Path) -> dict[str, Package]:
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
        )
    return out
