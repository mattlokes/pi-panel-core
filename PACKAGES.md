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
```

The compositor needs no fonts. Its clock font is in
`pi-panel-core-compositor/fonts/`, and DejaVu Sans is only the fallback.

## Not from apt

- **uv** in `~pi/.local/bin/uv`, installed with the official installer (0.12.10 on jazz and prime).
  - `pi-panel-run` puts `~/.local/bin` first on PATH, so apps whose `[run]` calls `uv` (immich) find it under systemd.
  - `pi-panel-pkg` runs package builds with it.
- **Python** is the system `python3` (3.13). `uv sync` makes the ctl and pluginman venvs from `/usr/bin/python3.13` and downloads no interpreter.
- **The code** is `git clone https://github.com/mattlokes/pi-panel-core.git ~pi/pi-panel-core`, then build it:
  ```bash
  (cd pi-panel-core-compositor && meson setup build && ninja -C build)
  (cd pi-panel-core-ctl       && uv sync --frozen --compile-bytecode --all-packages)
  (cd pi-panel-core-pluginman && uv sync --frozen --compile-bytecode)
  ```
  The unit files run from these paths: `build/` and the two `.venv`s.

## System setup (`deploy/install.sh`)

All of this happens in `install.sh`, so an image can just run it in the chroot:
- copies the units, `/etc/pam.d/pi-panel` and `/etc/polkit-1/rules.d/50-pi-panel.rules`
- symlinks `pi-panel-{ctl,ctld,pkg,run}` into `/usr/local/bin`
- writes `/etc/pi-panel/compositor.env`, which is empty on a Pi 5 (it has a GPU MMU)
- runs `loginctl enable-linger pi`. In a chroot without logind, create `/var/lib/systemd/linger/pi` instead.
- `systemctl set-default multi-user.target`, then enables `pi-panel.target`, `pi-panel-compositor` and `pi-panel-ctl`

## Already in Lite, and needed

```
polkitd                 # the rule that lets pi manage pi-panel-* units
libpam-systemd          # the compositor's logind session on tty1 (PAMName=pi-panel)
systemd                 # 257: varlinkctl
python3
xkb-data                # keymaps for libxkbcommon
pkg-config gcc          # build
curl ca-certificates    # uv installer, package fetches
```

## User

- The user is `pi` (uid 1000) with home `/home/pi`. The unit files, the polkit rule and the relative package paths all assume it.
- `pi` must be in the `video`, `render` and `input` groups. Lite's default first user already is.
- On a Lite image flashed without Imager customisation, first boot runs userconf-pi's rename dialog (`userconfig.service` on tty8), and `pi` has a nologin shell. Prime was fixed with `sudo cancel-rename pi && sudo usermod -s /bin/bash pi`. An image recipe that creates `pi` itself never sees this.
