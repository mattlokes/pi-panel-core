"""pi-panel.toml: what a package is and how to run it.

    [package]
    name = "immich"            # [a-z0-9][a-z0-9_-]*, max 63: unit instance, slot and topic name
    kind = "app"               # app | plugin
    version = "0.3.0"
    description = "Immich photo frame"

    [run]
    exec = "uv run --script pi_panel_immich.py --config ${config_dir}/config.yaml"
    env = { SDL_APP_ID = "immich" }

    [build]                    # optional; run in the package dir after install/update
    command = "uv sync --frozen --compile-bytecode"

    [config]                   # optional
    templates = ["config.yaml.example"]   # copied into ${config_dir}, minus ".example", if absent

    [app]                      # apps only, optional
    varlink = true             # serves io.pipanel.App on $PI_PANEL_APP_SOCKET

`exec` is split like a shell command line (quotes work) but is not run through
a shell, and may use ${config_dir}, ${package_dir}, ${name} and ${runtime_dir}.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILENAME = "pi-panel.toml"
KINDS = ("app", "plugin")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_VAR = re.compile(r"\$\{(\w+)\}")
VARIABLES = ("config_dir", "package_dir", "name", "runtime_dir")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Manifest:
    name: str
    kind: str
    exec: str
    version: str | None = None
    description: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    build: str | None = None
    templates: tuple[str, ...] = ()
    varlink: bool = False

    @classmethod
    def load(cls, directory: Path) -> "Manifest":
        path = directory / FILENAME
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ManifestError(f"no {FILENAME} in {directory}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ManifestError(f"{FILENAME}: {exc}") from None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        unknown = set(data) - {"package", "run", "build", "config", "app"}
        if unknown:
            raise ManifestError(f"unknown section(s): {', '.join(sorted(unknown))}")
        pkg = _table(data, "package")
        run = _table(data, "run")
        build = _table(data, "build", required=False)
        config = _table(data, "config", required=False)
        app = _table(data, "app", required=False)

        name = _str(pkg, "package.name")
        if not _NAME.match(name):
            raise ManifestError(
                f"package.name {name!r} must be lowercase letters, digits, '-' or '_' "
                "(it becomes a systemd instance and an MQTT topic level)")
        kind = _str(pkg, "package.kind")
        if kind not in KINDS:
            raise ManifestError(f"package.kind must be one of {', '.join(KINDS)}")
        if app and kind != "app":
            raise ManifestError("[app] is only for kind = \"app\"")

        exec_ = _str(run, "run.exec")
        try:
            argv = shlex.split(exec_)
        except ValueError as exc:
            raise ManifestError(f"run.exec: {exc}") from None
        if not argv:
            raise ManifestError("run.exec is empty")
        for var in _VAR.findall(exec_):
            if var not in VARIABLES:
                raise ManifestError(f"run.exec: unknown variable ${{{var}}}")

        env = run.get("env", {})
        if not isinstance(env, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ManifestError("run.env must map names to strings")

        templates = config.get("templates", []) if config else []
        if not isinstance(templates, list) or not all(isinstance(t, str) for t in templates):
            raise ManifestError("config.templates must be a list of file names")
        for t in templates:
            if Path(t).is_absolute() or ".." in Path(t).parts:
                raise ManifestError(f"config.templates: {t!r} must be inside the package")

        return cls(
            name=name,
            kind=kind,
            exec=exec_,
            version=_opt_str(pkg, "version"),
            description=_opt_str(pkg, "description"),
            env=dict(env),
            build=_opt_str(build, "command") if build else None,
            templates=tuple(templates),
            varlink=bool(app.get("varlink", False)) if app else False,
        )

    def argv(self, variables: dict[str, str]) -> list[str]:
        """The command to exec, with ${...} substituted in each argument."""
        return [_VAR.sub(lambda m: variables[m.group(1)], arg) for arg in shlex.split(self.exec)]


def _table(data: dict[str, Any], key: str, required: bool = True) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        if required:
            raise ManifestError(f"[{key}] is required")
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{key} must be a table")
    return value


def _str(table: dict[str, Any], dotted: str) -> str:
    value = table.get(dotted.rsplit(".", 1)[-1])
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{dotted} is required")
    return value.strip()


def _opt_str(table: dict[str, Any], key: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ManifestError(f"{key} must be a string")
    return value
