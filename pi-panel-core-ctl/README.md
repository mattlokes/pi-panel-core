# pi-panel-core-ctl

The central control daemon of pi-panel. It decides what the screen shows,
runs the apps, and is the panel's single API (`io.pipanel.Ctl`).

This directory is a uv workspace with three packages:

| Package | What |
|---|---|
| `pi-panel-ctl` | `pi-panel-ctld` (the daemon), `pi-panel-ctl` (CLI and `tui`) |
| `pi-panel-varlink` | a dependency-free asyncio Varlink client and server, `AppService` for apps, and `sd_notify` |
| `pi-panel-tui-kit` | shared Textual pieces, so `pi-panel-ctl tui` and `pi-panel-pkg tui` look and behave the same |

The [pi-panel-core README](../README.md) has the architecture and the install
procedure.

## What is on screen, and why

ctl works through a priority stack, highest first:

1. **Holds**: `Show(app, seconds?, priority?)`, e.g. "doorbell camera for 30s at priority 50". A hold without seconds pins the app until *next*, *previous* or *release*.
2. **Schedules**: time-window rules, e.g. "power off 23:00–06:30", "show clock 07:00–09:00 on weekdays", or "use the *night* rotation".
3. **Rotation**: the selected playlist, which cycles through apps that have a window. With no rotation configured, ctl uses every enabled app for `default_seconds` each.

A hold and a schedule compete on priority. On a tie, the hold wins.

Everything is pushed, nothing is polled:
- The compositor streams slot and transition events.
- systemd streams unit state changes.
- ctl streams its own state to subscribers (`pi-panel-ctl watch`, the TUI, the MQTT plugin).

Apps that serve `io.pipanel.App` are told when they are shown or hidden, so
they can stop working while off screen.

## Using it

```bash
pi-panel-ctl status                     # what is showing, and why
pi-panel-ctl apps
pi-panel-ctl show camera --seconds 30 --priority 50
pi-panel-ctl next | prev | pause | resume
pi-panel-ctl rotation set default immich:300 clock:60
pi-panel-ctl schedule set night --start 23:00 --end 06:30 --action power_off --priority 10
pi-panel-ctl enable immich | disable immich | restart immich
pi-panel-ctl action immich next         # an app's own action
pi-panel-ctl watch                      # the event stream
pi-panel-ctl tui
```

Any Varlink client works as well, since the interface is introspectable:

```bash
varlinkctl introspect /run/pi-panel/ctl/io.pipanel.Ctl io.pipanel.Ctl
varlinkctl call --more /run/pi-panel/ctl/io.pipanel.Ctl io.pipanel.Ctl.Subscribe '{}'
```

Configuration lives in `~/.config/pi-panel/ctl.toml`, which covers enabled
packages, rotations, schedules and the transition style. ctl writes changes
made through the API back to this file.

## Development

```bash
uv sync --all-packages
uv run pytest
```

The tests need no Pi, broker or root. ctld is tested end to end against a
fake compositor (speaking real Varlink) and fake systemd units. The Varlink
library is also checked against systemd's `varlinkctl`, which validates the
IDL files and makes real calls.

To run a complete panel unprivileged on the Pi, use a headless compositor and
user units:

```bash
systemd-run --user --unit pp-comp -E WLR_BACKENDS=headless -E WLR_RENDERER=pixman \
    ~/pi-panel-core/pi-panel-core-compositor/build/pi-panel-compositor \
    --ipc-socket /tmp/pp/c --wayland-socket pp-0
pi-panel-ctld --user-units --compositor-socket /tmp/pp/c --socket /tmp/pp/ctl
```
