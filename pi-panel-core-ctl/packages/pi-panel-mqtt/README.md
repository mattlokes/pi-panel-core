# pi-panel-mqtt

The core's MQTT bridge: it puts the panel on MQTT, with
[Home Assistant discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).

```
pi-panel-ctl ──Varlink (Subscribe)──▶ pi-panel-mqtt ──MQTT──▶ broker ──▶ Home Assistant
             ◀──Varlink calls────────               ◀──commands──
apps' io.pipanel.App ◀── action buttons
```

It talks only to `io.pipanel.Ctl`, never to the compositor. Nothing is polled:
state arrives through ctl's `Subscribe`, and MQTT gets a retained message only
when something actually changes.

## Turning it on

It is part of pi-panel-core (it used to be the separate plugin
`pi-panel-plugin-mqtt`). `uv sync --all-packages` in `pi-panel-core-ctl`
installs it into ctl's venv, and it runs as `pi-panel-mqtt.service`, which
**pi-panel-ctl starts and stops** from the `[mqtt]` table of `panel.toml`:

```toml
[mqtt]
enabled = true
host = "mqtt.home.local"

[mqtt.homeassistant]
enabled = true
```

```bash
$EDITOR /persistent/pi-panel/config/panel.toml
pi-panel-ctl reload                    # checks [mqtt]; a bad key is refused
journalctl -u pi-panel-mqtt -f
```

ctl restarts the bridge whenever the `[mqtt]` table changes, and stops it for
`enabled = false` or no table at all. Every key, with its default, is in
[`src/pi_panel_mqtt/config.py`](src/pi_panel_mqtt/config.py) and in
[`panel.toml.example`](../../panel.toml.example). `PI_PANEL_MQTT_*` environment
variables and command-line flags still override the file.

To keep a broker password out of `panel.toml`, put `PI_PANEL_MQTT_PASSWORD=...`
in a drop-in: `systemctl edit pi-panel-mqtt`.

## Topics

The base is `pi-panel/<device>`; the device id defaults to the hostname.

| Topic | Retained | Payload |
|---|---|---|
| `availability` | yes | `online`/`offline`: the **bridge** (MQTT last will) |
| `ctl/availability` | yes | `online`/`offline`: pi-panel-ctl reachable |
| `compositor/availability` | yes | `online`/`offline`: the display server |
| `state` | yes | JSON: what is showing and why, holds, rotation, display power, clock overlay |
| `apps` | yes | JSON: installed apps and plugins, enabled, unit state |
| `rotations` | yes | JSON: `{"selected": …, "rotations": [names]}` |
| `app/<name>/actions` | yes | JSON: the app's actions (`io.pipanel.App.ListActions`) |
| `command/show` | no | app name, or `{"app", "seconds"?, "priority"?}`: a hold, for automations |
| `command/switch` | no | app name: show it now, as a person picks it (drops holds without an expiry, as *Next* does) |
| `command/release` | no | hold token |
| `command/next`, `command/previous` | no | anything |
| `command/rotation/paused` | no | `ON`/`OFF` |
| `command/rotation/select` | no | rotation name |
| `command/app/<name>/restart` | no | anything |
| `command/app/<name>/enabled` | no | `ON`/`OFF` |
| `command/app/<name>/action/<action>` | no | anything |
| `command/clock` | no | JSON, any of `{"enabled", "format", "position", "size"}` |
| `command/clock/enabled` | no | `ON`/`OFF` |
| `command/clock/format` | no | `strftime(3)` format, e.g. `%a %H:%M` |
| `command/clock/position` | no | `top_left`, `top_right`, `bottom_left`, `bottom_right`, `center` |
| `command/clock/size` | no | text height in px; `0` = automatic |
| `command/result` | no | JSON outcome of the last command, including ctl's error name |

For example, a doorbell automation can put the camera app up for 30 seconds
above everything else:

```bash
mosquitto_pub -t pi-panel/jazz/command/show -m '{"app": "camera", "seconds": 30, "priority": 50}'
```

Or turn on the clock overlay, top right, showing the day too:

```bash
mosquitto_pub -t pi-panel/jazz/command/clock -m '{"enabled": true, "format": "%a %H:%M", "position": "top_right"}'
```

**Availability is split into three topics on purpose.** The bridge, ctl and
the compositor fail independently. One combined topic could not tell "the
panel crashed" from "the bridge lost the broker".

**Command topics are never retained.** A retained command would fire again at
every broker reconnect.

## Home Assistant

With `[mqtt.homeassistant] enabled = true`, the panel shows up as a device,
*Pi Panel (jazz)*, with:

- a **Showing** select, and a *Showing because* sensor. Choosing an app switches to it at once, over an app's own hold such as music playing (`command/switch`); timed holds, such as a doorbell's, still win
- **Next** / **Previous** buttons, a **Rotation paused** switch and a **Rotation** select
- **Display** (power) and **Compositor** (connectivity) binary sensors
- a **Clock overlay** switch, with *format* (text), *position* (select) and
  *size* (number) under the device's configuration entities

Every app and plugin is a device of its own, connected via the panel
(`via_device`), e.g. *Pi Panel (jazz) - Immich*, with:

- an **Enabled** switch and a **Restart** button
- a button for each of the app's actions, e.g. *Next photo*

The entity set follows the panel's contents. When an app or action goes away,
its entities and its `app/<name>/actions` topic are removed by publishing an
empty retained payload, and Home Assistant drops a package's device with its
last entity. That also covers what went away while the bridge was down: at
connect it subscribes to this device's per-app topics, and clears what the
broker replays that ctl no longer has (for example the old mqtt plugin's own
*Enabled*/*Restart* entities). Entity ids don't
depend on the device, so moving entities between devices keeps them.

There is deliberately no control that stops the compositor.

## Development

From `pi-panel-core-ctl` (the workspace root):

```bash
uv sync --all-packages
uv run pytest packages/pi-panel-mqtt     # a fake io.pipanel.Ctl over real Varlink; no broker, no Pi
uv run --with aiomqtt packages/pi-panel-mqtt/scripts/mqtt-tap.py sub 'pi-panel/#'
```
