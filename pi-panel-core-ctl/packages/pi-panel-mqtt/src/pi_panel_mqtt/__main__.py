"""pi-panel-mqtt: entry point (run by pi-panel-plugin@mqtt.service)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from .bridge import Bridge
from .config import DEFAULT_CONFIG_PATH, load

log = logging.getLogger("pi-panel-mqtt")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pi-panel-mqtt",
                                description="Bridge pi-panel-ctl to MQTT and Home Assistant.")
    p.add_argument("--config", type=Path, default=None, help=f"default: {DEFAULT_CONFIG_PATH}")
    p.add_argument("--host", default=None, help="MQTT broker host")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--username", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--device-id", default=None, help="topic device id (default: hostname)")
    p.add_argument("--base-topic", default=None)
    p.add_argument("--ctl-socket", default=None, help="io.pipanel.Ctl socket")
    p.add_argument("--no-discovery", action="store_true",
                   help="do not publish Home Assistant discovery configs")
    p.add_argument("--log-level", default=None)
    return p


def _overrides(args: argparse.Namespace) -> dict:
    ov = {
        "mqtt.host": args.host,
        "mqtt.port": args.port,
        "mqtt.username": args.username,
        "mqtt.password": args.password,
        "mqtt.device_id": args.device_id,
        "mqtt.base_topic": args.base_topic,
        "ctl.socket_path": args.ctl_socket,
        "log_level": args.log_level,
    }
    if args.no_discovery:
        ov["homeassistant.enabled"] = False
    return ov


async def _run(bridge: Bridge) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    task = asyncio.create_task(bridge.run())
    await stop.wait()
    log.info("shutting down")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
        await task


def main() -> int:
    args = build_parser().parse_args()
    try:
        settings = load(args.config, _overrides(args))
    except (ValueError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    # The journal adds its own timestamps; only include them on a terminal.
    fmt = "%(asctime)s %(levelname)-7s %(message)s" if sys.stderr.isatty() else "%(levelname)-7s %(message)s"
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format=fmt, stream=sys.stderr)
    bridge = Bridge(settings)
    log.info("broker %s:%d, device %r, ctl %s", settings.mqtt.host, settings.mqtt.port,
             bridge.topics.device_id, settings.ctl.resolved_socket())
    try:
        asyncio.run(_run(bridge))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
