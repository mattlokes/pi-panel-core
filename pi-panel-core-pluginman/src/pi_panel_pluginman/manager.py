"""Install, update and remove packages. Used by both the CLI and the TUI.

Every long step (clone, download, build) is async and streams its output one
line at a time to the `output` callback, so a UI can show progress live.

An install is staged: the package is fetched and validated in a scratch
directory, moved into place, and built there. If the build fails, the
previous version (if any) is put back, so a bad update never leaves an app
half-installed.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from pi_panel_varlink import Client, ProtocolError, VarlinkError, VarlinkTimeout, VarlinkUnavailable

from . import paths
from .manifest import FILENAME, Manifest, ManifestError
from .registry import Entry, Registry, Source

Output = Callable[[str], None]
Confirm = Callable[[Manifest], Awaitable[bool]]

TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tar")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class PackageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SourceSpec:
    type: str        # git | tarball | local
    url: str
    ref: str | None = None


def parse_source(text: str) -> SourceSpec:
    """How to fetch |text|.

    - `https://…/x.tar.gz`, `./x.tgz`       tarball (downloaded or copied)
    - `/path/to/dir`                         copy of the working tree, uncommitted
                                             changes included (development)
    - `/path/to/repo#ref`, `file:///…`       git: only what is committed
    - anything else                          git; `url#ref` picks a branch, tag or commit
    """
    url, _, ref = text.partition("#")
    ref = ref or None
    lower = url.lower()
    if lower.endswith(TARBALL_SUFFIXES):
        return SourceSpec("tarball", url)
    candidate = Path(url).expanduser()
    if not re.match(r"^[a-z+]+://", url) and candidate.is_dir():
        path = str(candidate.resolve())
        return SourceSpec("git" if ref else "local", path, ref)
    return SourceSpec("git", url, ref)


def missing_system_packages(packages: tuple[str, ...] | list[str]) -> list[str] | None:
    """The Debian packages in |packages| that are not installed. None when
    this is not a dpkg system, in which case nothing can be checked."""
    if not packages:
        return []
    if shutil.which("dpkg-query") is None:
        return None
    missing = []
    for pkg in packages:
        result = subprocess.run(["dpkg-query", "-W", "-f=${Status}", pkg],
                                capture_output=True, text=True)
        if result.returncode != 0 or "install ok installed" not in result.stdout:
            missing.append(pkg)
    return missing


def _env() -> dict[str, str]:
    """Environment for build commands: the panel's paths, and uv on PATH."""
    env = dict(os.environ)
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


