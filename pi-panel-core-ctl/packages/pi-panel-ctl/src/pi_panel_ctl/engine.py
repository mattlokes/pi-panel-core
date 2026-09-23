"""What should be on screen, and why: pure logic with an injected clock.

The daemon feeds this engine facts (which apps currently have a mapped window)
and requests (Show, Next, schedules), calls `decide()` and applies the
difference to the compositor. Nothing here does I/O, so every rule can be
tested exactly, including schedules that cross midnight.

The priority stack, highest first:

    holds      Show() requests, each with a priority and an optional expiry
    schedules  time-window rules, each with a priority
    rotation   the selected playlist (or one chosen by a schedule rule)

A hold and a schedule compete on priority; on a tie the hold wins, and among
holds the most recent wins.
"""

from __future__ import annotations

import itertools
import secrets
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from .model import Rotation, Schedule

# However far away the next known change is, look again at least this often:
# the wall clock can jump (NTP sync after boot, DST).
MAX_SLEEP = 60.0


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return _time.monotonic()

    def now(self) -> datetime:
        return datetime.now()


@dataclass(slots=True)
class Hold:
    token: str
    app: str
    priority: int
    expires_at: float | None  # monotonic
    seq: int

    def expires_in(self, now: float) -> float | None:
        return None if self.expires_at is None else max(0.0, self.expires_at - now)


@dataclass(frozen=True, slots=True)
class Decision:
    showing: str | None           # None: leave the display as it is
    reason: str                   # hold | schedule | rotation | none
    power: bool
    rotation: str                 # the rotation in effect
    active_schedules: tuple[str, ...] = ()


@dataclass(slots=True)
class _Cursor:
    rotation: str | None = None
    index: int = 0
    since: float = 0.0


