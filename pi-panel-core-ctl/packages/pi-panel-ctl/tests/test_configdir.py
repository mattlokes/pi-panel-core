"""`pi-panel-ctl config check|export|import`."""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import pytest

from pi_panel_ctl import configdir
from pi_panel_ctl.cli import build_parser, run
from pi_panel_ctl.packages import Package

from .test_daemon import APPS, Panel, workdir  # noqa: F401  (fixture)

# A check that passes when the app's config.toml says good = true.
CHECK = (f"{sys.executable} -c \"import sys, tomllib; "
         "c = tomllib.load(open(sys.argv[1], 'rb')); "
         "sys.exit(0 if c.get('good') else 'not good: ' + sys.argv[1])\" "
         "${config_dir}/config.toml")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_PANEL_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PI_PANEL_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PI_PANEL_HOME", str(tmp_path / "opt"))
    config = tmp_path / "config"
    (config / "apps" / "photos").mkdir(parents=True)
    (config / "apps" / "photos" / "config.toml").write_text("good = true\n")
    (config / "panel.toml").write_text('# mine\nenabled = ["photos", "ghost"]\n')
    (config / "compositor.env").write_text("WLR_RENDERER=pixman\n")
    return config


def packages(tmp_path: Path) -> dict[str, Package]:
    return {"photos": Package("photos", "app", path=str(tmp_path), check=CHECK),
            "clock": Package("clock", "app", path=str(tmp_path))}


def by_name(results):
    return {r.name: r.status for r in results}


def test_check_runs_each_enabled_package(home, tmp_path):
    results = configdir.check(home, packages(tmp_path))
    assert by_name(results) == {"panel.toml": "ok", "photos": "ok", "ghost": "warning"}
    (home / "apps" / "photos" / "config.toml").write_text("good = false\n")
    results = configdir.check(home, packages(tmp_path))
    assert by_name(results)["photos"] == "failed"
    assert "not good" in next(r.detail for r in results if r.name == "photos")
    assert configdir.failed(results)


def test_check_reports_a_bad_panel_toml(home, tmp_path):
    (home / "panel.toml").write_text("[mqtt]\nport = 'x'\n")
    results = configdir.check(home, packages(tmp_path))
    assert results[0].status == "failed" and "port must be an integer" in results[0].detail


def test_export_import_round_trip(home, tmp_path):
    (home / "panel.toml").chmod(0o600)        # it can hold the MQTT password
    first = tmp_path / "a.tar.gz"
    configdir.export(home, first)
    results, backup = configdir.install(home, first, packages(tmp_path), tmp_path / "backups")
    assert backup is not None and backup.exists()
    second = tmp_path / "b.tar.gz"
    configdir.export(home, second)

    def contents(path):
        with tarfile.open(path) as tar:
            return {m.name: tar.extractfile(m).read() if m.isfile() else None
                    for m in tar.getmembers()}
    assert contents(first) == contents(second)
    assert (home / "panel.toml").read_text().startswith("# mine")
    assert (home / "panel.toml").stat().st_mode & 0o777 == 0o600


def test_import_that_fails_its_checks_changes_nothing(home, tmp_path):
    other = tmp_path / "other"
    (other / "apps" / "photos").mkdir(parents=True)
    (other / "apps" / "photos" / "config.toml").write_text("good = false\n")
    (other / "panel.toml").write_text('enabled = ["photos"]\n')
    archive = tmp_path / "bad.tar.gz"
    configdir.export(other, archive)
    results, backup = configdir.install(home, archive, packages(tmp_path), tmp_path / "backups")
    assert backup is None and configdir.failed(results)
    assert (home / "apps" / "photos" / "config.toml").read_text() == "good = true\n"
    assert not (tmp_path / "backups").exists()


def test_import_refuses_paths_outside_the_tree(home, tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 0
        tar.addfile(info)
    with pytest.raises(configdir.ValidationError, match="cannot unpack"):
        configdir.install(home, evil, packages(tmp_path), tmp_path / "backups")
    assert not (home.parent / "escape.txt").exists()


async def test_cli_import_reloads_ctld(workdir, monkeypatch, capsys, tmp_path):  # noqa: F811
    async with Panel(workdir, APPS, enabled=["photos"]) as p:
        config = workdir / "config"
        monkeypatch.setenv("PI_PANEL_CONFIG_HOME", str(config))
        monkeypatch.setenv("PI_PANEL_DATA_HOME", str(workdir / "state"))
        monkeypatch.setenv("PI_PANEL_HOME", str(workdir / "opt"))
        p.ctl.config_path = config / "panel.toml"
        src = tmp_path / "src"
        src.mkdir()
        (src / "panel.toml").write_text('enabled = ["photos", "cam"]\n')
        archive = tmp_path / "c.tar.gz"
        configdir.export(src, archive)
        argv = ["--socket", p.ctl_path, "config", "import", str(archive)]
        assert await run(build_parser().parse_args(argv)) == 0
        out = capsys.readouterr().out
        assert "imported into" in out and "reloaded" in out
        assert p.ctl.config.enabled == ["photos", "cam"]
