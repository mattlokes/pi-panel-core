# pi-panel-core-compositor

A [wlroots](https://gitlab.freedesktop.org/wlroots/wlroots) kiosk compositor for
pi-panel. It shows one fullscreen window at a time, fades between them, and is
controlled over [Varlink](https://varlink.org).

Built for the Raspberry Pi 5 (Raspberry Pi OS / Debian 13 trixie, arm64).

It is deliberately display-only: it has **no configuration and launches
nothing**. [pi-panel-core-ctl](../pi-panel-core-ctl) registers named slots, and
systemd runs each app as `pi-panel-app@<name>.service`. When an app's window
appears, the compositor reads the client process's cgroup, sees the unit it
runs in, and puts the window in that slot.

```
pi-panel-ctl ──Varlink──▶ compositor  ◀──Wayland── pi-panel-app@immich.service
  RegisterSlot("immich")    slot "immich" ◀── window from that unit's cgroup
  Switch("immich")          fade to it
  Subscribe()               ◀── pushed events
```

## Features

- **Kiosk mode**: every window is forced fullscreen, with no decorations.
- **Dynamic slots**: slots are created over IPC and matched by systemd unit, with Wayland `app_id` as a fallback.
- **Transitions**: fade to black and back (about 500 ms each way), or an instant cut.
- **Display power**: turn the output off and on, e.g. for night time.
- **Clock overlay**: the time in a corner (or centred), drawn above every app and through fades. Off until `SetClock` turns it on.
- **Varlink IPC**: introspectable, with a push-based `Subscribe` and no polling.
- **Touchscreen forwarding** to the visible window. This has not been tested on hardware yet.

## Requirements

```bash
sudo apt install libwlroots-0.19-dev libwayland-dev libwayland-bin \
                 wayland-protocols libxkbcommon-dev libpixman-1-dev \
                 libdrm-dev meson ninja-build pkg-config python3
```

`libwlroots-0.19-dev` comes from `archive.raspberrypi.com`, which Raspberry Pi
OS already has configured. Plain Debian trixie only carries 0.18.

[cJSON](https://github.com/DaveGamble/cJSON) 1.7.18 (MIT) is vendored in
`src/vendor/`. That keeps the build free of libsystemd, and free of anything
that would need an `apt install`.

For the same reason, [stb_truetype](https://github.com/nothings/stb) 1.26
(public domain / MIT) is vendored there too. It draws the clock from a TTF on
the Pi, by default DejaVu Sans Bold (`-Dclock_font=PATH` changes it). To look
at the rasterizer's output without a compositor, run
`build/clock-preview FONT.ttf "12:34" 64 > clock.pam`.

## Build

```bash
meson setup build/
ninja -C build/
```

## Running

In production it runs as `pi-panel-compositor.service` under `pi-panel.target`.
The root [`deploy/install.sh`](../deploy/install.sh) installs it.

| File | Installs to |
|---|---|
| `deploy/systemd/pi-panel-compositor.service` | `/etc/systemd/system/` |
| `deploy/pam.d/pi-panel` | `/etc/pam.d/pi-panel` |

The unit takes over tty1 with a logind session of its own (`PAMName=`,
`TTYPath=`), which is what gives it DRM access, and it signals readiness with
`Type=notify`. If it fails repeatedly, `OnFailure=` brings back a login prompt
on tty1.

```bash
systemctl restart pi-panel-compositor
journalctl -u pi-panel-compositor -b
```

### Development: a headless second instance

No root is needed, and the live panel is not disturbed:

```bash
systemd-run --user --unit pp-dev -E WLR_BACKENDS=headless -E WLR_RENDERER=pixman \
    $PWD/build/pi-panel-compositor --ipc-socket /tmp/c.varlink --wayland-socket pp-dev-0 --debug

varlinkctl call /tmp/c.varlink io.pipanel.Compositor.RegisterSlot '{"name":"a"}'
systemd-run --user --unit pi-panel-app@a -E WAYLAND_DISPLAY=pp-dev-0 foot
varlinkctl call -j /tmp/c.varlink io.pipanel.Compositor.ListSlots '{}'
```

A `systemd-run --user` unit named `pi-panel-app@a` matches slot `a` exactly as
the real system unit would.

## Varlink interface

The interface is `io.pipanel.Compositor`, defined in
[`src/io.pipanel.Compositor.varlink`](src/io.pipanel.Compositor.varlink). The
file is compiled into the binary, so you can always read the live version with:

```bash
varlinkctl introspect /run/pi-panel/compositor/io.pipanel.Compositor io.pipanel.Compositor
```

| Method | Purpose |
|---|---|
| `RegisterSlot(name, match_unit?, match_app_id?)` | Create a slot, or update an existing one. It adopts any matching unregistered window. |
| `UnregisterSlot(name)` | Forget a slot. Its window becomes `anon-<id>`. |
| `ListSlots()` / `GetStatus()` | Current state. |
| `Switch(slot, transition?)` | Show a slot, with a fade (the default) or a cut. |
| `SetOutputPower(on)` | Blank or unblank the display. |
| `SetClock(enabled, format?, position?, size?)` | Show, hide or restyle the clock overlay. Omitted fields are kept. |
| `Subscribe()` | Needs `more`. Sends a snapshot, then one event per change. |
| `Quit()` | Clean shutdown. |

A window that matches no slot is listed as `anon-<id>` and is never shown on
its own; switch to it explicitly to see it. If nothing is visible, the first
*registered* window to map is shown, so the panel does not sit black while the
controller is down.

The clock takes a `strftime(3)` format (default `%H:%M`), a corner or
`center` (default `bottom_right`), and a text height in pixels (`0`, the
default, means a sixteenth of the output height). It is redrawn on the minute,
or on the second if the format shows seconds:

```bash
varlinkctl call /run/pi-panel/compositor/io.pipanel.Compositor \
    io.pipanel.Compositor.SetClock '{"enabled":true,"format":"%a %H:%M","position":"top_right"}'
```

The socket is `0660`. A subscriber more than 256 KiB behind is disconnected
rather than allowed to stall the compositor.

## Keybindings

Almost every key is forwarded to the visible window. The one escape hatch is
`Ctrl+Alt+Backspace`, which quits the compositor; systemd then restarts it.
Under systemd, `systemctl stop pi-panel.target` is the way to stop the panel.

## Environment variables

| Variable | Purpose |
|---|---|
| `WLR_BACKENDS` | Force a backend: `drm`, `headless`, `wayland`, `x11` |
| `WLR_DRM_DEVICES` | Colon-separated DRM node paths |
| `WLR_RENDERER` | Force a renderer: `gles2`, `vulkan`, `pixman` |
| `WLR_NO_HARDWARE_CURSORS=1` | Disable the hardware cursor plane |

For the service, put these in `/etc/pi-panel/compositor.env`.
