# pi-panel-core-welcome

The screen a panel shows when no app is: a fresh image, nothing enabled, or
every enabled app still starting or failed. It shows:

- the hostname, IP addresses, release version (`/etc/pi-panel-release`, or
  "development build") and A/B slot (on an image with `tryboot_a_b=1`)
- why nothing else is showing, and the `ssh` command and next step to fix it
- the failures and warnings of `pi-panel-ctl config check`, through ctl's
  `CheckConfig`, re-checked every 30 s while it is on screen

It is part of the core, not a package. `pi-panel-welcome.service` runs it,
and pi-panel-ctl starts that unit, registers the slot `welcome` for it, and
switches to it whenever there is nothing else to show. The name `welcome` is
reserved.

```bash
uv venv --system-site-packages --python /usr/bin/python3 --allow-existing
uv sync && uv run pytest                     # offline, no GTK needed

# On a Pi with GTK: the screen with pretend apps and errors, as a PNG.
WAYLAND_DISPLAY=pi-panel-0 .venv/bin/pi-panel-welcome --demo --size 1920x1080 --screenshot /tmp/welcome.png
# The same, with the live panel's state from ctl (what the panel shows now).
WAYLAND_DISPLAY=pi-panel-0 .venv/bin/pi-panel-welcome --size 1920x1080 --screenshot /tmp/welcome.png
```

The compositor has no screencopy (`grim` does not work), so `--screenshot` is
how to see the screen remotely. A screenshot window is outside every slot and
gets no frame callbacks, so it is drawn only once: a live `--screenshot`
waits for ctl's answer (checks included), then stops updating.

PyGObject and GTK 4 come from apt (`python3-gi`, `gir1.2-gtk-4.0`, see the
core's PACKAGES.md), which the venv sees through `--system-site-packages`.
