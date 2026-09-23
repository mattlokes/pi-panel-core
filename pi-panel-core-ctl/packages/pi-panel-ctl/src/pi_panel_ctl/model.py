"""Rotation, schedule and clock overlay data, and the rules for when a schedule applies."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEDULE_ACTIONS = ("power_off", "show", "rotation")
CLOCK_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right", "center")
# The compositor's limits (io.pipanel.Compositor.SetClock), checked here so a
# bad value is refused to the caller instead of by the compositor later.
CLOCK_FORMAT_MAX_BYTES = 63
CLOCK_SIZE_MIN, CLOCK_SIZE_MAX = 8, 1000
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ValidationError(ValueError):
    """A rotation or schedule is malformed. The message says why."""


@dataclass(frozen=True, slots=True)
class RotationEntry:
    app: str
    seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {"app": self.app, "seconds": self.seconds}


@dataclass(frozen=True, slots=True)
class Rotation:
    name: str
    entries: tuple[RotationEntry, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rotation":
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("a rotation needs a name")
        raw = data.get("entries") or []
        if not isinstance(raw, list):
            raise ValidationError("entries must be a list")
        entries = []
        for i, e in enumerate(raw):
            if not isinstance(e, dict) or not isinstance(e.get("app"), str):
                raise ValidationError(f"entry {i} needs an app")
            seconds = e.get("seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ValidationError(f"entry {i} needs seconds > 0")
            entries.append(RotationEntry(e["app"], float(seconds)))
        return cls(name, tuple(entries))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "entries": [e.to_dict() for e in self.entries]}


def parse_days(spec: str) -> frozenset[int]:
    """'daily', 'mon-fri', 'sat,sun', 'mon,wed-fri' -> weekday numbers (Mon=0).

    Ranges may wrap: 'fri-mon' is Fri, Sat, Sun, Mon."""
    spec = spec.strip().lower()
    if spec in ("daily", "every day", "*", "mon-sun"):
        return frozenset(range(7))
    days: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in DAY_NAMES or b not in DAY_NAMES:
                raise ValidationError(f"unknown day in {part!r}")
            i, j = DAY_NAMES.index(a), DAY_NAMES.index(b)
            while True:
                days.add(i)
                if i == j:
                    break
                i = (i + 1) % 7
        elif part in DAY_NAMES:
            days.add(DAY_NAMES.index(part))
        else:
            raise ValidationError(f"unknown day {part!r}")
    if not days:
        raise ValidationError("no days given")
    return frozenset(days)


def parse_hhmm(value: str) -> time:
    m = _HHMM.match(value.strip())
    if not m:
        raise ValidationError(f"time {value!r} is not HH:MM")
    return time(int(m.group(1)), int(m.group(2)))


@dataclass(frozen=True, slots=True)
class Schedule:
    name: str
    days: str
    start: str
    end: str
    action: str
    app: str | None = None
    rotation: str | None = None
    priority: int = 0
    # Parsed forms, filled by validate().
    _days: frozenset[int] = field(default=frozenset(), compare=False, repr=False)
    _start: time = field(default=time(0), compare=False, repr=False)
    _end: time = field(default=time(0), compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Schedule":
        def s(key: str, required: bool = True) -> str | None:
            value = data.get(key)
            if value is None and not required:
                return None
            if not isinstance(value, str) or not value:
                raise ValidationError(f"{key} is required")
            return value

        priority = data.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValidationError("priority must be an integer")
        sched = cls(
            name=s("name"),  # type: ignore[arg-type]
            days=s("days"),  # type: ignore[arg-type]
            start=s("start"),  # type: ignore[arg-type]
            end=s("end"),  # type: ignore[arg-type]
            action=s("action"),  # type: ignore[arg-type]
            app=s("app", required=False),
            rotation=s("rotation", required=False),
            priority=priority,
        )
        return sched.validate()

    def validate(self) -> "Schedule":
        if self.action not in SCHEDULE_ACTIONS:
            raise ValidationError(f"action must be one of {', '.join(SCHEDULE_ACTIONS)}")
        if self.action == "show" and not self.app:
            raise ValidationError("action 'show' needs an app")
        if self.action == "rotation" and not self.rotation:
            raise ValidationError("action 'rotation' needs a rotation")
        start, end = parse_hhmm(self.start), parse_hhmm(self.end)
        if start == end:
            raise ValidationError("start and end are the same time")
        object.__setattr__(self, "_days", parse_days(self.days))
        object.__setattr__(self, "_start", start)
        object.__setattr__(self, "_end", end)
        return self

    def is_active(self, now: datetime) -> bool:
        """Whether |now| (local time) falls in the window.

        A window that runs past midnight belongs to the day it *starts*:
        'fri 23:00-06:30' covers Friday night into Saturday morning."""
        t = now.time()
        if self._start < self._end:
            return now.weekday() in self._days and self._start <= t < self._end
        if t >= self._start:
            return now.weekday() in self._days
        if t < self._end:
            return (now - timedelta(days=1)).weekday() in self._days
        return False

    def next_boundary(self, now: datetime) -> datetime:
        """The next moment this rule starts or stops applying (for timers)."""
        candidates = []
        for day in range(0, 8):
            base = (now + timedelta(days=day)).replace(second=0, microsecond=0)
            for t in (self._start, self._end):
                moment = base.replace(hour=t.hour, minute=t.minute)
                if moment > now:
                    candidates.append(moment)
            if candidates:
                break
        return min(candidates)

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "days": self.days,
            "start": self.start,
            "end": self.end,
            "action": self.action,
            "app": self.app,
            "rotation": self.rotation,
            "priority": self.priority,
        }
        if now is not None:
            d["active"] = self.is_active(now)
        return d

    def to_config(self) -> dict[str, Any]:
        """For ctl.toml: TOML has no null, so absent keys are simply left out."""
        return {k: v for k, v in self.to_dict().items() if v is not None}


@dataclass(frozen=True, slots=True)
class ClockSettings:
    """The compositor's clock overlay, as ctl wants it.

    ctl owns these settings (they live in ctl.toml) and the reconcile pass
    pushes them to the compositor whenever what it reports differs."""

    enabled: bool = False
    format: str = "%H:%M"          # strftime(3), the panel's local time
    position: str = "bottom_right"
    size: int = 0                  # text height in px; 0 = automatic

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClockSettings":
        return cls().merged(data)

    def merged(self, changes: dict[str, Any]) -> "ClockSettings":
        """These settings with |changes| applied; None/absent keys are kept."""
        unknown = set(changes) - {"enabled", "format", "position", "size"}
        if unknown:
            raise ValidationError(f"unknown clock key(s): {', '.join(sorted(unknown))}")
        values = {k: v for k, v in changes.items() if v is not None}
        enabled = values.get("enabled", self.enabled)
        fmt = values.get("format", self.format)
        position = values.get("position", self.position)
        size = values.get("size", self.size)
        if not isinstance(enabled, bool):
            raise ValidationError("enabled must be true or false")
        if (not isinstance(fmt, str) or not fmt
                or len(fmt.encode()) > CLOCK_FORMAT_MAX_BYTES):
            raise ValidationError(f"format must be 1-{CLOCK_FORMAT_MAX_BYTES} bytes")
        if position not in CLOCK_POSITIONS:
            raise ValidationError(f"position must be one of {', '.join(CLOCK_POSITIONS)}")
        if (isinstance(size, bool) or not isinstance(size, int)
                or not (size == 0 or CLOCK_SIZE_MIN <= size <= CLOCK_SIZE_MAX)):
            raise ValidationError(f"size must be 0 (automatic) or {CLOCK_SIZE_MIN}-{CLOCK_SIZE_MAX}")
        return ClockSettings(enabled, fmt, position, size)

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "format": self.format,
                "position": self.position, "size": self.size}
