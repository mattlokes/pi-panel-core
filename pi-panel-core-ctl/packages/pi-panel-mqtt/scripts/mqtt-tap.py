#!/usr/bin/env python3
"""Tiny MQTT subscribe/publish tool.

Neither the Pi nor the dev Mac has mosquitto-clients, and installing it on the
Pi needs root, so this covers the same ground with no system packages:

    uv run --with aiomqtt scripts/mqtt-tap.py sub 'pi-panel/#'
    uv run --with aiomqtt scripts/mqtt-tap.py pub pi-panel/jazz/command/show immich
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import aiomqtt


async def sub(args: argparse.Namespace) -> None:
    async with aiomqtt.Client(args.host, args.port) as client:
        await client.subscribe(args.topic, qos=1)
        print(f"# subscribed to {args.topic} on {args.host}:{args.port}", flush=True)
        async for message in client.messages:
            payload = message.payload
            text = (
                payload.decode(errors="replace")
                if isinstance(payload, (bytes, bytearray))
                else str(payload)
            )
            retained = " [retained]" if message.retain else ""
            print(f"{message.topic}{retained} {text}", flush=True)


async def pub(args: argparse.Namespace) -> None:
    async with aiomqtt.Client(args.host, args.port) as client:
        await client.publish(
            args.topic, args.payload, qos=1, retain=args.retain
        )
    print(f"published to {args.topic}")


def main() -> int:
    p = argparse.ArgumentParser(description="minimal MQTT tap")
    p.add_argument("--host", default="mqtt.home.local")
    p.add_argument("--port", type=int, default=1883)
    sp = p.add_subparsers(dest="mode", required=True)

    s = sp.add_parser("sub", help="subscribe and print")
    s.add_argument("topic")
    s.set_defaults(func=sub)

    q = sp.add_parser("pub", help="publish once")
    q.add_argument("topic")
    q.add_argument("payload")
    q.add_argument("--retain", action="store_true")
    q.set_defaults(func=pub)

    args = p.parse_args()
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        pass
    except aiomqtt.MqttError as exc:
        print(f"mqtt error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
