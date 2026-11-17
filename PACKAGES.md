# System packages

What pi-panel-core needs from the OS, for building an image. This was measured on
a fresh Raspberry Pi OS **Lite** (trixie, arm64, the 2026-10-06 image) on a
Pi 5, with every install done as `apt-get install --no-install-recommends`.
Each list holds only what was missing from Lite. The packages Lite already
provides are listed at the end.

Apps and plugins each have their own `PACKAGES.md`. On a panel, `pi-panel-pkg`
checks them through the `[system]` table in the manifest.

## Build only

These are needed to build the compositor, not to run it. Together they pull
in about 150 packages, mostly X11/xcb and glib headers plus llvm. An image
recipe can build in a stage and leave them out.

```
libwlroots-0.19-dev     # from archive.raspberrypi.com: plain Debian trixie has 0.18
libwayland-dev
libwayland-bin          # wayland-scanner, for xdg-shell-protocol.h
wayland-protocols
libxkbcommon-dev
libpixman-1-dev
libdrm-dev
meson
ninja-build
```

`pkg-config`, `gcc` (`build-essential`) and `python3` (tools/embed.py) are
already in Lite.

## Runtime

```
libwlroots-0.19         # compositor; its Depends bring in Mesa (libegl-mesa0, mesa-libgallium, libgbm1, libgles2)
git                     # pi-panel-pkg clones packages; also how the core itself is fetched
python3-gi              # the welcome screen (pi-panel-core-welcome): PyGObject, seen through its venv's system site-packages
gir1.2-gtk-4.0          # the welcome screen: GTK 4
```

The two welcome-screen packages are the same ones the GTK apps (now-playing)
already list, so a panel with now-playing installed needs nothing new. They
were added in plan 3 and have not been measured on a fresh Lite (prime has
them through now-playing); plan 4 re-baselines this file for minbase anyway.

The compositor needs no fonts. Its clock font is in
`pi-panel-core-compositor/fonts/`, and DejaVu Sans is only the fallback.

## Not from apt

- **uv** in `/usr/local/bin/uv`, system-wide, so the service user and every login user share one (`curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh`; 0.12.10 on jazz and prime).
  - `pi-panel-run` makes sure `/usr/local/bin` is on PATH, so apps whose `[run]` calls `uv` (immich) find it.
  - `pi-panel-pkg` runs package builds with it.
- **Python** is the system `python3` (3.13). `uv sync` makes the ctl and pluginman venvs from `/usr/bin/python3.13` and downloads no interpreter.
- **The code** is `git clone https://github.com/mattlokes/pi-panel-core.git /opt/pi-panel/pi-panel-core` (with `/opt/pi-panel` owned by the login user; the service user only reads it), then build it in place:
  ```bash
  (cd pi-panel-core-compositor && meson setup build && ninja -C build)
  (cd pi-panel-core-ctl       && uv sync --frozen --compile-bytecode --all-packages)
  (cd pi-panel-core-pluginman && uv sync --frozen --compile-bytecode)
  (cd pi-panel-core-welcome   && uv venv --system-site-packages --python /usr/bin/python3 --allow-existing \
                              && uv sync --frozen --compile-bytecode)
  ```
  The unit files run from these paths: `build/` and the three `.venv`s. Neither can be moved after building: the venvs have absolute shebangs, and meson bakes the clock font's path.

- **The MQTT bridge** (`pi-panel-core-ctl/packages/pi-panel-mqtt`) needs nothing from apt: aiomqtt and paho-mqtt come from PyPI through ctl's `uv.lock`, into ctl's venv. It was the separate plugin `pi-panel-plugin-mqtt` until plan 2; that measured no apt packages either.

## System setup (`deploy/install.sh`)

All of this happens in `install.sh`, so an image can just run it in the chroot:
- copies the units, `/etc/pam.d/pi-panel` and `/etc/polkit-1/rules.d/50-pi-panel.rules`
- symlinks `pi-panel-{ctl,ctld,pkg,run}` into `/usr/local/bin`
- creates the system user and group `pi-panel` (also in `video`, `render`, `input`; home `/persistent/pi-panel/home`, nologin) and adds the login user (`PI_PANEL_LOGIN_USER`, default `$SUDO_USER`) to the group
- creates `/persistent/pi-panel/{config,state}`, owned by `pi-panel:pi-panel`, setgid and group-writable
- writes no compositor.env: `pi-panel-core-compositor/deploy/start` picks the renderer at each start (pixman on Pis without a GPU MMU); overrides go in `/persistent/pi-panel/config/compositor.env`
- runs `loginctl enable-linger pi-panel`; in a chroot (no running systemd) it writes `/var/lib/systemd/linger/pi-panel` itself and skips `daemon-reload`
- `systemctl set-default multi-user.target`, then enables `pi-panel.target`, `pi-panel-compositor` and `pi-panel-ctl`. `pi-panel-mqtt.service` is installed but not enabled: ctl starts it from `[mqtt]` in panel.toml

## Already in Lite, and needed

```
polkitd                 # the rule that lets pi-panel and its group manage pi-panel-* units
libpam-systemd          # the compositor's logind session on tty1 (PAMName=pi-panel)
systemd                 # 257: varlinkctl
python3
xkb-data                # keymaps for libxkbcommon
pkg-config gcc          # build
curl ca-certificates    # uv installer, package fetches
```

## User

- The panel runs as the system user `pi-panel` (group `pi-panel`), created by `install.sh`. The unit files and the polkit rule assume it. It is in the `video`, `render` and `input` groups.
- The login user can be anyone (`pi` on jazz and prime). It joins the `pi-panel` group, which gives it the sockets, config, state and the `pi-panel-*` units.
- On a Lite image flashed without Imager customisation, first boot runs userconf-pi's rename dialog (`userconfig.service` on tty8), and `pi` has a nologin shell. Prime was fixed with `sudo cancel-rename pi && sudo usermod -s /bin/bash pi`. An image recipe that creates `pi` itself never sees this.
