# pi-panel-plugin-mqtt

A pi-panel plugin that puts the panel on MQTT, with
[Home Assistant discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery).

```
pi-panel-ctl ──Varlink (Subscribe)──▶ pi-panel-mqtt ──MQTT──▶ broker ──▶ Home Assistant
             ◀──Varlink calls──────── (plugin)      ◀──commands──
apps' io.pipanel.App ◀── action buttons
```

It talks only to `io.pipanel.Ctl`, never to the compositor. Nothing is polled:
state arrives through ctl's `Subscribe`, and MQTT gets a retained message only
when something actually changes.

## Install

```bash
pi-panel-pkg install https://github.com/mattlokes/pi-panel-plugin-mqtt.git
$EDITOR ~/.config/pi-panel/plugins/mqtt/mqtt.toml      # broker host etc.
pi-panel-ctl enable mqtt
journalctl -u pi-panel-plugin@mqtt -f
```

Put a broker password in the environment rather than in the file: add
`PI_PANEL_MQTT_PASSWORD=...` to a drop-in for `pi-panel-plugin@mqtt.service`.

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

With `[homeassistant] enabled = true`, the panel shows up as a device,
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
its entities are removed by publishing an empty retained config, and Home
Assistant drops a package's device with its last entity. Entity ids don't
depend on the device, so moving entities between devices keeps them.

There is deliberately no control that stops the compositor.

## Development

```bash
uv sync
uv run pytest            # a fake io.pipanel.Ctl over real Varlink; no broker, no Pi
uv run --with aiomqtt scripts/mqtt-tap.py sub 'pi-panel/#'
```

`pi-panel-varlink` comes from `../../pi-panel-core/pi-panel-core-ctl` by
relative path. That path also works once the plugin is installed to
`~/pi-panel-plugin/mqtt`.
