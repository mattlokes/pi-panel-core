"""panel.toml: the panel's main configuration (enabled packages, rotations,
schedules, the clock overlay).

People edit this file by hand, and ctl changes it over Varlink. A change made
over Varlink is *merged* into the file on disk with tomlkit: only the keys it
changed are written, so comments, layout and hand edits to other keys survive.
Hand edits take effect with `pi-panel-ctl reload` (or SIGHUP, or a restart).

    schema = 1                    # the file's format version
    enabled = ["immich", "clock"]
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

    [mqtt]                        # the MQTT bridge (pi-panel-mqtt.service); its keys
    enabled = true                # are documented and checked in pi_panel_mqtt.config
    host = "mqtt.home.local"

Without a rotation named "default", ctl rotates through every enabled app for
`default_seconds` each, which is what a fresh install wants.

The file used to be called ctl.toml; ctl still reads that name when there is
no panel.toml (see `resolve_path`).
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit
import tomlkit.exceptions
from pi_panel_mqtt.config import ConfigError as MqttConfigError
from pi_panel_mqtt.config import validate as validate_mqtt

from .model import ClockSettings, Rotation, Schedule, ValidationError

log = logging.getLogger("pi-panel-ctl")

SCHEMA = 1
TRANSITIONS = ("fade", "cut")
KNOWN_KEYS = {"schema", "enabled", "selected_rotation", "default_seconds", "transition",
              "rotation", "schedule", "clock", "mqtt"}

HEADER = """\
# pi-panel's main configuration.
# Edit it by hand, then apply with `pi-panel-ctl reload`. Changes made through
# pi-panel-ctl are merged in, keeping comments and other hand edits.
"""


def resolve_path(config_home: Path) -> Path:
    """panel.toml, or the old ctl.toml when only that exists."""
    panel = config_home / "panel.toml"
    legacy = config_home / "ctl.toml"
    if not panel.exists() and legacy.exists():
        log.warning("reading %s: rename it to panel.toml", legacy)
        return legacy
    return panel


@dataclass
class CtlConfig:
    enabled: list[str] = field(default_factory=list)
    selected_rotation: str = "default"
    default_seconds: float = 60.0
    transition: str = "fade"
    rotations: dict[str, Rotation] = field(default_factory=dict)
    schedules: list[Schedule] = field(default_factory=list)
    clock: ClockSettings = field(default_factory=ClockSettings)
    # The [mqtt] table as written (validated by pi_panel_mqtt), or None: no bridge.
    # ctl only decides whether pi-panel-mqtt.service runs; the bridge reads it.
    mqtt: dict[str, Any] | None = None

    def mqtt_enabled(self) -> bool:
        return self.mqtt is not None and self.mqtt.get("enabled", True) is True

    # --- reading ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "CtlConfig":
        """The file's configuration, or the defaults when it does not exist.
        Anything wrong with it, from a TOML syntax error to a bad value, is a
        ValidationError naming the file."""
        if not path.exists():
            return cls()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"{path}: {exc}") from None
        try:
            return cls.from_dict(data)
        except ValidationError as exc:
            raise ValidationError(f"{path}: {exc}") from None
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            # A shape the checks did not foresee: still a config error, not a crash.
            raise ValidationError(f"{path}: malformed ({type(exc).__name__}: {exc})") from None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CtlConfig":
        unknown = set(data) - KNOWN_KEYS
        if unknown:
            # A typo'd key would otherwise be silently ignored.
            raise ValidationError(f"unknown key(s): {', '.join(sorted(unknown))}")

        schema = data.get("schema", SCHEMA)
        if isinstance(schema, bool) or not isinstance(schema, int) or schema < 1:
            raise ValidationError("schema must be a positive integer")
        if schema > SCHEMA:
            raise ValidationError(
                f"schema {schema} is newer than this pi-panel understands ({SCHEMA})")

        enabled = data.get("enabled", [])
        if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
            raise ValidationError("enabled must be a list of package names")
        selected = data.get("selected_rotation", "default")
        if not isinstance(selected, str) or not selected:
            raise ValidationError("selected_rotation must be a rotation name")
        seconds = data.get("default_seconds", 60.0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValidationError("default_seconds must be a number > 0")
        transition = data.get("transition", "fade")
        if transition not in TRANSITIONS:
            raise ValidationError("transition must be fade or cut")

        cfg = cls(enabled=list(enabled), selected_rotation=selected,
                  default_seconds=float(seconds), transition=transition)

        raw_clock = data.get("clock", {})
        if not isinstance(raw_clock, dict):
            raise ValidationError("clock must be a table")
        try:
            cfg.clock = ClockSettings.from_dict(raw_clock)
        except ValidationError as exc:
            raise ValidationError(f"clock: {exc}") from None

        for i, raw in enumerate(_tables(data, "rotation")):
            try:
                rot = Rotation.from_dict(raw)
            except ValidationError as exc:
                raise ValidationError(f"rotation {i + 1}: {exc}") from None
            if rot.name in cfg.rotations:
                raise ValidationError(f"duplicate rotation {rot.name!r}")
            cfg.rotations[rot.name] = rot

        if "mqtt" in data:
            try:
                validate_mqtt(data["mqtt"])
            except MqttConfigError as exc:
                raise ValidationError(str(exc)) from None
            cfg.mqtt = data["mqtt"]

        for i, raw in enumerate(_tables(data, "schedule")):
            try:
                sched = Schedule.from_dict(raw)
            except ValidationError as exc:
                raise ValidationError(f"schedule {i + 1}: {exc}") from None
            if any(s.name == sched.name for s in cfg.schedules):
                raise ValidationError(f"duplicate schedule {sched.name!r}")
            cfg.schedules.append(sched)
        return cfg

    # --- writing ---------------------------------------------------------

    def to_toml(self) -> dict[str, Any]:
        """Every setting, as plain data. Also the `base` that `save` compares with."""
        data: dict[str, Any] = {
            "schema": SCHEMA,
            "enabled": list(self.enabled),
            "selected_rotation": self.selected_rotation,
            "default_seconds": self.default_seconds,
            "transition": self.transition,
            "clock": self.clock.to_dict(),
            "rotation": [r.to_dict() for r in self.rotations.values()],
            "schedule": [s.to_config() for s in self.schedules],
        }
        if self.mqtt is not None:
            data["mqtt"] = self.mqtt
        return data

    def save(self, path: Path, base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Merge these settings into the file at `path` and return the new base.

        `base` is what the file held when it was last loaded or saved (the
        to_toml() of that config). Only keys whose value differs from it are
        written; the rest of the file, comments and hand edits included, is
        left as it is. Without a base, every key is written.

        Atomic: a power cut mid-write must not leave a truncated file. The new
        file is created 0600: it can hold credentials (the [mqtt] password).
        """
        new = self.to_toml()
        doc = _read_document(path)
        if "schema" not in doc:
            doc["schema"] = SCHEMA
        for key, value in new.items():
            if base is not None and base.get(key) == value:
                continue
            if key == "clock" and isinstance(doc.get("clock"), dict):
                _merge_table(doc["clock"], value)
            elif key in ("rotation", "schedule"):
                if value:
                    doc[key] = _aot(value)
                elif key in doc:
                    del doc[key]
            else:
                doc[key] = value
        _write_atomic(path, tomlkit.dumps(doc))
        return new


