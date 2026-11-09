# pi-panel-mqtt

The core's bridge from `io.pipanel.Ctl` to MQTT, with Home Assistant
discovery. A member of pi-panel-core-ctl's uv workspace (merged in with
`git subtree` from the old `pi-panel-plugin-mqtt` repo). It runs as
`pi-panel-mqtt.service` from ctl's venv, and **ctl starts, stops and restarts
it** from the `[mqtt]` table of `panel.toml`.

## Environment

- The broker is `mqtt.home.local` (192.168.4.97:1883). It **accepts anonymous connections**.
- Home Assistant is separate, at `hass.home.local`.
- Publishing to the real broker creates or changes HA entities, so ask before doing it from a dev machine. For experiments, use `--device-id` with a test name and `--no-discovery`.

```bash
uv run pytest packages/pi-panel-mqtt   # from pi-panel-core-ctl: fake ctl over real Varlink + a recording publisher, no broker
```

## Design points worth preserving

- **Talk to ctl only.** Never talk to the compositor: ctl is the panel's API and it decides what is shown.
- **`sync()` is the only publisher of retained state.** It computes every retained topic from memory and publishes what differs from its cache. The cache is cleared on every broker (re)connect, because retained state may have been lost. This design makes duplicate or missed events harmless.
- **The ctl loop and the MQTT loop are independent.** Either can be down. ctl state is kept while MQTT is down, and vice versa.
- **Availability comes in three topics** (bridge / ctl / compositor). Entities need bridge + ctl; display entities also need the compositor. The compositor connectivity sensor needs only bridge + ctl, since its whole job is reporting the compositor as down.
- **Command topics are never retained**, and the bridge ignores its own `command/result`, which it receives through the `command/#` wildcard.
- **Removal is an empty retained payload** on the old topic, for discovery configs and `app/<name>/actions` alike. `sync()` diffs the set of per-app topics it published on every pass. Topics left by an earlier run are found by subscribing to this device's per-app wildcards at connect; `note_retained` collects what the broker replays, and `sync()` clears what ctl no longer has, **but only once ctl has sent a state**, because before that everything looks stale.
- **`validate()` in config.py is the one checker of `[mqtt]`.** ctl imports it, so `pi-panel-ctl reload` refuses a bad table before the bridge restarts with it. Keep it strict: unknown keys and wrong types are errors.
- **The clock overlay entities need bridge + ctl only**, not the compositor. The settings live in ctl, which applies them when the compositor comes up. They are published only when ctl's state has a `clock` key.
- **HA reads a `select`'s options from its discovery config**, so the config is republished when the app or rotation set changes. That happens automatically, because discovery is just part of `desired()`.

## Gotchas

- **Home Assistant does not move an existing entity to another device** when a discovery update changes its `device`. It creates the new device but leaves the entity where it was first registered. To move entities, publish an empty retained config for each, then republish (restarting the bridge does it). The entity ids come back the same, because `object_id` is the unique id.

- In TOML a bare key after a `[section]` header belongs to that section. Keep top-level keys above the first header. `tests/test_config.py` checks the shipped example, `pi-panel-core-ctl/panel.toml.example`.
