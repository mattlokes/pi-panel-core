# pi-panel-compositor

A [wlroots](https://gitlab.freedesktop.org/wlroots/wlroots)-based Wayland compositor for kiosk-style interfaces.  
Built for and on the Raspberry Pi 5 (Raspberry Pi OS / Debian 13 trixie, arm64) with touchscreen support.

## Features

- **Kiosk mode** — all windows are forced fullscreen; no decorations or taskbar
- **View system** — multiple named "views", each containing one fullscreen application; only one view visible at a time
- **Smooth transitions** — fade-to-black → switch → fade-in animation (~500 ms per half)
- **Unix socket IPC** — line-based protocol lets a Python script switch views, launch apps, query status
- **App launcher** — compositor can fork/exec configured apps and auto-restart them on exit
- **Touchscreen** — full touch forwarding to the active view
- **Backend auto-detection** — nested window under X11/Wayland (development) or DRM/KMS takeover (Pi production)

## Requirements

- **wlroots 0.19** (0.19.1 is what Raspberry Pi OS trixie ships, and what
  labwc on the stock desktop already uses)
- wayland-server, libxkbcommon, pixman
- wayland-protocols + wayland-scanner (`xdg-shell-protocol.h` is generated at
  build time — it is not shipped by any package)
- meson + ninja

### Raspberry Pi OS (Debian 13 trixie)

```bash
sudo apt install libwlroots-0.19-dev libwayland-dev libwayland-bin \
                 wayland-protocols libxkbcommon-dev libpixman-1-dev \
                 libdrm-dev meson ninja-build pkg-config
```

> The `libwlroots-0.19-dev` package comes from the `archive.raspberrypi.com`
> repository, which is already configured on Raspberry Pi OS. Plain Debian
> trixie only carries 0.18 — if you ever build elsewhere, you would need to
> retarget or add that archive.

## Build

```bash
meson setup build/
ninja -C build/
```

## Configuration

Create `/etc/pi-panel/compositor.conf` (or pass `--config FILE`):

```
# name  auto-restart  command
dashboard  yes  python3 /home/pi/apps/dashboard.py
browser    no   chromium-browser --kiosk http://localhost:3000
```

See `example.conf` for details.  The compositor sets `WAYLAND_DISPLAY`
automatically and unsets `DISPLAY` in each child process.

## Running

### Development (nested inside the Pi desktop)

The stock Raspberry Pi OS desktop is itself a Wayland session (labwc), so the
quickest edit/run loop is to run the compositor nested in a window there:

```bash
WLR_BACKENDS=wayland ./build/pi-panel-compositor --debug
```

Launch test apps into the compositor (use whatever Wayland socket was printed):

```bash
WAYLAND_DISPLAY=wayland-1 foot --app-id=dashboard
WAYLAND_DISPLAY=wayland-1 foot --app-id=browser
```

### Production (bare DRM/KMS, from a TTY)

```bash
sudo usermod -aG video,render,input $USER   # once, then re-login
./build/pi-panel-compositor --config /etc/pi-panel/compositor.conf

# Force a specific DRM node if needed:
WLR_DRM_DEVICES=/dev/dri/card1 WLR_RENDERER=gles2 \
    ./build/pi-panel-compositor
```

## IPC Python Client

```bash
python3 client/pi_panel_client.py list
python3 client/pi_panel_client.py switch 0
python3 client/pi_panel_client.py switch-name dashboard
python3 client/pi_panel_client.py switch-app com.example.app
python3 client/pi_panel_client.py launch myapp "python3 /opt/apps/myapp.py"
python3 client/pi_panel_client.py close myapp
python3 client/pi_panel_client.py restart myapp
python3 client/pi_panel_client.py status
```

As a library:

```python
from pi_panel_client import PiPanelClient

c = PiPanelClient()               # connects to /tmp/pi-panel.sock
c.switch_name("dashboard")        # fade transition to dashboard view
views = c.list_views()            # list of dicts
c.launch("video", "mpv --fullscreen /media/clip.mp4")
```

## Setting app_id for Python/SDL2 apps

```python
import os
os.environ['SDL_VIDEODRIVER']          = 'wayland'
os.environ['SDL_VIDEO_WAYLAND_APP_ID'] = 'my-dashboard'
import pygame
```

The compositor assigns clients to view slots by **matching the client's process
against the process it launched**, walking up the process tree — so an app
behind a wrapper script still lands in its configured slot, and `app_id` is not
required. It does improve `switch-app` and `list` output, though.

Each view is launched in its own session, so `close` and `restart` signal the
whole process group and take a forking wrapper down along with the app it
spawned. The one case matching cannot cover is a fully re-parented orphan (the
launched process exits while the GUI process keeps running); such a client
lands in an anonymous view.

## Which view is visible at startup

Clients race to map, so "first to map" is not a stable default. Until the
controller issues its first `switch`, the compositor keeps the **first view in
config order** visible, correcting itself if a later-configured client happens
to map first. Something is shown as soon as any client maps, so the panel is
never needlessly black. After an explicit switch, the compositor stops
overriding your choice.

## IPC Protocol Reference

Text line-based over `AF_UNIX SOCK_STREAM` (default: `/tmp/pi-panel.sock`).

| Command | Response |
|---|---|
| `switch <id>` | `OK` or `ERROR ...` |
| `switch-name <name>` | `OK` or `ERROR ...` |
| `switch-app <app_id>` | `OK` or `ERROR ...` |
| `launch <name> <command>` | `OK id=N` or `ERROR ...` |
| `close <name\|id>` | `OK` or `ERROR ...` |
| `restart <name\|id>` | `OK pid=N` or `ERROR ...` |
| `list` | `DATA N` + N lines + `END` |
| `status` | `OK active_id=N active_name=X view_count=N transitioning=false` |
| `version` | `OK pi-panel-compositor/1.0 protocol/1` |

## Useful Environment Variables

| Variable | Purpose |
|---|---|
| `WLR_BACKENDS` | Force backend: `x11`, `wayland`, `drm`, `headless` |
| `WLR_DRM_DEVICES` | Colon-separated DRM node paths |
| `WLR_RENDERER` | Force renderer: `gles2`, `vulkan`, `pixman` |
| `WLR_NO_HARDWARE_CURSORS=1` | Disable hardware cursor plane (fixes flicker) |
| `WLR_LOG_LEVEL` | `debug`, `info`, `error` |
| `WAYLAND_DEBUG=1` | Log Wayland protocol messages |
