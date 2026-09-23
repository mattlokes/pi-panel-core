"""pi-panel-pkg: install, update and remove pi-panel apps and plugins."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import paths, scaffold
from .manager import Manager, PackageError
from .manifest import Manifest


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pi-panel-pkg", description="Manage pi-panel packages.")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("install", help="install from a git URL (url#ref), a tarball, or a directory")
    x.add_argument("source")
    x.add_argument("--enable", action="store_true", help="enable it in pi-panel-ctl afterwards")
    x.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    x = sub.add_parser("update", help="reinstall from the recorded source")
    x.add_argument("name", nargs="?", help="default: every package")
    x = sub.add_parser("remove", help="stop and uninstall")
    x.add_argument("name")
    x.add_argument("--purge", action="store_true", help="also delete its config directory")
    sub.add_parser("list", help="installed packages")
    x = sub.add_parser("info", help="details of one package")
    x.add_argument("name")
    x = sub.add_parser("new", help="create a new package from the template")
    x.add_argument("kind", choices=["app", "plugin"])
    x.add_argument("name")
    x.add_argument("--dir", type=Path, default=Path.cwd(), help="parent directory (default: .)")
    sub.add_parser("tui", help="interactive terminal UI")
    return p


def _confirm_tty(manifest: Manifest) -> bool:
    print(f"\n  {manifest.name} ({manifest.kind}) {manifest.version or ''}")
    if manifest.description:
        print(f"  {manifest.description}")
    print(f"  runs:  {manifest.exec}")
    if manifest.build:
        print(f"  build: {manifest.build}")
    print("\nThis runs the package's build command and, once enabled, its program, as you.")
    return input("Install? [y/N] ").strip().lower() in ("y", "yes")


async def run(args: argparse.Namespace) -> int:
    manager = Manager(output=lambda line: print(line, flush=True))

    if args.cmd == "install":
        async def confirm(manifest: Manifest) -> bool:
            if args.yes or not sys.stdin.isatty():
                return True
            return await asyncio.to_thread(_confirm_tty, manifest)

        entry = await manager.install(args.source, confirm=confirm)
        if args.enable:
            await manager.set_enabled(entry.name, True)
    elif args.cmd == "update":
        names = [args.name] if args.name else [e.name for e in manager.list()]
        for name in names:
            await manager.update(name)
    elif args.cmd == "remove":
        await manager.remove(args.name, purge=args.purge)
    elif args.cmd == "list":
        entries = manager.list()
        ctl = await manager.ctl_apps()
        if not entries:
            print("nothing installed")
        for e in entries:
            state = ""
            if ctl is not None and e.name in ctl:
                a = ctl[e.name]
                state = f"{'enabled' if a['enabled'] else 'disabled'}, {a['unit_state']}"
            elif ctl is None:
                state = "ctl offline"
            rev = (e.source.commit or e.source.sha256 or "")[:10]
            print(f"{e.name:<16} {e.kind:<7} {e.version or '-':<10} {rev:<11} {state}")
    elif args.cmd == "info":
        entry = manager.registry.get(args.name)
        if entry is None:
            print(f"{args.name!r} is not installed", file=sys.stderr)
            return 1
        print(f"name:        {entry.name}")
        print(f"kind:        {entry.kind}")
        print(f"version:     {entry.version or '-'}")
        print(f"description: {entry.description or '-'}")
        print(f"path:        {entry.path}")
        print(f"config:      {paths.package_config_dir(entry.kind, entry.name)}")
        print(f"source:      {entry.source.type} {entry.source.url}"
              f"{'#' + entry.source.ref if entry.source.ref else ''}")
        if entry.source.commit:
            print(f"commit:      {entry.source.commit}")
        if entry.source.sha256:
            print(f"sha256:      {entry.source.sha256}")
        print(f"installed:   {entry.installed_at}")
        try:
            manifest = Manifest.load(Path(entry.path))
        except Exception:  # noqa: BLE001 - info must work on a broken install
            manifest = None
        if manifest and manifest.system_packages:
            from .manager import missing_system_packages
            missing = missing_system_packages(manifest.system_packages) or []
            listing = ", ".join(p + (" (MISSING)" if p in missing else "")
                                for p in manifest.system_packages)
            print(f"system:      {listing}")
        if entry.kind == "app":
            print(f"rotate:      {'yes' if entry.rotate else 'no (shown only on request)'}")
        print(f"unit:        pi-panel-{entry.kind}@{entry.name}.service")
    elif args.cmd == "new":
        target = scaffold.create(args.kind, args.name, args.dir)
        print(f"created {target}")
        print(f"  pi-panel-pkg install {target}")
    elif args.cmd == "tui":
        from .tui import PluginManagerApp
        await PluginManagerApp(Manager).run_async()
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except (PackageError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
