"""packages.json: what is installed, where from, and where to.

There are two, with the same format:

    the image registry   /opt/pi-panel/packages.json            what ships with the OS
    the state registry   /persistent/pi-panel/state/packages.json  what was added on the panel

`Registry` reads both and merges them; on a name clash the image wins (and a
warning says so). pluginman writes them; pi-panel-ctl reads them (name, kind,
version, description, path, varlink, rotate). Anything else here is
pluginman's own bookkeeping.

    {"version": 1, "packages": {"immich": {
        "name": "immich", "kind": "app", "version": "0.3.0", "description": "...",
        "path": "/opt/pi-panel/pi-panel-apps/immich", "varlink": true, "rotate": true,
        "source": {"type": "git", "url": "...", "ref": "main", "commit": "abc123"},
        "installed_at": "2026-09-23T12:00:00+00:00"}}}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
IMAGE, STATE = "image", "state"

log = logging.getLogger("pi-panel-pkg")


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
    origin: str = STATE            # which registry it is in; not stored

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        del data["origin"]
        return data

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


def _read(path: Path, origin: str) -> dict[str, Entry]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    out = {}
    for n, e in (data.get("packages") or {}).items():
        entry = Entry.from_dict(e)
        entry.origin = origin
        out[n] = entry
    return out


class Registry:
    """Both registries, merged. `entries` is the merged view; writes go to
    the registry an entry belongs to."""

    def __init__(self, path: Path, image_path: Path | None = None) -> None:
        self.path = path                  # the state registry
        self.image_path = image_path      # the image registry (None: not used)
        self.entries: dict[str, Entry] = {}
        self.load()

    def load(self) -> None:
        self.state = _read(self.path, STATE)
        self.image = _read(self.image_path, IMAGE) if self.image_path else {}
        for name in self.state.keys() & self.image.keys():
            log.warning("%s is in both registries; the image's wins", name)
        self.entries = {**self.state, **self.image}

    def _file(self, origin: str) -> tuple[Path, dict[str, Entry]]:
        if origin == IMAGE:
            if self.image_path is None:
                raise ValueError("no image registry configured")
            return self.image_path, self.image
        return self.path, self.state

    def save(self, origin: str = STATE) -> None:
        path, entries = self._file(origin)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": FORMAT_VERSION,
                "packages": {n: e.to_dict() for n, e in sorted(entries.items())}}
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".packages.json.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, name: str) -> Entry | None:
        return self.entries.get(name)

    def put(self, entry: Entry) -> None:
        """Record an entry in the registry its `origin` names."""
        _, entries = self._file(entry.origin)
        entries[entry.name] = entry
        self.save(entry.origin)
        self.entries = {**self.state, **self.image}

    def remove(self, name: str) -> None:
        entry = self.entries.get(name)
        if entry is None:
            return
        _, entries = self._file(entry.origin)
        entries.pop(name, None)
        self.save(entry.origin)
        self.entries = {**self.state, **self.image}
