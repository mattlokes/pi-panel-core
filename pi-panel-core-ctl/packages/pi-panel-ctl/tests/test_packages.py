"""The image registry and the state registry, merged."""

from __future__ import annotations

import json
import logging

from pi_panel_ctl.packages import load_registries


def write(path, **packages):
    path.write_text(json.dumps({"version": 1, "packages": {
        n: {"name": n, **e} for n, e in packages.items()}}))


def test_merge_with_the_image_winning_a_clash(tmp_path, caplog):
    image, state = tmp_path / "image.json", tmp_path / "state.json"
    write(image, immich={"kind": "app", "path": "/opt/pi-panel/pi-panel-apps/immich"})
    write(state, immich={"kind": "app", "path": "/elsewhere"},
          extra={"kind": "plugin", "path": "/persistent/x"})
    with caplog.at_level(logging.WARNING):
        pkgs = load_registries(image, state)
    assert pkgs["immich"].origin == "image" and pkgs["immich"].path.startswith("/opt/")
    assert pkgs["extra"].origin == "state"
    assert "both package registries" in caplog.text


def test_missing_registries_mean_nothing_installed(tmp_path):
    assert load_registries(tmp_path / "no-image.json", tmp_path / "no-state.json") == {}
    assert load_registries(None, tmp_path / "no-state.json") == {}
