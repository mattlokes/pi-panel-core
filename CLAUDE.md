# pi-panel-core

One repo with three components: `pi-panel-core-compositor` (C/wlroots),
`pi-panel-core-ctl` (the daemon plus the shared Python libraries) and
`pi-panel-core-pluginman`. `deploy/` holds the panel-wide files and
`install.sh`. Each component has its own CLAUDE.md with its design points.

Apps (`../pi-panel-apps/*`) and plugins (`../pi-panel-plugin/*`) are
**separate repos**.

## The target

The target is `pi@jazz` (Raspberry Pi 5, trixie, arm64, uid 1000).
- `sudo` on jazz needs a password, so root steps (only `deploy/install.sh`) go back to the user.
- Once installed, the polkit rule lets `pi` manage every `pi-panel-*` unit without a password, so `systemctl restart pi-panel-ctl` works over SSH.
- Do not run process-name patterns (`pgrep -f …`) inline over SSH, because they match your own session. Use `systemctl` and `systemd-run --user` units instead.
- On jazz, logs from user units are not in `journalctl --user`. Query them with `journalctl _SYSTEMD_USER_UNIT=<unit>`.

```bash
rsync -a --delete --exclude .git --exclude .venv --exclude build --exclude __pycache__ \
    ./ pi@jazz:~/pi-panel-core/
```

## Layout contract (shared by every pi-panel repo)

- **Directory names are load-bearing.** Other repos depend on the shared libraries by the relative path `../../pi-panel-core/pi-panel-core-ctl/packages/...`, and pluginman uses `../pi-panel-core-ctl/packages/...`. Installed packages go to `~/pi-panel-apps/<name>` and `~/pi-panel-plugin/<name>`, next to `~/pi-panel-core`. Unit files hard-code `/home/pi/pi-panel-core/...`. Don't rename directories.
- **Sockets:** `/run/pi-panel/{compositor,ctl,apps/<name>}/`. Each unit owns its own `RuntimeDirectory` leaf.
- **Files:**
  - config: `~/.config/pi-panel/ctl.toml`, plus `apps/<name>/` and `plugins/<name>/` for package config
  - registry: `~/.local/share/pi-panel/packages.json`, written by pluginman and read by ctl
- **Environment overrides:** `PI_PANEL_{HOME,CONFIG_HOME,DATA_HOME,RUNTIME_DIR}`. `PI_PANEL_CONFIG_DIR` is different: it is a package's *own* config directory, set by `pi-panel-run`.
- **Varlink interfaces:**
  - `io.pipanel.Compositor` is private to ctl.
  - `io.pipanel.Ctl` is the panel's public API.
  - `io.pipanel.App` is served by apps.
  - The `.varlink` files are served verbatim and must pass `varlinkctl validate-idl`.

## Git

The compositor's history before the merge came in with `git subtree` (not
squashed). Those commits keep their original root-level paths
(`src/ipc.c`, not `pi-panel-core-compositor/src/ipc.c`), so
`git log -- pi-panel-core-compositor` shows only the merge. To see the old
history, use `git log <merge>^2`, or `git log --oneline --graph`.
