"""The [mqtt] table of panel.toml, merged as defaults < file < environment < CLI.

    [mqtt]
    enabled = true                 # ctl runs pi-panel-mqtt.service while true
    host = "mqtt.home.local"
    port = 1883
    username = ""                  # empty: anonymous
    password = ""                  # or PI_PANEL_MQTT_PASSWORD (a unit drop-in)
    base_topic = "pi-panel"
    device_id = ""                 # empty: the hostname
    keepalive = 30
    tls = false
    log_level = "INFO"

    [mqtt.homeassistant]
    enabled = true                 # publish Home Assistant discovery
    discovery_prefix = "homeassistant"

No [mqtt] table means no bridge. `validate()` is the one checker of the table:
ctl calls it when it loads panel.toml, so a bad key is refused by
`pi-panel-ctl reload` before the bridge ever restarts with it.
"""

from __future__ import annotations

import os
import socket
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

ENV_PREFIX = "PI_PANEL_MQTT_"
DEFAULT_CONFIG_HOME = "/persistent/pi-panel/config"   # as pi_panel_ctl.paths
DEFAULT_CTL_SOCKET = "/run/pi-panel/ctl/io.pipanel.Ctl"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    pass


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
    enabled: bool = True
    ctl: CtlSettings = field(default_factory=CtlSettings)
    mqtt: MqttSettings = field(default_factory=MqttSettings)
    homeassistant: HomeAssistantSettings = field(default_factory=HomeAssistantSettings)
    log_level: str = "INFO"


# --- validation -------------------------------------------------------------

def _str(key: str, value: Any, *, empty: bool = True, topic: bool = False) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"[mqtt] {key} must be a string")
    if not empty and not value:
        raise ConfigError(f"[mqtt] {key} must not be empty")
    if topic and any(c in value for c in "+#\0"):
        raise ConfigError(f"[mqtt] {key} must not contain + # or NUL (MQTT wildcards)")
    return value


def _int(key: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"[mqtt] {key} must be an integer {lo}-{hi}")
    return value


def _bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"[mqtt] {key} must be true or false")
    return value


_TOP = {"enabled", "host", "port", "username", "password", "base_topic", "device_id",
        "keepalive", "tls", "log_level", "homeassistant"}
_HA = {"enabled", "discovery_prefix"}


def validate(table: Any) -> Settings:
    """Settings from an [mqtt] table (environment and CLI not applied).
    Raises ConfigError for an unknown key or a bad value."""
    if not isinstance(table, dict):
        raise ConfigError("[mqtt] must be a table")
    unknown = set(table) - _TOP
    if unknown:
        raise ConfigError(f"[mqtt] unknown key(s): {', '.join(sorted(unknown))}")
    t = table
    d = MqttSettings()
    mqtt = MqttSettings(
        host=_str("host", t.get("host", d.host), empty=False),
        port=_int("port", t.get("port", d.port), 1, 65535),
        username=_str("username", t.get("username", "")) or None,
        password=_str("password", t.get("password", "")) or None,
        base_topic=_str("base_topic", t.get("base_topic", d.base_topic), empty=False, topic=True),
        device_id=_str("device_id", t.get("device_id", d.device_id), topic=True),
        keepalive=_int("keepalive", t.get("keepalive", d.keepalive), 5, 3600),
        tls=_bool("tls", t.get("tls", d.tls)),
    )
    if "/" in mqtt.device_id:
        raise ConfigError("[mqtt] device_id must not contain /")

    ha_table = t.get("homeassistant", {})
    if not isinstance(ha_table, dict):
        raise ConfigError("[mqtt.homeassistant] must be a table")
    unknown = set(ha_table) - _HA
    if unknown:
        raise ConfigError(f"[mqtt.homeassistant] unknown key(s): {', '.join(sorted(unknown))}")
    ha = HomeAssistantSettings(
        enabled=_bool("homeassistant.enabled", ha_table.get("enabled", True)),
        discovery_prefix=_str("homeassistant.discovery_prefix",
                              ha_table.get("discovery_prefix", "homeassistant"),
                              empty=False, topic=True),
    )

    level = _str("log_level", t.get("log_level", "INFO")).upper()
    if level not in LOG_LEVELS:
        raise ConfigError(f"[mqtt] log_level must be one of {', '.join(LOG_LEVELS)}")

    return Settings(enabled=_bool("enabled", t.get("enabled", True)),
                    mqtt=mqtt, homeassistant=ha, log_level=level)


# --- loading ----------------------------------------------------------------

def config_path() -> Path:
    """panel.toml in the config directory, or the old ctl.toml when only that
    exists (the same rule as pi-panel-ctl)."""
    home = Path(os.environ.get("PI_PANEL_CONFIG_HOME") or DEFAULT_CONFIG_HOME)
    panel = home / "panel.toml"
    legacy = home / "ctl.toml"
    return legacy if not panel.exists() and legacy.exists() else panel


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
_ENV_TYPES = {"port": int, "tls": bool, "enabled": bool}


def _coerce(field_name: str, raw: str) -> Any:
    target = _ENV_TYPES.get(field_name, str)
    if target is bool:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if target is int:
        try:
            return int(raw)
        except ValueError:
            raise ConfigError(f"{ENV_PREFIX}{field_name.upper()} must be an integer") from None
    return raw


def load(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """Settings from panel.toml's [mqtt], then the environment, then explicit
    overrides (parsed CLI flags; None values are skipped, so an unset flag
    never clobbers a lower layer). Raises ConfigError naming the file."""
    cfg_path = path or config_path()
    table: Any = {}
    if cfg_path.is_file():
        try:
            data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise ConfigError(f"{cfg_path}: {exc}") from None
        table = data.get("mqtt", {})
    try:
        settings = validate(table)
    except ConfigError as exc:
        raise ConfigError(f"{cfg_path}: {exc}") from None

    for suffix, (section_name, field_name) in _ENV_MAP.items():
        raw = os.environ.get(ENV_PREFIX + suffix)
        if raw is None:
            continue
        if section_name is None:
            settings = replace(settings, **{field_name: raw.upper()})
            continue
        current = getattr(settings, section_name)
        settings = replace(settings, **{section_name: replace(
            current, **{field_name: _coerce(field_name, raw)})})

    for dotted, value in (overrides or {}).items():
        if value is None:
            continue
        section_name, _, field_name = dotted.partition(".")
        if not field_name:
            settings = replace(settings, **{section_name: value})
            continue
        current = getattr(settings, section_name)
        settings = replace(settings, **{section_name: replace(current, **{field_name: value})})
    return settings
