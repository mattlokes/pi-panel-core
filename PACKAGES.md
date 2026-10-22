# System packages

None. This was measured on a fresh Raspberry Pi OS Lite (trixie, arm64) on a
Pi 5 with pi-panel-core already installed. Everything comes from PyPI through
`uv.lock` (aiomqtt, paho-mqtt).

## Not from apt

- `uv` (from the core), for `[build]`. The venv uses the system `python3`.
- `[build]` runs `uv sync` without `--no-dev`, so the venv also gets the dev group (pytest and friends, about 3 MB). That is harmless, but an image can leave it out.

## Config

The `device_id` default of `""` means the hostname. Panels with different
hostnames therefore never share Home Assistant entities, so one `mqtt.toml`
can be copied between panels unchanged.
