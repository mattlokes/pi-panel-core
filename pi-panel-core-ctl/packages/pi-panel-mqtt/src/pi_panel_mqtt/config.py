"""Settings, merged from defaults < TOML file < environment < CLI flags."""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# pi-panel-run points PI_PANEL_CONFIG_DIR at this plugin's own config directory.
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("PI_PANEL_CONFIG_DIR")
    or Path.home() / ".config" / "pi-panel" / "plugins" / "mqtt"
) / "mqtt.toml"
ENV_PREFIX = "PI_PANEL_MQTT_"
DEFAULT_CTL_SOCKET = "/run/pi-panel/ctl/io.pipanel.Ctl"


@dataclass(frozen=True, slots=True)
class CtlSettings:
    socket_path: str = ""  # empty -> $PI_PANEL_CTL_SOCKET -> DEFAULT_CTL_SOCKET

    def resolved_socket(self) -> str:
        return self.socket_path or os.environ.get("PI_PANEL_CTL_SOCKET") or DEFAULT_CTL_SOCKET


@dataclass(frozen=True, slots=True)
class MqttSettings:
    host: str = "mqtt.home.local"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    base_topic: str = "pi-panel"
    device_id: str = ""  # empty -> hostname
    keepalive: int = 30
    tls: bool = False

    def resolved_device_id(self) -> str:
        return self.device_id or socket.gethostname().split(".")[0]


@dataclass(frozen=True, slots=True)
class HomeAssistantSettings:
    enabled: bool = True
    discovery_prefix: str = "homeassistant"


@dataclass(frozen=True, slots=True)
class Settings:
    ctl: CtlSettings = field(default_factory=CtlSettings)
    mqtt: MqttSettings = field(default_factory=MqttSettings)
    homeassistant: HomeAssistantSettings = field(default_factory=HomeAssistantSettings)
    log_level: str = "INFO"


_SECTIONS: dict[str, type] = {
    "ctl": CtlSettings,
    "mqtt": MqttSettings,
    "homeassistant": HomeAssistantSettings,
}

# env var name -> (section, field). Section None means top level.
_ENV_MAP: dict[str, tuple[str | None, str]] = {
    "HOST": ("mqtt", "host"),
    "PORT": ("mqtt", "port"),
    "USERNAME": ("mqtt", "username"),
    "PASSWORD": ("mqtt", "password"),
    "BASE_TOPIC": ("mqtt", "base_topic"),
    "DEVICE_ID": ("mqtt", "device_id"),
    "TLS": ("mqtt", "tls"),
    "CTL_SOCKET": ("ctl", "socket_path"),
    "DISCOVERY": ("homeassistant", "enabled"),
    "DISCOVERY_PREFIX": ("homeassistant", "discovery_prefix"),
    "LOG_LEVEL": (None, "log_level"),
}


def _coerce(target_type: Any, raw: str) -> Any:
    if target_type is bool or target_type == "bool":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if target_type is int:
        return int(raw)
    if target_type is float:
        return float(raw)
    return raw


def _field_type(cls: type, name: str) -> Any:
    hints = {f.name: f.type for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    t = hints.get(name, str)
    # Dataclass field types are strings under `from __future__ import annotations`.
    if isinstance(t, str):
        if t.startswith("bool"):
            return bool
        if t.startswith("int"):
            return int
        if t.startswith("float"):
            return float
        return str
    return t


def load(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """Build Settings from file, environment, then explicit overrides.

    `overrides` carries parsed CLI flags. Only keys whose value is not None are
    applied, so an unset flag never clobbers a lower-precedence layer.
    """
    settings = Settings()

    # --- file ---
    cfg_path = path or DEFAULT_CONFIG_PATH
    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
        for section_name, section_cls in _SECTIONS.items():
            if section_name not in data:
                continue
            block = data[section_name]
            known = {f.name for f in section_cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
            unknown = set(block) - known
            if unknown:
                raise ValueError(
                    f"{cfg_path}: unknown key(s) in [{section_name}]: "
                    f"{', '.join(sorted(unknown))}"
                )
            current = getattr(settings, section_name)
            settings = replace(
                settings, **{section_name: replace(current, **block)}
            )
        if "log_level" in data:
            settings = replace(settings, log_level=data["log_level"])

    # --- environment ---
    for suffix, (section_name, field_name) in _ENV_MAP.items():
        raw = os.environ.get(ENV_PREFIX + suffix)
        if raw is None:
            continue
        if section_name is None:
            settings = replace(settings, **{field_name: raw})
            continue
        current = getattr(settings, section_name)
        value = _coerce(_field_type(type(current), field_name), raw)
        settings = replace(
            settings, **{section_name: replace(current, **{field_name: value})}
        )

    # --- CLI ---
    for dotted, value in (overrides or {}).items():
        if value is None:
            continue
        section_name, _, field_name = dotted.partition(".")
        if not field_name:
            settings = replace(settings, **{section_name: value})
            continue
        current = getattr(settings, section_name)
        settings = replace(
            settings, **{section_name: replace(current, **{field_name: value})}
        )

    return settings
