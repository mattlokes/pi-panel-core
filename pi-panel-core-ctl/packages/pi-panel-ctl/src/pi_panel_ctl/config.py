"""ctl.toml: which packages are enabled, rotations and schedules.

ctl owns this file. Changes made over Varlink are written straight back, so
it always holds the panel's current configuration, and hand edits are picked
up on restart.

    enabled = ["immich", "clock", "mqtt"]
    selected_rotation = "default"
    default_seconds = 60          # per app, for the implicit rotation
    transition = "fade"           # or "cut"

    [[rotation]]
    name = "default"
    entries = [{app = "immich", seconds = 300}, {app = "clock", seconds = 60}]

    [clock]                       # the compositor's clock overlay
    enabled = true
    format = "%H:%M"              # strftime(3)
    position = "bottom_right"     # top_left, top_right, bottom_left, bottom_right, center
    size = 0                      # text height in px; 0 = automatic

    [[schedule]]
    name = "night"
    days = "daily"
    start = "23:00"
    end = "06:30"
    action = "power_off"
    priority = 10

Without a rotation named "default", ctl rotates through every enabled app for
`default_seconds` each, which is what a fresh install wants.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from .model import ClockSettings, Rotation, Schedule, ValidationError

TRANSITIONS = ("fade", "cut")


@dataclass
class CtlConfig:
    enabled: list[str] = field(default_factory=list)
    selected_rotation: str = "default"
    default_seconds: float = 60.0
    transition: str = "fade"
    rotations: dict[str, Rotation] = field(default_factory=dict)
    schedules: list[Schedule] = field(default_factory=list)
    clock: ClockSettings = field(default_factory=ClockSettings)

    @classmethod
    def load(cls, path: Path) -> "CtlConfig":
        if not path.exists():
            return cls()
        with path.open("rb") as fh:
            data = tomllib.load(fh)

        known = {"enabled", "selected_rotation", "default_seconds", "transition",
                 "rotation", "schedule", "clock"}
        unknown = set(data) - known
        if unknown:
            # A typo'd key would otherwise be silently dropped at the next save.
            raise ValidationError(f"{path}: unknown key(s): {', '.join(sorted(unknown))}")

        cfg = cls(
            enabled=[str(x) for x in data.get("enabled", [])],
            selected_rotation=str(data.get("selected_rotation", "default")),
            default_seconds=float(data.get("default_seconds", 60.0)),
            transition=str(data.get("transition", "fade")),
        )
        if cfg.transition not in TRANSITIONS:
            raise ValidationError(f"{path}: transition must be fade or cut")
        raw_clock = data.get("clock", {})
        if not isinstance(raw_clock, dict):
            raise ValidationError(f"{path}: clock must be a table")
        try:
            cfg.clock = ClockSettings.from_dict(raw_clock)
        except ValidationError as exc:
            raise ValidationError(f"{path}: clock: {exc}") from None
        for raw in data.get("rotation", []):
            rot = Rotation.from_dict(raw)
            cfg.rotations[rot.name] = rot
        names = set()
        for raw in data.get("schedule", []):
            sched = Schedule.from_dict(raw)
            if sched.name in names:
                raise ValidationError(f"{path}: duplicate schedule {sched.name!r}")
            names.add(sched.name)
            cfg.schedules.append(sched)
        return cfg

    def to_toml(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "enabled": self.enabled,
            "selected_rotation": self.selected_rotation,
            "default_seconds": self.default_seconds,
            "transition": self.transition,
            "clock": self.clock.to_dict(),
        }
        if self.rotations:
            data["rotation"] = [r.to_dict() for r in self.rotations.values()]
        if self.schedules:
            data["schedule"] = [s.to_config() for s in self.schedules]
        return data

    def save(self, path: Path) -> None:
        """Atomic: a power cut mid-write must not leave a truncated config."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ctl.toml.")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"# Managed by pi-panel-ctl; changes over Varlink are written back here.\n")
                tomli_w.dump(self.to_toml(), fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