@dataclass
class Engine:
    rotations: dict[str, Rotation]
    selected: str
    schedules: list[Schedule] = field(default_factory=list)
    clock: Clock = field(default_factory=SystemClock)

    available: set[str] = field(default_factory=set)
    paused: bool = False
    holds: dict[str, Hold] = field(default_factory=dict)

    _cursor: _Cursor = field(default_factory=_Cursor)
    _seq: itertools.count = field(default_factory=itertools.count)

    # --- requests --------------------------------------------------------

    def set_available(self, apps: set[str]) -> None:
        self.available = set(apps)

    def show(self, app: str, seconds: float | None = None, priority: int = 0) -> str:
        token = secrets.token_hex(6)
        expires = None if seconds is None else self.clock.monotonic() + seconds
        self.holds[token] = Hold(token, app, priority, expires, next(self._seq))
        return token

    def release(self, token: str) -> bool:
        return self.holds.pop(token, None) is not None

    def release_app(self, app: str) -> None:
        for token in [t for t, h in self.holds.items() if h.app == app]:
            del self.holds[token]

    def step(self, delta: int) -> None:
        """Next (+1) / Previous (-1). Also drops holds that never expire: a
        pinned app is the thing a user pressing 'next' wants to move away from."""
        for token in [t for t, h in self.holds.items() if h.expires_at is None]:
            del self.holds[token]
        rotation = self._rotation_in_effect()
        entries = self._entries(rotation)
        if not entries:
            return
        if self._cursor.rotation != rotation:
            self._reset_cursor(rotation)
        self._advance(entries, delta)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        if self.paused:
            self.paused = False
            # The current entry gets a full period again.
            self._cursor.since = self.clock.monotonic()

    # --- configuration ---------------------------------------------------

    def set_rotation(self, rotation: Rotation) -> None:
        self.rotations[rotation.name] = rotation
        if self._cursor.rotation == rotation.name:
            self._cursor.index = 0 if not rotation.entries else min(
                self._cursor.index, len(rotation.entries) - 1
            )

    def remove_rotation(self, name: str) -> None:
        self.rotations.pop(name, None)

    def select_rotation(self, name: str) -> None:
        self.selected = name

    # --- decision --------------------------------------------------------

    def decide(self) -> Decision:
        """The current decision. Advances the rotation and expires holds as
        time passes, so call it whenever something may have changed and
        whenever next_wakeup() says to."""
        mono = self.clock.monotonic()
        now = self.clock.now()
        for token in [t for t, h in self.holds.items()
                      if h.expires_at is not None and h.expires_at <= mono]:
            del self.holds[token]

        active = [s for s in self.schedules if s.is_active(now)]
        active_names = tuple(s.name for s in active)

        best_hold = max(self.holds.values(), key=lambda h: (h.priority, h.seq), default=None)
        best_rule = max(active, key=lambda s: s.priority, default=None)

        if best_hold and (best_rule is None or best_hold.priority >= best_rule.priority):
            return Decision(best_hold.app, "hold", True, self.selected, active_names)

        if best_rule is not None:
            if best_rule.action == "power_off":
                return Decision(None, "schedule", False, self.selected, active_names)
            if best_rule.action == "show":
                return Decision(best_rule.app, "schedule", True, self.selected, active_names)
            rotation = best_rule.rotation or self.selected
            showing = self._rotation_pick(rotation, mono)
            return Decision(showing, "schedule" if showing else "none", True, rotation, active_names)

        showing = self._rotation_pick(self.selected, mono)
        return Decision(showing, "rotation" if showing else "none", True, self.selected, active_names)

    def next_wakeup(self) -> float:
        """Seconds until the decision could change on its own."""
        mono = self.clock.monotonic()
        now = self.clock.now()
        waits = [MAX_SLEEP]
        for h in self.holds.values():
            if h.expires_at is not None:
                waits.append(h.expires_at - mono)
        for s in self.schedules:
            waits.append((s.next_boundary(now) - now).total_seconds())
        if not self.paused and self._cursor.rotation is not None:
            entries = self._entries(self._cursor.rotation)
            rotation = self.rotations.get(self._cursor.rotation)
            if entries and rotation and self._cursor.index < len(rotation.entries):
                entry = rotation.entries[self._cursor.index]
                waits.append(self._cursor.since + entry.seconds - mono)
        return max(0.05, min(waits))

    # --- rotation internals ----------------------------------------------

    def _rotation_in_effect(self) -> str:
        now = self.clock.now()
        rules = [s for s in self.schedules if s.is_active(now) and s.action == "rotation"]
        best = max(rules, key=lambda s: s.priority, default=None)
        return best.rotation if best and best.rotation else self.selected

    def _entries(self, name: str) -> list[int]:
        """Indexes of entries whose app can be shown right now."""
        rotation = self.rotations.get(name)
        if rotation is None:
            return []
        return [i for i, e in enumerate(rotation.entries) if e.app in self.available]

    def _reset_cursor(self, name: str) -> None:
        self._cursor = _Cursor(name, 0, self.clock.monotonic())
        entries = self._entries(name)
        if entries:
            self._cursor.index = entries[0]

    def _advance(self, entries: list[int], delta: int) -> None:
        rotation = self.rotations[self._cursor.rotation]  # type: ignore[index]
        n = len(rotation.entries)
        i = self._cursor.index
        for _ in range(n):
            i = (i + delta) % n
            if i in entries:
                break
        self._cursor.index = i
        self._cursor.since = self.clock.monotonic()

    def _rotation_pick(self, name: str, mono: float) -> str | None:
        entries = self._entries(name)
        if not entries:
            return None
        if self._cursor.rotation != name:
            self._reset_cursor(name)
        rotation = self.rotations[name]
        if self._cursor.index not in entries:
            # The current app lost its window (or the rotation was edited):
            # move on at once rather than showing nothing.
            self._advance(entries, +1)
        elif not self.paused:
            entry = rotation.entries[self._cursor.index]
            if mono - self._cursor.since >= entry.seconds:
                self._advance(entries, +1)
        return rotation.entries[self._cursor.index].app
