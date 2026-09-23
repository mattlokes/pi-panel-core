"""Manifest validation, installs from git/tarball/directory, pi-panel-run."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from pi_panel_pluginman import paths, run, scaffold
from pi_panel_pluginman.manager import Manager, PackageError, parse_source
from pi_panel_pluginman.manifest import Manifest, ManifestError

MANIFEST = """\
[package]
name = "{name}"
kind = "{kind}"
version = "{version}"
description = "test package"

[run]
exec = "python3 main.py --config ${{config_dir}}/conf.yaml --name ${{name}}"
env = {{ FOO = "bar", PI_PANEL_NAME = "hijack" }}

[build]
command = "{build}"

[config]
templates = ["conf.yaml.example"]
"""


@pytest.fixture
def panel_home(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="pm", dir="/tmp"))
    for var, sub in (("PI_PANEL_HOME", "home"), ("PI_PANEL_CONFIG_HOME", "config"),
                     ("PI_PANEL_DATA_HOME", "data"), ("PI_PANEL_RUNTIME_DIR", "run")):
        monkeypatch.setenv(var, str(root / sub))
    yield root
    shutil.rmtree(root, ignore_errors=True)


def make_package(where: Path, name: str = "demo", kind: str = "app", version: str = "1.0",
                 build: str = "echo building && echo ok > built.txt") -> Path:
    where.mkdir(parents=True, exist_ok=True)
    (where / "pi-panel.toml").write_text(MANIFEST.format(name=name, kind=kind, version=version,
                                                         build=build))
    (where / "main.py").write_text("print('hi')\n")
    (where / "conf.yaml.example").write_text("setting: 1\n")
    return where


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(where: Path, **kw) -> Path:
    make_package(where, **kw)
    git(where, "init", "-q")
    git(where, "add", "-A")
    git(where, "commit", "-q", "-m", "v1")
    return where


class Lines(list):
    def __call__(self, line: str) -> None:
        self.append(line)


# --- manifest -------------------------------------------------------------

def test_manifest_round_trip(tmp_path):
    m = Manifest.load(make_package(tmp_path))
    assert (m.name, m.kind, m.version) == ("demo", "app", "1.0")
    assert m.argv({"config_dir": "/c", "name": "demo", "package_dir": "/p", "runtime_dir": "/r"}) == \
        ["python3", "main.py", "--config", "/c/conf.yaml", "--name", "demo"]


@pytest.mark.parametrize("patch,message", [
    ({"package": {"name": "Bad Name", "kind": "app"}}, "lowercase"),
    ({"package": {"name": "x", "kind": "widget"}}, "kind"),
    ({"run": {"exec": ""}}, "run.exec"),
    ({"run": {"exec": "a ${nope}"}}, "unknown variable"),
    ({"run": {"exec": "a 'unclosed"}}, "run.exec"),
    ({"config": {"templates": ["../../etc/passwd"]}}, "inside the package"),
    ({"extra": {}}, "unknown section"),
    ({"package": {"name": "x", "kind": "plugin"}, "app": {"varlink": True}}, "only for kind"),
])
def test_manifest_rejects(patch, message):
    data = {"package": {"name": "x", "kind": "app"}, "run": {"exec": "true"}} | patch
    with pytest.raises(ManifestError, match=message):
        Manifest.from_dict(data)


def test_missing_manifest(tmp_path):
    with pytest.raises(ManifestError, match="no pi-panel.toml"):
        Manifest.load(tmp_path)


@pytest.mark.parametrize("text,expected", [
    ("https://github.com/x/y.git", ("git", "https://github.com/x/y.git", None)),
    ("https://github.com/x/y.git#v1.2", ("git", "https://github.com/x/y.git", "v1.2")),
    ("git@github.com:x/y.git", ("git", "git@github.com:x/y.git", None)),
    ("https://example.com/x-1.0.tar.gz", ("tarball", "https://example.com/x-1.0.tar.gz", None)),
    ("./release.tgz", ("tarball", "./release.tgz", None)),
])
def test_parse_source(text, expected):
    spec = parse_source(text)
    assert (spec.type, spec.url, spec.ref) == expected


# --- install / update / remove ------------------------------------------

async def test_install_from_git_repo(panel_home, tmp_path):
    repo = make_repo(tmp_path / "src")
    out = Lines()
    entry = await Manager(output=out).install(f"file://{repo}")
    dest = paths.package_dir("app", "demo")
    assert dest == panel_home / "home" / "pi-panel-apps" / "demo"
    assert (dest / "built.txt").read_text() == "ok\n"               # build ran in place
    assert "building" in out
    assert entry.source.type == "git" and len(entry.source.commit) == 40
    assert (paths.package_config_dir("app", "demo") / "conf.yaml").read_text() == "setting: 1\n"

    reg = json.loads(paths.registry().read_text())
    assert reg["packages"]["demo"]["path"] == str(dest)
    assert reg["packages"]["demo"]["kind"] == "app"


async def test_install_git_ref(panel_home, tmp_path):
    repo = make_repo(tmp_path / "src", version="1.0")
    git(repo, "tag", "v1")
    (repo / "pi-panel.toml").write_text((repo / "pi-panel.toml").read_text().replace("1.0", "2.0"))
    git(repo, "commit", "-qam", "v2")
    entry = await Manager(output=Lines()).install(f"{repo}#v1")
    assert entry.version == "1.0"


async def test_update_keeps_config_and_changes_version(panel_home, tmp_path):
    repo = make_repo(tmp_path / "src", version="1.0")
    mgr = Manager(output=Lines())
    await mgr.install(f"file://{repo}")
    cfg = paths.package_config_dir("app", "demo") / "conf.yaml"
    cfg.write_text("setting: 42\n")          # the user edited it
    (repo / "pi-panel.toml").write_text((repo / "pi-panel.toml").read_text().replace("1.0", "1.1"))
    git(repo, "commit", "-qam", "v1.1")
    updated = await mgr.update("demo")
    assert updated.version == "1.1"
    assert cfg.read_text() == "setting: 42\n"


async def test_failed_build_restores_previous_version(panel_home, tmp_path):
    repo = make_repo(tmp_path / "src", version="1.0")
    mgr = Manager(output=Lines())
    await mgr.install(f"file://{repo}")
    (repo / "pi-panel.toml").write_text(MANIFEST.format(
        name="demo", kind="app", version="2.0", build="echo nope && exit 3"))
    git(repo, "commit", "-qam", "broken")
    with pytest.raises(PackageError, match="exit status 3"):
        await mgr.install(f"file://{repo}")
    dest = paths.package_dir("app", "demo")
    assert Manifest.load(dest).version == "1.0"
    assert mgr.registry.get("demo").version == "1.0"


async def test_failed_first_install_leaves_nothing(panel_home, tmp_path):
    src = make_package(tmp_path / "src", build="exit 1")
    with pytest.raises(PackageError):
        await Manager(output=Lines()).install(str(src))
    assert not paths.package_dir("app", "demo").exists()
    assert not paths.registry().exists()


def _tarball(path: Path, members: dict[str, bytes], top: str | None = "demo-1.0") -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"{top}/{name}" if top else name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


async def test_local_directory_copies_uncommitted_work(panel_home, tmp_path):
    repo = make_repo(tmp_path / "src", version="1.0")
    (repo / "pi-panel.toml").write_text((repo / "pi-panel.toml").read_text().replace("1.0", "1.1-dev"))
    (repo / ".gitignore").write_text("secret.yaml\n")
    (repo / "secret.yaml").write_text("api_key: hunter2\n")
    (repo / "new_file.py").write_text("x = 1\n")           # untracked, not ignored
    entry = await Manager(output=Lines()).install(str(repo))
    dest = paths.package_dir("app", "demo")
    assert entry.source.type == "local" and entry.version == "1.1-dev"
    assert not (dest / ".git").exists()
    assert (dest / "new_file.py").exists()
    assert not (dest / "secret.yaml").exists(), "gitignored secrets must not be copied"


async def test_install_from_tarball_with_top_level_dir(panel_home, tmp_path):
    pkg = make_package(tmp_path / "pkg", kind="plugin", build="true")
    archive = _tarball(tmp_path / "demo.tar.gz",
                       {p.name: p.read_bytes() for p in pkg.iterdir()})
    entry = await Manager(output=Lines()).install(str(archive))
    assert entry.kind == "plugin" and entry.source.type == "tarball"
    assert len(entry.source.sha256) == 64
    assert (paths.package_dir("plugin", "demo") / "main.py").exists()


async def test_tarball_path_traversal_is_refused(panel_home, tmp_path):
    archive = _tarball(tmp_path / "evil.tar.gz", {"../../../../tmp/pwned": b"x"}, top=None)
    with pytest.raises(PackageError, match="cannot unpack"):
        await Manager(output=Lines()).install(str(archive))


async def test_not_a_package(panel_home, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(PackageError, match="not a pi-panel package"):
        await Manager(output=Lines()).install(str(tmp_path / "empty"))


async def test_kind_clash(panel_home, tmp_path):
    await Manager(output=Lines()).install(str(make_package(tmp_path / "a", kind="app", build="true")))
    with pytest.raises(PackageError, match="already installed as a app"):
        await Manager(output=Lines()).install(
            str(make_package(tmp_path / "b", kind="plugin", build="true")))


async def test_confirm_can_cancel(panel_home, tmp_path):
    async def no(manifest):
        return False
    with pytest.raises(PackageError, match="cancelled"):
        await Manager(output=Lines()).install(str(make_package(tmp_path / "a")), confirm=no)
    assert not paths.package_dir("app", "demo").exists()


async def test_remove(panel_home, tmp_path):
    mgr = Manager(output=Lines())
    await mgr.install(str(make_package(tmp_path / "a", build="true")))
    await mgr.remove("demo", purge=True)
    assert not paths.package_dir("app", "demo").exists()
    assert not paths.package_config_dir("app", "demo").exists()
    assert mgr.registry.get("demo") is None


# --- pi-panel-run -----------------------------------------------------------

async def test_run_builds_environment_and_argv(panel_home, tmp_path, monkeypatch):
    await Manager(output=Lines()).install(str(make_package(tmp_path / "a", build="true")))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    package_dir, argv, env = run.build("app", "demo")
    assert package_dir == paths.package_dir("app", "demo")
    config_dir = paths.package_config_dir("app", "demo")
    assert argv == ["python3", "main.py", "--config", f"{config_dir}/conf.yaml", "--name", "demo"]
    assert env["FOO"] == "bar"
    assert env["PI_PANEL_NAME"] == "demo"                   # the manifest cannot override it
    assert env["PI_PANEL_CONFIG_DIR"] == str(config_dir)
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"
    assert "PI_PANEL_APP_SOCKET" not in env                  # manifest has no [app] varlink


def test_run_wrong_kind_or_missing(panel_home):
    with pytest.raises(SystemExit, match="not installed"):
        run.build("app", "ghost")


async def test_run_execs_the_program(panel_home, tmp_path):
    """The real thing: pi-panel-run replaces itself with the package's program."""
    pkg = tmp_path / "a"
    pkg.mkdir()
    (pkg / "pi-panel.toml").write_text(
        '[package]\nname = "echo"\nkind = "plugin"\n'
        '[run]\nexec = "sh -c \'echo $PI_PANEL_NAME in $(pwd)\'"\n')
    await Manager(output=Lines()).install(str(pkg))
    result = subprocess.run([sys.executable, "-m", "pi_panel_pluginman.run", "plugin", "echo"],
                            capture_output=True, text=True, env=os.environ)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"echo in {paths.package_dir('plugin', 'echo')}"


# --- scaffold ---------------------------------------------------------------

@pytest.mark.parametrize("kind", ["app", "plugin"])
def test_scaffold_produces_valid_packages(tmp_path, kind):
    target = scaffold.create(kind, "weather", tmp_path)
    m = Manifest.load(target)
    assert m.name == "weather" and m.kind == kind
    script = target / ("app.py" if kind == "app" else "plugin.py")
    compile(script.read_text(), str(script), "exec")
    with pytest.raises(FileExistsError):
        scaffold.create(kind, "weather", tmp_path)
