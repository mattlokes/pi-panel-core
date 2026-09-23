"""packages.json: what is installed, where from, and where to.

pluginman writes it; pi-panel-ctl reads it (name, kind, version, description,
path, varlink, rotate). Anything else here is pluginman's own bookkeeping.

    {"version": 1, "packages": {"immich": {
        "name": "immich", "kind": "app", "version": "0.3.0", "description": "...",
        "path": "/home/pi/pi-panel-apps/immich", "varlink": true, "rotate": true,
        "source": {"type": "git", "url": "...", "ref": "main", "commit": "abc123"},
        "installed_at": "2026-09-23T12:00:00+00:00"}}}
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1


@dataclass(slots=True)
class Source:
    type: str                      # git | tarball | local
    url: str
    ref: str | None = None         # git branch/tag/commit asked for
    commit: str | None = None      # git commit actually installed
    sha256: str | None = None      # tarball digest


@dataclass(slots=True)
class Entry:
    name: str
    kind: str
    path: str
    source: Source
    version: str | None = None
    description: str | None = None
    varlink: bool = False
    rotate: bool = True
    installed_at: str = field(default_factory=lambda: datetime.now(timezone.utc)
                              .isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        src = data.get("source") or {}
        return cls(
            name=data["name"], kind=data["kind"], path=data["path"],
            source=Source(src.get("type", "local"), src.get("url", ""), src.get("ref"),
                          src.get("commit"), src.get("sha256")),
            version=data.get("version"), description=data.get("description"),
            varlink=bool(data.get("varlink", False)),
            rotate=bool(data.get("rotate", True)),
            installed_at=data.get("installed_at", ""),
        )


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, Entry] = {}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.entries = {}
            return
        self.entries = {n: Entry.from_dict(e) for n, e in (data.get("packages") or {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": FORMAT_VERSION,
                "packages": {n: e.to_dict() for n, e in sorted(self.entries.items())}}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".packages.json.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, name: str) -> Entry | None:
        return self.entries.get(name)

    def put(self, entry: Entry) -> None:
        self.entries[entry.name] = entry
        self.save()

    def remove(self, name: str) -> None:
        self.entries.pop(name, None)
        self.save()
