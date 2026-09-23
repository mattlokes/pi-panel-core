"""pi-panel-ctl: command-line client for pi-panel-ctld (io.pipanel.Ctl).

Everything here is a thin Varlink call; `varlinkctl` against the same socket
works just as well, this is only friendlier.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from pi_panel_varlink import Client, ProtocolError, VarlinkError, VarlinkTimeout, VarlinkUnavailable

from . import paths

CTL = "io.pipanel.Ctl"


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _fmt_state(s: dict[str, Any]) -> str:
    lines = [
        f"showing:    {s['showing'] or '-'}  ({s['reason']})",
        f"display:    {'on' if s['output_power'] else 'OFF'}",
        f"rotation:   {s['rotation']}{'  (paused)' if s['rotation_paused'] else ''}",
        f"compositor: {'online' if s['compositor_online'] else 'OFFLINE'}",
    ]
    if s["active_schedules"]:
        lines.append(f"schedules:  {', '.join(s['active_schedules'])}")
    for h in s["holds"]:
        left = "until released" if h["expires_in"] is None else f"{h['expires_in']:.0f}s left"
        lines.append(f"hold:       {h['app']} p{h['priority']} {left}  [{h['token']}]")
    return "\n".join(lines)


def _fmt_apps(apps: list[dict[str, Any]]) -> str:
    rows = [("", "NAME", "KIND", "ENABLED", "UNIT", "WINDOW", "VERSION")]
    for a in apps:
        rows.append((
            "▶" if a["visible"] else " ",
            a["name"], a["kind"], "yes" if a["enabled"] else "no", a["unit_state"],
            ("mapped" if a["mapped"] else "-") if a["kind"] == "app" else "",
            a["version"] or "",
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)


def _parse_entry(text: str) -> dict[str, Any]:
    app, sep, seconds = text.rpartition(":")
    if not sep:
        raise argparse.ArgumentTypeError(f"{text!r}: expected APP:SECONDS")
    return {"app": app, "seconds": float(seconds)}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pi-panel-ctl", description="Control a pi-panel.")
    p.add_argument("--socket", default=None, help=f"default: {paths.ctl_socket()}")
    p.add_argument("--json", action="store_true", help="print raw JSON replies")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="what is on screen, and why")
    sub.add_parser("apps", help="installed apps and plugins")
    s = sub.add_parser("show", help="put an app on screen (a hold)")
    s.add_argument("app")
    s.add_argument("--seconds", type=float, default=None, help="default: until released")
    s.add_argument("--priority", type=int, default=0)
    s = sub.add_parser("release", help="end a hold")
    s.add_argument("token")
    for name, helptext in (("next", "next app in the rotation"),
                           ("prev", "previous app in the rotation"),
                           ("pause", "pause the rotation"),
                           ("resume", "resume the rotation"),
                           ("rescan", "re-read installed packages")):
        sub.add_parser(name, help=helptext)

    r = sub.add_parser("rotation", help="list, select, set or remove rotations")
    rs = r.add_subparsers(dest="rcmd", required=True)
    rs.add_parser("list")
    x = rs.add_parser("select")
    x.add_argument("name")
    x = rs.add_parser("set", help="e.g. set default immich:300 clock:60")
    x.add_argument("name")
    x.add_argument("entries", nargs="+", type=_parse_entry, metavar="APP:SECONDS")
    x = rs.add_parser("remove")
    x.add_argument("name")

    sc = sub.add_parser("schedule", help="list, set or remove schedule rules")
    ss = sc.add_subparsers(dest="scmd", required=True)
    ss.add_parser("list")
    x = ss.add_parser("set", help="e.g. set night --start 23:00 --end 06:30 --action power_off")
    x.add_argument("name")
    x.add_argument("--days", default="daily")
    x.add_argument("--start", required=True)
    x.add_argument("--end", required=True)
    x.add_argument("--action", required=True, choices=["power_off", "show", "rotation"])
    x.add_argument("--app")
    x.add_argument("--rotation")
    x.add_argument("--priority", type=int, default=0)
    x = ss.add_parser("remove")
    x.add_argument("name")

    for name in ("enable", "disable", "restart"):
        x = sub.add_parser(name, help=f"{name} a package")
        x.add_argument("name")

    sub.add_parser("watch", help="stream events until interrupted")

    x = sub.add_parser("app", help="call a method on an app's own Varlink socket")
    x.add_argument("name")
    x.add_argument("method", help="e.g. ListActions, or a fully qualified io.x.Y.Method")
    x.add_argument("params", nargs="?", default="{}", help="JSON parameters")
    x = sub.add_parser("action", help="invoke an app action, e.g. `action immich next`")
    x.add_argument("name")
    x.add_argument("action")

    sub.add_parser("tui", help="interactive terminal UI")
    return p


async def run(args: argparse.Namespace) -> int:
    address = args.socket or str(paths.ctl_socket())
    ctl = Client(address)

    async def call(method: str, params: dict | None = None) -> dict:
        return await ctl.call(f"{CTL}.{method}", params)

    cmd = args.cmd
    if cmd == "status":
        state = (await call("GetState"))["state"]
        print(json.dumps(state, indent=2) if args.json else _fmt_state(state))
    elif cmd == "apps":
        apps = (await call("ListApps"))["apps"]
        print(json.dumps(apps, indent=2) if args.json else _fmt_apps(apps))
    elif cmd == "show":
        params: dict[str, Any] = {"app": args.app, "priority": args.priority}
        if args.seconds is not None:
            params["seconds"] = args.seconds
        print((await call("Show", params))["token"])
    elif cmd == "release":
        await call("Release", {"token": args.token})
    elif cmd in ("next", "prev", "pause", "resume"):
        await call({"next": "Next", "prev": "Previous", "pause": "PauseRotation",
                    "resume": "ResumeRotation"}[cmd])
    elif cmd == "rescan":
        apps = (await call("Rescan"))["apps"]
        print(_fmt_apps(apps))
    elif cmd == "rotation":
        if args.rcmd == "list":
            reply = await call("GetRotations")
            if args.json:
                _print_json(reply)
            else:
                for rot in reply["rotations"]:
                    mark = "*" if rot["name"] == reply["selected"] else " "
                    entries = "  ".join(f"{e['app']}:{e['seconds']:g}" for e in rot["entries"])
                    print(f"{mark} {rot['name']:<12} {entries or '(empty)'}")
        elif args.rcmd == "select":
            await call("SelectRotation", {"name": args.name})
        elif args.rcmd == "set":
            await call("SetRotation", {"rotation": {"name": args.name, "entries": args.entries}})
        elif args.rcmd == "remove":
            await call("RemoveRotation", {"name": args.name})
    elif cmd == "schedule":
        if args.scmd == "list":
            rules = (await call("GetSchedules"))["schedules"]
            if args.json:
                _print_json(rules)
            else:
                for s in rules:
                    target = s.get("app") or s.get("rotation") or ""
                    mark = "●" if s.get("active") else " "
                    print(f"{mark} {s['name']:<12} {s['days']:<10} {s['start']}-{s['end']}  "
                          f"{s['action']} {target}  p{s['priority']}")
        elif args.scmd == "set":
            rule = {"name": args.name, "days": args.days, "start": args.start, "end": args.end,
                    "action": args.action, "app": args.app, "rotation": args.rotation,
                    "priority": args.priority}
            await call("SetSchedule", {"schedule": rule})
        elif args.scmd == "remove":
            await call("RemoveSchedule", {"name": args.name})
    elif cmd in ("enable", "disable"):
        await call("EnablePackage", {"name": args.name, "enabled": cmd == "enable"})
    elif cmd == "restart":
        await call("RestartPackage", {"name": args.name})
    elif cmd == "watch":
        async for reply in ctl.call_more(f"{CTL}.Subscribe"):
            event = reply["event"]
            if args.json:
                print(json.dumps(event), flush=True)
            else:
                s = event["state"]
                print(f"[{event['kind']}] showing={s['showing']} reason={s['reason']} "
                      f"power={'on' if s['output_power'] else 'off'}", flush=True)
    elif cmd in ("app", "action"):
        apps = {a["name"]: a for a in (await call("ListApps"))["apps"]}
        app = apps.get(args.name)
        if app is None:
            print(f"no such app: {args.name}", file=sys.stderr)
            return 1
        if not app["socket"]:
            print(f"{args.name} does not serve a Varlink interface", file=sys.stderr)
            return 1
        client = Client(app["socket"])
        if cmd == "action":
            await client.call("io.pipanel.App.InvokeAction", {"name": args.action})
        else:
            method = args.method if "." in args.method else f"io.pipanel.App.{args.method}"
            _print_json(await client.call(method, json.loads(args.params)))
    elif cmd == "tui":
        from .tui import run_tui
        await run_tui(address)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except VarlinkError as exc:
        detail = " ".join(f"{k}={v}" for k, v in exc.parameters.items())
        print(f"error: {exc.error.rsplit('.', 1)[-1]} {detail}".rstrip(), file=sys.stderr)
        return 1
    except (VarlinkUnavailable, VarlinkTimeout, ProtocolError) as exc:
        # `app` and `action` talk to an app's own socket, not to ctld.
        who = "the app" if args.cmd in ("app", "action") else "pi-panel-ctld"
        print(f"cannot reach {who}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
