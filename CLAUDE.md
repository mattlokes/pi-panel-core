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

`pi@prime` is the **production** panel (Pi 5, Raspberry Pi OS Lite trixie, NVMe). It is installed from GitHub clones, the way the README describes, not rsync. Develop on jazz. Its sudo needs a password too. It was built up from Lite to measure [PACKAGES.md](PACKAGES.md). Keep it free of anything that isn't recorded there.

```bash
rsync -a --delete --exclude .git --exclude .venv --exclude build --exclude __pycache__ \
    ./ pi@jazz:/opt/pi-panel/pi-panel-core/
```

Jazz moves to this layout in plan 2 phase F. Until then it still runs the old
`~/pi-panel-core` install.

## Remotes and CI

- **`origin`** is Forgejo: `git@forgejo.internal:matt/<repo>.git`. The web UI is https://forgejo.matts-homelab.com/. The repos are public and are the primary copy. The repos are `pi-panel-core`, `pi-panel-immich`, `pi-panel-now-playing` and `pi-panel-webrtc-client`.
- **`github`** is `git@github.com:mattlokes/<repo>.git`, public. Forgejo push-mirrors to it on every push. Treat it as read-only and don't push to it directly. Prime and the pluginman `packages.json` sources install from the GitHub URLs.
- **CI** is `.forgejo/workflows/ci.yml` with `runs-on: arm64`. It runs on **starscream**, a Pi 4 with 1.8 GiB RAM, using the host executor as user `forgejo-runner`. Keep builds at `-j2` or lower. Never put `-march`, `-mcpu` or `native` in build flags: the Pi 4 builds must stay valid on the Pi 5.
- Don't push unless asked to at that moment.

## Layout contract (shared by every pi-panel repo)

- **Directory names are load-bearing.** Other repos depend on the shared libraries by the relative path `../../pi-panel-core/pi-panel-core-ctl/packages/...`, and pluginman uses `../pi-panel-core-ctl/packages/...`. Installed packages go to `/opt/pi-panel/pi-panel-apps/<name>` and `/opt/pi-panel/pi-panel-plugin/<name>`, next to `/opt/pi-panel/pi-panel-core`. Unit files hard-code `/opt/pi-panel/pi-panel-core/...`. Don't rename directories.
- **Built in place.** The venvs have absolute shebangs and meson bakes the clock font's absolute path, so the core and every package are built where they run, never built elsewhere and copied.
- **Sockets:** `/run/pi-panel/{compositor,ctl,apps/<name>}/`. Each unit owns its own `RuntimeDirectory` leaf.
- **Files:**
  - code: `/opt/pi-panel` (`PI_PANEL_HOME`), read-only once it ships in an image. Its `packages.json` is the **image registry**: the packages that ship with the OS, beside the core in `pi-panel-apps/` and `pi-panel-plugin/`.
  - config: `/persistent/pi-panel/config` (`PI_PANEL_CONFIG_HOME`): `panel.toml` (the old name `ctl.toml` is still read), `apps/<name>/` and `plugins/<name>/` for package config, and an optional `compositor.env`
  - state: `/persistent/pi-panel/state` (`PI_PANEL_DATA_HOME`): the **state registry** `packages.json` and the packages added on the panel (`packages/pi-panel-apps/<name>`, beside a `packages/pi-panel-core` symlink so the relative path still works), plus `staging/`
  - ctl and pluginman merge the two registries; the image's entry wins a name clash. pluginman refuses to update, remove or replace an image package ("ships with the OS") unless given `--image`, which plan 4's image build and a writable dev panel (jazz) use.
- **Environment overrides:** `PI_PANEL_{HOME,CONFIG_HOME,DATA_HOME,RUNTIME_DIR}`. The defaults are the layout above, in both `pi_panel_ctl/paths.py` and `pi_panel_pluginman/paths.py` (a ctl test checks they agree), so CLIs run over SSH find the same files as the units. `PI_PANEL_CONFIG_DIR` is different: it is a package's *own* config directory, set by `pi-panel-run`.
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
