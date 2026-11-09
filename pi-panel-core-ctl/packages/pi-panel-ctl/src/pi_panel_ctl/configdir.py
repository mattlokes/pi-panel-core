"""`pi-panel-ctl config check|export|import`: the config directory as a whole.

These run locally, as the user, without ctld: they read files, and run each
enabled package's `[config] check` command. Only `import` talks to ctld, at
the end, to apply what it installed (Reload).

The config directory holds panel.toml, apps/<name>/ and plugins/<name>/ (each
package's own config) and an optional compositor.env. It is the one thing to
back up: `export` writes it as a .tar.gz, `import` puts one back.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .config import CtlConfig, resolve_path
from .model import ValidationError
from .packages import Package

CHECK_TIMEOUT = 60.0
_VAR = re.compile(r"\$\{([a-z_]+)\}")
# Left behind by an interrupted atomic save; never worth exporting.
_SKIP = re.compile(r"^\.(panel|ctl)\.toml\.")


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    status: str        # "ok" | "failed" | "warning"
    detail: str = ""


def check_argv(pkg: Package, config_home: Path) -> list[str]:
    """A package's check command, with its variables resolved against
    `config_home` (which need not be the live config directory)."""
    variables = {
        "config_dir": str(config_home / f"{pkg.kind}s" / pkg.name),
        "package_dir": pkg.path or "",
        "name": pkg.name,
        "runtime_dir": str(paths.runtime_dir()),
    }
    return [_VAR.sub(lambda m: variables.get(m.group(1), m.group(0)), arg)
            for arg in shlex.split(pkg.check or "")]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    local_bin = str(Path.home() / ".local" / "bin")      # uv, as for pi-panel-run
    if local_bin not in env.get("PATH", "").split(":"):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def _run_check(pkg: Package, config_home: Path) -> Result:
    argv = check_argv(pkg, config_home)
    try:
        proc = subprocess.run(argv, cwd=pkg.path or None, env=_env(), capture_output=True,
                              text=True, timeout=CHECK_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(pkg.name, "failed", f"{shlex.join(argv)}: {exc}")
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        return Result(pkg.name, "failed", output or f"exit status {proc.returncode}")
    return Result(pkg.name, "ok", output.splitlines()[-1] if output else "")


def check(config_home: Path, packages: dict[str, Package]) -> list[Result]:
    """panel.toml (with [mqtt]), then each enabled package's own check."""
    path = resolve_path(config_home)
    try:
        cfg = CtlConfig.load(path)
    except ValidationError as exc:
        return [Result(path.name, "failed", str(exc))]
    results = [Result(path.name, "ok", "" if path.exists() else "absent: defaults")]
    for name in cfg.enabled:
        pkg = packages.get(name)
        if pkg is None:
            results.append(Result(name, "warning", "enabled but not installed"))
        elif not pkg.check:
            results.append(Result(name, "ok", "no check declared"))
        else:
            results.append(_run_check(pkg, config_home))
    return results


def failed(results: list[Result]) -> bool:
    return any(r.status == "failed" for r in results)


# --- export / import ----------------------------------------------------------

def default_export_name() -> str:
    host = socket.gethostname().split(".")[0]
    return f"pi-panel-config-{host}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"


def export(config_home: Path, dest: Path) -> list[str]:
    """Write the config directory to `dest` (.tar.gz). Returns the members."""
    names: list[str] = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest, "w:gz") as tar:
        for path in sorted(config_home.rglob("*")):
            rel = path.relative_to(config_home)
            if _SKIP.match(path.name) or path.is_symlink():
                continue
            tar.add(path, arcname=str(rel), recursive=False)
            names.append(str(rel))
    return names


def unpack(archive: Path, into: Path) -> None:
    """Extract an exported config. filter="data" refuses absolute paths, "..",
    links out of the tree and device files."""
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(into, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise ValidationError(f"{archive}: cannot unpack: {exc}") from None


def install(config_home: Path, archive: Path, packages: dict[str, Package],
            backup_dir: Path) -> tuple[list[Result], Path | None]:
    """Check the archive's config as a whole, then (only if it passes) back up
    the current directory and replace its contents with the archive's.
    Returns the check results and the backup's path (None: nothing changed)."""
    staging_root = paths.data_home() / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="config-import-", dir=staging_root))
    try:
        unpack(archive, stage)
        results = check(stage, packages)
        if failed(results):
            return results, None
        backup = backup_dir / f"config-before-import-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        export(config_home, backup)
        # Replace the contents, not the directory: its parent belongs to root.
        config_home.mkdir(parents=True, exist_ok=True)
        for child in config_home.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in stage.iterdir():
            shutil.move(str(child), config_home / child.name)
        return results, backup
    finally:
        shutil.rmtree(stage, ignore_errors=True)