def _tables(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(t, dict) for t in raw):
        raise ValidationError(f"{key} must be [[{key}]] tables")
    return raw


def _read_document(path: Path) -> tomlkit.TOMLDocument:
    """The file as an editable document; a new one (with a header) when it
    does not exist. A file that does not parse is kept beside it as .bad."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return tomlkit.parse(HEADER)
    try:
        return tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as exc:
        bad = path.with_name(path.name + ".bad")
        log.error("%s does not parse (%s); keeping it as %s and writing a fresh one",
                  path, exc, bad)
        _write_atomic(bad, text)
        return tomlkit.parse(HEADER)


def _merge_table(table: Any, values: dict[str, Any]) -> None:
    """Update a table in place, so its comments survive."""
    for k in [k for k in table if k not in values]:
        del table[k]
    for k, v in values.items():
        if table.get(k) != v:
            table[k] = v


def _aot(rows: list[dict[str, Any]]) -> Any:
    """[[key]] tables, with lists of tables inside them written inline:
    entries = [{app = "a", seconds = 10}], not [[rotation.entries]] blocks."""
    aot = tomlkit.aot()
    for row in rows:
        table = tomlkit.table()
        for k, v in row.items():
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                arr = tomlkit.array()
                for x in v:
                    inline = tomlkit.inline_table()
                    inline.update(x)
                    arr.append(inline)
                table[k] = arr
            else:
                table[k] = v
        aot.append(table)
    return aot


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