class Manager:
    def __init__(self, output: Output = print, ctl_socket: str | None = None) -> None:
        self.output = output
        self.registry = Registry(paths.registry())
        self.ctl = Client(ctl_socket or str(paths.ctl_socket()), timeout=5.0)

    # --- helpers ---------------------------------------------------------

    async def _run(self, argv: list[str], cwd: Path | None = None, shell: bool = False) -> None:
        """Run a command, streaming its output. Raises PackageError on failure."""
        shown = argv[0] if shell else shlex.join(argv)
        self.output(f"$ {shown}")
        if shell:
            proc = await asyncio.create_subprocess_shell(
                argv[0], cwd=cwd, env=_env(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, env=_env(),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except FileNotFoundError:
                raise PackageError(f"{argv[0]}: command not found") from None
        assert proc.stdout is not None
        async for line in proc.stdout:
            self.output(line.decode(errors="replace").rstrip())
        rc = await proc.wait()
        if rc != 0:
            raise PackageError(f"`{shown}` failed with exit status {rc}")

    async def _capture(self, argv: list[str], cwd: Path) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return out.decode().strip()

    async def _ctl(self, method: str, params: dict | None = None) -> dict | None:
        """Best effort: pluginman works without ctl running (e.g. first install)."""
        try:
            return await self.ctl.call(f"io.pipanel.Ctl.{method}", params)
        except (VarlinkUnavailable, VarlinkTimeout, ProtocolError):
            self.output("(pi-panel-ctl is not running; it will pick this up when it starts)")
        except VarlinkError as exc:
            self.output(f"pi-panel-ctl: {exc}")
        return None

    # --- fetching --------------------------------------------------------

    async def _fetch(self, spec: SourceSpec, into: Path) -> Source:
        source = Source(spec.type, spec.url, spec.ref)
        if spec.type == "git":
            if spec.ref:
                # A full clone, so a tag or commit hash works as well as a branch.
                await self._run(["git", "clone", "--quiet", "--no-checkout", spec.url, str(into)])
                await self._run(["git", "-c", "advice.detachedHead=false", "checkout",
                                 "--quiet", spec.ref], cwd=into)
            else:
                await self._run(["git", "clone", "--quiet", "--depth", "1", spec.url, str(into)])
            source.commit = await self._capture(["git", "rev-parse", "HEAD"], into)
        elif spec.type == "tarball":
            archive = into.parent / "archive"
            if re.match(r"^https?://", spec.url):
                self.output(f"downloading {spec.url}")
                await asyncio.to_thread(urllib.request.urlretrieve, spec.url, archive)
            else:
                shutil.copyfile(Path(spec.url).expanduser(), archive)
            source.sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.output(f"sha256 {source.sha256}")
            into.mkdir()
            try:
                with tarfile.open(archive) as tar:
                    # filter="data" refuses absolute paths, "..", devices and
                    # links out of the tree: a tarball cannot write outside `into`.
                    tar.extractall(into, filter="data")
            except (tarfile.TarError, OSError) as exc:
                raise PackageError(f"cannot unpack {spec.url}: {exc}") from None
        else:
            self.output(f"copying {spec.url}")
            src = Path(spec.url)
            if (src / ".git").exists():
                # The working tree as git sees it: uncommitted changes in,
                # gitignored files out. That keeps local secrets (an app's
                # config.yaml with an API key) from being copied along.
                listing = await self._capture(
                    ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], src)
                into.mkdir()
                for rel in filter(None, listing.split("\0")):
                    target = into / rel
                    if not (src / rel).exists():
                        continue   # deleted but not yet committed
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src / rel, target, follow_symlinks=False)
            else:
                shutil.copytree(src, into, symlinks=True,
                                ignore=shutil.ignore_patterns(".venv", "__pycache__",
                                                              ".pytest_cache"))
        return source

    @staticmethod
    def _package_root(tree: Path) -> Path:
        """The manifest's directory. Release tarballs (e.g. GitHub's) wrap the
        tree in one top-level directory; look one level down for it."""
        if (tree / FILENAME).exists():
            return tree
        children = [c for c in tree.iterdir() if not c.name.startswith(".")]
        if len(children) == 1 and (children[0] / FILENAME).exists():
            return children[0]
        raise PackageError(f"no {FILENAME} found: this is not a pi-panel package")

    # --- operations ------------------------------------------------------

    async def install(self, source_text: str, confirm: Confirm | None = None) -> Entry:
        spec = parse_source(source_text)
        paths.staging().mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="install-", dir=paths.staging()))
        try:
            source = await self._fetch(spec, stage / "src")
            root = self._package_root(stage / "src")
            try:
                manifest = Manifest.load(root)
            except ManifestError as exc:
                raise PackageError(str(exc)) from None
            self.output(f"package: {manifest.name} ({manifest.kind}) {manifest.version or ''}".rstrip())

            existing = self.registry.get(manifest.name)
            if existing and existing.kind != manifest.kind:
                raise PackageError(f"{manifest.name!r} is already installed as a {existing.kind}")
            if confirm is not None and not await confirm(manifest):
                raise PackageError("cancelled")

            # pluginman never uses root, so it cannot install these itself;
            # fail before building, with the exact command to run.
            missing = await asyncio.to_thread(missing_system_packages, manifest.system_packages)
            if missing is None:
                self.output("warning: not a dpkg system; cannot check [system] packages")
            elif missing:
                raise PackageError(
                    f"{manifest.name} needs system packages that are not installed.\n"
                    f"Install them first:  sudo apt install {' '.join(missing)}")

            dest = paths.package_dir(manifest.kind, manifest.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            previous = stage / "previous"
            if dest.exists():
                shutil.move(str(dest), previous)
            shutil.move(str(root), dest)
            try:
                if manifest.build:
                    self.output("building")
                    await self._run([manifest.build], cwd=dest, shell=True)
            except BaseException:
                self.output("build failed; restoring the previous version" if previous.exists()
                            else "build failed; removing the package")
                shutil.rmtree(dest, ignore_errors=True)
                if previous.exists():
                    shutil.move(str(previous), dest)
                raise

            self._install_config(manifest, dest)
            entry = Entry(
                name=manifest.name, kind=manifest.kind, path=str(dest), source=source,
                version=manifest.version, description=manifest.description,
                varlink=manifest.varlink, rotate=manifest.rotate,
            )
            self.registry.put(entry)
            self.output(f"installed {manifest.name} into {dest}")
            await self._ctl("Rescan")
            return entry
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _install_config(self, manifest: Manifest, dest: Path) -> None:
        if not manifest.templates:
            return
        config_dir = paths.package_config_dir(manifest.kind, manifest.name)
        config_dir.mkdir(parents=True, exist_ok=True)
        for template in manifest.templates:
            src = dest / template
            if not src.is_file():
                self.output(f"warning: config template {template} is missing from the package")
                continue
            name = src.name[: -len(".example")] if src.name.endswith(".example") else src.name
            target = config_dir / name
            if target.exists():
                self.output(f"keeping existing config {target}")
            else:
                shutil.copyfile(src, target)
                self.output(f"wrote config {target} (edit it)")

    async def update(self, name: str) -> Entry:
        entry = self.registry.get(name)
        if entry is None:
            raise PackageError(f"{name!r} is not installed")
        text = entry.source.url + (f"#{entry.source.ref}" if entry.source.ref else "")
        self.output(f"updating {name} from {text}")
        updated = await self.install(text)
        if updated.name != name:
            raise PackageError(f"the source now provides {updated.name!r}, not {name!r}")
        if entry.source.commit and entry.source.commit == updated.source.commit:
            self.output("already up to date")
        await self._ctl("RestartPackage", {"name": name})
        return updated

    async def remove(self, name: str, purge: bool = False) -> None:
        entry = self.registry.get(name)
        if entry is None:
            raise PackageError(f"{name!r} is not installed")
        # Stop it first (and drop it from ctl.toml), so nothing runs from a
        # directory that is about to disappear.
        await self._ctl("EnablePackage", {"name": name, "enabled": False})
        shutil.rmtree(entry.path, ignore_errors=True)
        self.registry.remove(name)
        if purge:
            shutil.rmtree(paths.package_config_dir(entry.kind, name), ignore_errors=True)
            self.output("removed its config too")
        self.output(f"removed {name}")
        await self._ctl("Rescan")

    def list(self) -> list[Entry]:
        self.registry.load()
        return sorted(self.registry.entries.values(), key=lambda e: (e.kind, e.name))

    async def ctl_apps(self) -> dict[str, dict] | None:
        """ctl's view (enabled, unit state), or None if ctl is not running."""
        try:
            reply = await self.ctl.call("io.pipanel.Ctl.ListApps")
        except (VarlinkUnavailable, VarlinkTimeout, ProtocolError, VarlinkError):
            return None
        return {a["name"]: a for a in reply.get("apps", [])}

    async def set_enabled(self, name: str, enabled: bool) -> bool:
        return await self._ctl("EnablePackage", {"name": name, "enabled": enabled}) is not None
