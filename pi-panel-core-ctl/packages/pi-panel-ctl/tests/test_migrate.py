"""The move from ~/ to /opt + /persistent: step 1, the config."""

from __future__ import annotations

import stat
import tomllib

import pytest

from pi_panel_ctl import migrate
from pi_panel_ctl.config import CtlConfig

CTL_TOML = """\
# Managed by pi-panel-ctl; changes over Varlink are written back here.
enabled = [
    "immich",
    "mqtt",
    "webrtc-client",
    "now-playing",
]
selected_rotation = "default"
default_seconds = 60.0
transition = "fade"

[clock]
enabled = true
format = "%H:%M"
position = "bottom_left"
size = 150
"""

MQTT_TOML = """\
log_level = "INFO"

[ctl]
socket_path = ""

[mqtt]
host = "mqtt.home.local"
port = 1883
username = ""
password = ""
base_topic = "pi-panel"
device_id = ""
keepalive = 30
tls = false

[homeassistant]
enabled = true
discovery_prefix = "homeassistant"
"""


@pytest.fixture
def old(tmp_path, monkeypatch):
    """A config directory shaped like jazz's on 2026-11-09."""
    home = tmp_path / "old"
    (home / "plugins" / "mqtt").mkdir(parents=True)
    (home / "apps" / "now-playing").mkdir(parents=True)
    (home / "apps" / "immich").mkdir(parents=True)
    (home / "ctl.toml").write_text(CTL_TOML)
    (home / "plugins" / "mqtt" / "mqtt.toml").write_text(MQTT_TOML)
    secret = home / "apps" / "now-playing" / "config.toml"
    secret.write_text('backend = "home_assistant"\n')
    secret.chmod(0o600)
    (home / "apps" / "immich" / "config.yaml").write_text("immich_url: 'http://p'\n")
    etc = tmp_path / "compositor.env"
    etc.write_text("# Environment for pi-panel-compositor.service\n")
    monkeypatch.setenv("PI_PANEL_CONFIG_HOME", str(tmp_path / "new"))
    monkeypatch.setenv("PI_PANEL_HOME", str(tmp_path / "opt"))
    return home, etc, tmp_path / "new"


def test_plan_converts_ctl_and_mqtt_toml(old):
    home, etc, new = old
    plan, cfg = migrate.plan_config(home, new, etc)
    assert cfg.enabled == ["immich", "webrtc-client", "now-playing"]      # mqtt left
    assert cfg.mqtt == {"enabled": True, "log_level": "INFO", "host": "mqtt.home.local",
                        "port": 1883, "username": "", "password": "", "base_topic": "pi-panel",
                        "device_id": "", "keepalive": 30, "tls": False,
                        "homeassistant": {"enabled": True, "discovery_prefix": "homeassistant"}}
    assert cfg.clock.size == 150
    data = tomllib.loads(plan.panel_toml)
    assert data["schema"] == 1
    assert "Managed by pi-panel-ctl" not in plan.panel_toml
    assert plan.compositor_env is None                     # it set nothing
    assert [d.name for _, d in plan.copies] == ["immich", "now-playing"]  # not plugins/mqtt


def test_write_config_keeps_modes_and_validates(old):
    home, etc, new = old
    plan, _ = migrate.plan_config(home, new, etc)
    migrate.write_config(plan, new)
    assert stat.S_IMODE((new / "panel.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((new / "apps" / "now-playing" / "config.toml").stat().st_mode) == 0o600
    assert CtlConfig.load(new / "panel.toml").mqtt_enabled()
    assert not (new / "plugins").exists()


def test_compositor_env_with_settings_is_carried_over(old):
    home, etc, new = old
    etc.write_text("WLR_RENDERER=pixman\n")
    plan, _ = migrate.plan_config(home, new, etc)
    assert plan.compositor_env == "WLR_RENDERER=pixman\n"


def test_a_bad_mqtt_toml_stops_before_writing(old):
    home, etc, new = old
    (home / "plugins" / "mqtt" / "mqtt.toml").write_text("[mqtt]\nport = 'x'\n")
    with pytest.raises(migrate.MigrationError, match="port must be an integer"):
        migrate.plan_config(home, new, etc)
    assert not new.exists()


def test_main_config_only_and_refuses_to_run_twice(old, capsys):
    home, etc, new = old
    argv = ["--from-config", str(home), "--from-registry", str(home / "none.json"),
            "--etc-env", str(etc), "--config-only"]
    assert migrate.main([*argv, "--dry-run"]) == 0
    assert not (new / "panel.toml").exists()
    assert migrate.main(argv) == 0
    assert (new / "panel.toml").exists()
    assert migrate.main(argv) == 1
    assert "already migrated" in capsys.readouterr().err
