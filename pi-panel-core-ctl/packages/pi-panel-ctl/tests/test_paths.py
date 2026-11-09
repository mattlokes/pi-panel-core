"""ctl and pluginman each have a paths.py (neither depends on the other).
They must agree on the layout, or ctl and pi-panel-pkg look in different places."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from pi_panel_ctl import paths

CORE = Path(__file__).resolve().parents[4]
PLUGINMAN_PATHS = CORE / "pi-panel-core-pluginman" / "src" / "pi_panel_pluginman" / "paths.py"

ENV = ("PI_PANEL_HOME", "PI_PANEL_CONFIG_HOME", "PI_PANEL_DATA_HOME", "PI_PANEL_RUNTIME_DIR")


def _pluginman_paths():
    spec = importlib.util.spec_from_file_location("pluginman_paths", PLUGINMAN_PATHS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_the_panel_layout(clean_env):
    assert paths.config_home() == Path("/persistent/pi-panel/config")
    assert paths.data_home() == Path("/persistent/pi-panel/state")
    assert paths.runtime_dir() == Path("/run/pi-panel")
    assert paths.registry() == Path("/persistent/pi-panel/state/packages.json")


def test_pluginman_agrees(clean_env):
    pm = _pluginman_paths()
    assert pm.home() == Path("/opt/pi-panel")
    for name in ("home", "config_home", "data_home", "runtime_dir", "registry",
                 "image_registry", "ctl_socket"):
        assert getattr(pm, name)() == getattr(paths, name)(), name
    assert pm.app_socket("x") == paths.app_socket("x")


def test_pluginman_agrees_under_overrides(monkeypatch, tmp_path):
    for name in ENV:
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    pm = _pluginman_paths()
    for name in ("home", "config_home", "data_home", "runtime_dir", "registry",
                 "image_registry", "ctl_socket"):
        assert getattr(pm, name)() == getattr(paths, name)(), name
