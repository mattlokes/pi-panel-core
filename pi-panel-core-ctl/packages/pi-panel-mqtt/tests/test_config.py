"""Config loading and precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_panel_mqtt.config import load

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "config" / "mqtt.toml.example"


def test_shipped_example_parses_into_the_right_sections():
    """Regression: `log_level` sat after [homeassistant] and TOML scoped it
    into that section, so the shipped example was rejected at startup."""
    s = load(EXAMPLE)
    assert s.log_level == "INFO"
    assert s.mqtt.host == "mqtt.home.local"
    assert s.ctl.socket_path == ""
    assert s.homeassistant.enabled is True


def test_defaults_when_no_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PI_PANEL_CTL_SOCKET", raising=False)
    s = load(tmp_path / "absent.toml")
    assert s.mqtt.port == 1883
    assert s.ctl.resolved_socket() == "/run/pi-panel/ctl/io.pipanel.Ctl"


def test_ctl_socket_from_pi_panel_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_CTL_SOCKET", "/x/ctl")
    assert load(tmp_path / "absent.toml").ctl.resolved_socket() == "/x/ctl"


def test_unknown_key_is_rejected(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text("[mqtt]\nhost = 'x'\nnope = 1\n")
    with pytest.raises(ValueError, match="unknown key"):
        load(p)


def test_env_overrides_file(tmp_path: Path, monkeypatch):
    p = tmp_path / "c.toml"
    p.write_text("[mqtt]\nhost = 'from-file'\nport = 1111\n")
    monkeypatch.setenv("PI_PANEL_MQTT_HOST", "from-env")
    monkeypatch.setenv("PI_PANEL_MQTT_PORT", "2222")
    s = load(p)
    assert s.mqtt.host == "from-env"
    assert s.mqtt.port == 2222  # coerced to int


def test_cli_overrides_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_MQTT_HOST", "from-env")
    s = load(tmp_path / "absent.toml", {"mqtt.host": "from-cli"})
    assert s.mqtt.host == "from-cli"


def test_unset_cli_flags_do_not_clobber(tmp_path: Path):
    p = tmp_path / "c.toml"
    p.write_text("[mqtt]\nhost = 'from-file'\n")
    s = load(p, {"mqtt.host": None, "mqtt.port": None})
    assert s.mqtt.host == "from-file"


def test_env_bool_coercion(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_MQTT_DISCOVERY", "false")
    s = load(tmp_path / "absent.toml")
    assert s.homeassistant.enabled is False


def test_device_id_defaults_to_hostname(tmp_path: Path):
    s = load(tmp_path / "absent.toml")
    assert s.mqtt.resolved_device_id()  # non-empty
    s2 = load(tmp_path / "absent.toml", {"mqtt.device_id": "jazz"})
    assert s2.mqtt.resolved_device_id() == "jazz"
