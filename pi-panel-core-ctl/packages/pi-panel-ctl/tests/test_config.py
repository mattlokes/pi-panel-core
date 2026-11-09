"""panel.toml: loading, validation, and saves that keep a hand-edited file intact."""

from __future__ import annotations

import os
import stat
import tomllib

import pytest

from pi_panel_ctl.config import CtlConfig, resolve_path
from pi_panel_ctl.model import Rotation, RotationEntry, ValidationError

HAND_EDITED = """\
# My panel. Photos by day, nothing at night.
schema = 1
enabled = ["photos", "clock"]   # clock is the fallback
transition = "fade"

[clock]
# big and in the corner
enabled = true
size = 120
"""


def test_missing_file_gives_defaults(tmp_path):
    cfg = CtlConfig.load(tmp_path / "panel.toml")
    assert cfg.enabled == [] and cfg.transition == "fade"


@pytest.mark.parametrize("text, message", [
    ('enabled = ["a"\n', "panel.toml"),                       # TOML syntax error
    ("colour = 1\n", "unknown key(s): colour"),
    ("schema = 2\n", "newer than this pi-panel"),
    ('enabled = "photos"\n', "enabled must be a list"),
    ("default_seconds = 0\n", "default_seconds must be a number > 0"),
    ('transition = "wipe"\n', "transition must be fade or cut"),
    ("rotation = 3\n", "rotation must be [[rotation]] tables"),
    ('[[rotation]]\nname = "a"\nentries = [{app = "x", seconds = 0}]\n', "rotation 1: entry 0"),
    ('[[rotation]]\nname = "a"\n[[rotation]]\nname = "a"\n', "duplicate rotation 'a'"),
    ('[clock]\ncolour = "red"\n', "clock: unknown clock key"),
    ('[[schedule]]\nname = "n"\n', "schedule 1:"),
])
def test_bad_files_are_validation_errors_naming_the_file(tmp_path, text, message):
    path = tmp_path / "panel.toml"
    path.write_text(text)
    with pytest.raises(ValidationError) as exc:
        CtlConfig.load(path)
    assert str(path) in str(exc.value) and message in str(exc.value)


def test_save_keeps_comments_and_untouched_keys(tmp_path):
    path = tmp_path / "panel.toml"
    path.write_text(HAND_EDITED)
    cfg = CtlConfig.load(path)
    base = cfg.to_toml()
    cfg.enabled.append("cam")
    cfg.rotations["evening"] = Rotation("evening", (RotationEntry("clock", 30),))
    base = cfg.save(path, base)

    text = path.read_text()
    assert "# My panel. Photos by day, nothing at night." in text
    assert "# big and in the corner" in text
    assert 'entries = [{app = "clock", seconds = 30}]' in text   # inline, readable
    again = CtlConfig.load(path)
    assert again.enabled == ["photos", "clock", "cam"]
    assert again.clock.size == 120
    assert list(again.rotations) == ["evening"]
    assert base == again.to_toml()


def test_save_keeps_hand_edits_made_since_load(tmp_path):
    """ctl only writes what it changed: a key edited by hand since ctl loaded the
    file (and not changed by ctl) is left alone."""
    path = tmp_path / "panel.toml"
    path.write_text(HAND_EDITED)
    cfg = CtlConfig.load(path)
    base = cfg.to_toml()
    path.write_text(path.read_text().replace('transition = "fade"', 'transition = "cut"'))
    cfg.enabled.remove("clock")
    cfg.save(path, base)
    again = CtlConfig.load(path)
    assert again.transition == "cut" and again.enabled == ["photos"]


def test_removing_the_last_rotation_removes_the_table(tmp_path):
    path = tmp_path / "panel.toml"
    cfg = CtlConfig(rotations={"a": Rotation("a", (RotationEntry("x", 5),))})
    base = cfg.save(path)
    del cfg.rotations["a"]
    cfg.save(path, base)
    assert "rotation" not in tomllib.loads(path.read_text())


def test_new_file_has_schema_header_and_mode_0600(tmp_path):
    path = tmp_path / "sub" / "panel.toml"
    CtlConfig(enabled=["photos"]).save(path)
    text = path.read_text()
    assert text.startswith("# pi-panel's main configuration.")
    assert tomllib.loads(text)["schema"] == 1
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_unparseable_file_is_kept_aside_on_save(tmp_path):
    path = tmp_path / "panel.toml"
    path.write_text("enabled = [\n")
    CtlConfig(enabled=["photos"]).save(path)
    assert (tmp_path / "panel.toml.bad").read_text() == "enabled = [\n"
    assert CtlConfig.load(path).enabled == ["photos"]


def test_resolve_path_prefers_panel_toml_and_falls_back_to_ctl_toml(tmp_path):
    assert resolve_path(tmp_path) == tmp_path / "panel.toml"
    (tmp_path / "ctl.toml").write_text("")
    assert resolve_path(tmp_path) == tmp_path / "ctl.toml"
    (tmp_path / "panel.toml").write_text("")
    assert resolve_path(tmp_path) == tmp_path / "panel.toml"
