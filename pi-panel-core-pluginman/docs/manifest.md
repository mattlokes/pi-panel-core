# pi-panel.toml

Every package has a `pi-panel.toml` at its root:

```toml
[package]
name = "immich"             # [a-z0-9][a-z0-9_-]*, at most 63 characters
kind = "app"                # app | plugin
version = "0.3.0"           # optional
description = "Immich photo frame"   # optional

[run]
exec = "uv run --script pi_panel_immich.py --config ${config_dir}/config.yaml"
env = { SDL_APP_ID = "immich" }       # optional

[build]                     # optional
command = "uv sync --frozen --compile-bytecode"

[config]                    # optional
templates = ["config.yaml.example"]

[app]                       # apps only, optional
varlink = true
```

## `[package]`

`name` is used for several things at once: the systemd instance
(`pi-panel-app@immich`), the compositor slot, the install directory, and the
MQTT topic level. That's why it is restricted to lowercase letters, digits,
`-` and `_`.

## `[run]`

`exec` is split like a shell command line, so quotes work, but it is **not**
run through a shell. Use `sh -c '…'` if you need one. These variables are
substituted:

| Variable | Value |
|---|---|
| `${config_dir}` | the package's config directory, `~/.config/pi-panel/<kind>s/<name>` |
| `${package_dir}` | where the package is installed, which is also the working directory |
| `${name}` | the package name |
| `${runtime_dir}` | `/run/pi-panel` |

`env` adds environment variables. It cannot override the `PI_PANEL_*`
variables that `pi-panel-run` sets.

## `[build]`

`command` runs in the installed package directory after every install and
update, through `/bin/sh`, with `~/.local/bin` (where `uv` lives) on `PATH`.
Its output streams to `pi-panel-pkg` and the TUI. If it fails, the previous
version is restored.

A Python package with a `pyproject.toml` usually wants
`uv sync --frozen --compile-bytecode`, then an `exec` pointing at
`.venv/bin/<script>`. Precompiling matters on a Pi that boots from slow
storage. A PEP 723 script needs no build step: `uv run --script` resolves and
caches its dependencies the first time it runs.

## `[config]`

Each file listed in `templates` is copied into `${config_dir}` on install,
without its `.example` suffix, but only if the target does not already exist.
Your edits therefore survive updates.

## `[app]`

`varlink = true` declares that the app serves `io.pipanel.App` on
`$PI_PANEL_APP_SOCKET`. See [app-contract.md](app-contract.md).
