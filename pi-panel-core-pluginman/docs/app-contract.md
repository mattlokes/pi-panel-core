# The pi-panel app contract

An **app** is anything shown on the panel's screen. To be one, a program must:

1. **Be a Wayland client with one toplevel window.**
   - The compositor makes the window fullscreen whatever it asks for, so just
     open one window.
   - `WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR` and `SDL_VIDEODRIVER=wayland` are
     already set. Qt and GTK are also pointed at Wayland.
   - An SDL or pygame app should set `SDL_APP_ID=<name>` in its manifest's
     `[run].env`. This is optional: the compositor matches the window by its
     systemd unit, not its app_id, but the app_id makes `pi-panel-ctl` output
     more readable.

2. **Exit within 5 seconds of SIGTERM.**
   - systemd stops and restarts the app with SIGTERM, and sends SIGKILL after
     5 seconds.
   - If you install a SIGTERM handler that only sets a flag, check that flag
     everywhere you block, including network waits. An app that hangs on
     shutdown makes every restart and update slow.

3. **Run from its package directory, as the panel user.**
   - Read configuration from `$PI_PANEL_CONFIG_DIR`. Files listed under
     `[config] templates` in the manifest are copied there on the first
     install and are never overwritten by an update.
   - Use `${config_dir}` in `[run].exec` to pass that path on the command
     line.

4. **Log to stdout and stderr.** The output goes to the journal:
   `journalctl -u pi-panel-app@<name>`.

That is the whole contract. See `pi-panel-pkg new app <name>` for a working
example.

## Optional: io.pipanel.App

Set `[app] varlink = true` and serve the
[`io.pipanel.App`](../../pi-panel-core-ctl/packages/pi-panel-varlink/src/pi_panel_varlink/interfaces/io.pipanel.App.varlink)
Varlink interface on `$PI_PANEL_APP_SOCKET`. The interface has three methods:

| Method | Called by | Use it to |
|---|---|---|
| `SetVisible(visible)` | pi-panel-ctl, whenever the app is shown or hidden (including when the display turns off) | stop expensive work (animation, polling) while hidden |
| `ListActions()` | clients such as the MQTT plugin, which turns each action into a Home Assistant button | list the actions you support (`name` and `label`) |
| `InvokeAction(name)` | `pi-panel-ctl action <app> <name>`, Home Assistant | do the action |

From Python, `pi_panel_varlink.AppService` does all of this on a background
thread. It is built for the usual case, a blocking render loop:

```python
from pi_panel_varlink import AppService

service = AppService(
    on_visible=lambda visible: (show if visible else hide).set(),
    actions={"next": ("Next photo", lambda: next_requested.set())},
)
service.start()   # returns False, doing nothing, when not run by pi-panel
```

You can serve more interfaces of your own on the same socket, e.g.
`io.pipanel.app.Immich`. `pi-panel-ctl app <name> <Method> '<json>'` calls them.

## Depending on pi-panel-varlink

Installed apps live in `~/pi-panel-apps/<name>/`, next to `~/pi-panel-core/`,
so a relative path source works everywhere:

```toml
# PEP 723 script metadata, or [tool.uv.sources] in a pyproject.toml
[tool.uv.sources]
pi-panel-varlink = { path = "../../pi-panel-core/pi-panel-core-ctl/packages/pi-panel-varlink" }
```
