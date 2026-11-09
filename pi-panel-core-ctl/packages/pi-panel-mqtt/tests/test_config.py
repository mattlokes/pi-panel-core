"""[mqtt] in panel.toml: validation, loading and precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_panel_mqtt.config import ConfigError, config_path, load, validate

# pi-panel-core-ctl/panel.toml.example: the shipped example of the whole file.
EXAMPLE = Path(__file__).resolve().parents[3] / "panel.toml.example"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "panel.toml"
    p.write_text(text)
    return p


def test_shipped_example():
    s = load(EXAMPLE)
    assert s.enabled is True
    assert s.mqtt.host == "mqtt.home.local" and s.mqtt.port == 1883
    assert s.mqtt.username is None and s.mqtt.password is None   # "" means anonymous
    assert s.homeassistant.enabled is True
    assert s.log_level == "INFO"


def test_no_mqtt_table_gives_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("PI_PANEL_CTL_SOCKET", raising=False)
    s = load(write(tmp_path, 'enabled = ["photos"]\n'))
    assert s.mqtt.port == 1883
    assert s.ctl.resolved_socket() == "/run/pi-panel/ctl/io.pipanel.Ctl"


def test_missing_file_gives_defaults(tmp_path):
    assert load(tmp_path / "absent.toml").mqtt.port == 1883


@pytest.mark.parametrize("table, message", [
    ({"nope": 1}, "unknown key(s): nope"),
    ({"port": "1883"}, "port must be an integer 1-65535"),
    ({"port": 0}, "port must be an integer 1-65535"),
    ({"tls": "yes"}, "tls must be true or false"),
    ({"enabled": 1}, "enabled must be true or false"),
    ({"host": ""}, "host must not be empty"),
    ({"base_topic": "a/#"}, "base_topic must not contain + #"),
    ({"device_id": "a/b"}, "device_id must not contain /"),
    ({"keepalive": 1}, "keepalive must be an integer 5-3600"),
    ({"log_level": "LOUD"}, "log_level must be one of"),
    ({"homeassistant": {"prefix": "x"}}, "[mqtt.homeassistant] unknown key(s): prefix"),
    ({"homeassistant": "on"}, "[mqtt.homeassistant] must be a table"),
    ("on", "[mqtt] must be a table"),
])
def test_validate_rejects(table, message):
    with pytest.raises(ConfigError) as exc:
        validate(table)
    assert message in str(exc.value)


def test_load_errors_name_the_file(tmp_path):
    p = write(tmp_path, "[mqtt]\nport = 0\n")
    with pytest.raises(ConfigError, match=str(p)):
        load(p)
    p = write(tmp_path, "[mqtt\n")
    with pytest.raises(ConfigError, match=str(p)):
        load(p)


def test_env_overrides_file(tmp_path, monkeypatch):
    p = write(tmp_path, "[mqtt]\nhost = 'from-file'\nport = 1111\n")
    monkeypatch.setenv("PI_PANEL_MQTT_HOST", "from-env")
    monkeypatch.setenv("PI_PANEL_MQTT_PORT", "2222")
    s = load(p)
    assert s.mqtt.host == "from-env"
    assert s.mqtt.port == 2222  # coerced to int


def test_password_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_MQTT_PASSWORD", "s3cret")
    assert load(write(tmp_path, "[mqtt]\nusername = 'panel'\n")).mqtt.password == "s3cret"


def test_cli_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_MQTT_HOST", "from-env")
    s = load(tmp_path / "absent.toml", {"mqtt.host": "from-cli"})
    assert s.mqtt.host == "from-cli"


def test_unset_cli_flags_do_not_clobber(tmp_path):
    s = load(write(tmp_path, "[mqtt]\nhost = 'from-file'\n"), {"mqtt.host": None, "mqtt.port": None})
    assert s.mqtt.host == "from-file"


def test_env_bool_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_MQTT_DISCOVERY", "false")
    assert load(tmp_path / "absent.toml").homeassistant.enabled is False


def test_device_id_defaults_to_hostname(tmp_path):
    s = load(tmp_path / "absent.toml")
    assert s.mqtt.resolved_device_id()  # non-empty
    s2 = load(tmp_path / "absent.toml", {"mqtt.device_id": "jazz"})
    assert s2.mqtt.resolved_device_id() == "jazz"


def test_config_path_follows_ctl_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_CONFIG_HOME", str(tmp_path))
    assert config_path() == tmp_path / "panel.toml"
    (tmp_path / "ctl.toml").write_text("")
    assert config_path() == tmp_path / "ctl.toml"
    (tmp_path / "panel.toml").write_text("")
    assert config_path() == tmp_path / "panel.toml"
