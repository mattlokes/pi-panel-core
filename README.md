# pi-panel-core

pi-panel is a smart-home display optimised for single-board computers such as
the Raspberry Pi. This repo is its core: everything needed to run a panel.
The things it shows (apps) and optional helpers (plugins) are separate repos,
installed with `pi-panel-pkg`.

| Directory | What |
|---|---|
| [`pi-panel-core-compositor/`](pi-panel-core-compositor) | a kiosk Wayland compositor (wlroots, C). It shows one app at a time and has no configuration: slots are registered at runtime. |
| [`pi-panel-core-ctl/`](pi-panel-core-ctl) | `pi-panel-ctld`, the central daemon that decides what is shown and runs the apps; the `pi-panel-ctl` CLI and TUI; and the shared Python libraries (`pi-panel-varlink`, `pi-panel-tui-kit`) |
| [`pi-panel-core-pluginman/`](pi-panel-core-pluginman) | `pi-panel-pkg` (install apps and plugins from git or tarballs; CLI and TUI), `pi-panel-run`, and the unit templates that run every package |
| [`deploy/`](deploy) | `install.sh`, plus the files that tie the pieces together: `pi-panel.target`, `pi-panel.slice`, and the polkit rule |

## How it fits together

```
                        pi-panel.target  (systemctl restart pi-panel.target = the whole panel)
                ┌──────────────┴───────────────┐
  pi-panel-compositor.service         pi-panel-ctl.service
  wlroots, tty1, no config            decides what is shown
        ▲  Varlink (private)                │ starts/stops over D-Bus (polkit)
        └─── RegisterSlot/Switch/Subscribe ─┤
                                            ├──▶ pi-panel-app@immich.service     (apps: on screen)
  CLI, TUIs, plugins, apps ──Varlink──▶ ctl ├──▶ pi-panel-app@clock.service
     io.pipanel.Ctl                         └──▶ pi-panel-plugin@mqtt.service    (plugins: helpers)
```

- **systemd owns every process.** Everything runs in `pi-panel.slice` and is `PartOf=pi-panel.target`. The compositor takes over tty1 through a logind session of its own, and ctl starts apps and plugins as instances of two generic templates.
- **Everything speaks [Varlink](https://varlink.org)**: JSON over a Unix socket, with interfaces you can introspect. `varlinkctl` works against every pi-panel socket.
- **Nothing polls.** The compositor streams window events, systemd streams unit states, and ctl streams its own state to the CLI, the TUIs and plugins.
- **Apps and plugins live next to the core:** `~/pi-panel-apps/<name>` and `~/pi-panel-plugin/<name>`, installed by `pi-panel-pkg` without root.

## Installing a panel

On Raspberry Pi OS (trixie), as the panel user `pi`:

```bash
sudo apt install libwlroots-0.19-dev libwayland-dev libwayland-bin wayland-protocols \
                 libxkbcommon-dev libpixman-1-dev libdrm-dev meson ninja-build pkg-config
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/mattlokes/pi-panel-core.git ~/pi-panel-core && cd ~/pi-panel-core
(cd pi-panel-core-compositor && meson setup build && ninja -C build)
(cd pi-panel-core-ctl       && uv sync --frozen --compile-bytecode --all-packages)
(cd pi-panel-core-pluginman && uv sync --frozen --compile-bytecode)
sudo deploy/install.sh          # the only root step
sudo reboot
```

`deploy/install.sh` does the following:
- installs the units, the PAM file and the polkit rule
- links `pi-panel-ctl`, `pi-panel-ctld`, `pi-panel-pkg` and `pi-panel-run` into `/usr/local/bin`
- enables lingering for `pi`, and enables `pi-panel.target`
- migrates an old install away from the `~/.bash_profile` / tty1 startup

You can run it again safely after updates.

Then add something to show:

```bash
pi-panel-pkg install https://github.com/mattlokes/pi-panel-app-immich.git
pi-panel-ctl enable immich
```

## Using it

```bash
pi-panel-ctl tui          # the panel: what is shown and why, rotation, apps, actions
pi-panel-pkg tui          # packages: install, update, remove, enable
pi-panel-ctl status
pi-panel-ctl show immich --seconds 60
journalctl -u pi-panel-ctl -u pi-panel-compositor -u 'pi-panel-app@*' -f
```

From another machine, use `ssh -t pi@<panel> pi-panel-ctl tui`. See each
component's README for the details.

**Recovery.** `systemctl restart pi-panel.target` works over SSH without a
password. If the compositor keeps failing, a login prompt comes back on tty1.

## Updating

```bash
cd ~/pi-panel-core && git pull
(cd pi-panel-core-compositor && ninja -C build)
(cd pi-panel-core-ctl && uv sync --frozen --compile-bytecode --all-packages)
(cd pi-panel-core-pluginman && uv sync --frozen --compile-bytecode)
sudo deploy/install.sh            # only if deploy/ or a unit file changed
systemctl restart pi-panel.target
pi-panel-pkg update               # apps and plugins
```

## Development

Each component tests offline: no Pi, root or broker is needed.

```bash
(cd pi-panel-core-ctl && uv sync --all-packages && uv run pytest)
(cd pi-panel-core-pluginman && uv sync && uv run pytest)
```

The compositor builds only where wlroots 0.19 is installed (the Pi). Its
README shows how to run a headless second instance next to the live panel.
