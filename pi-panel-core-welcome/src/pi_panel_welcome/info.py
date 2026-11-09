"""Facts about this device for the welcome screen: who it is, where to reach
it, and what it runs. No GTK; every reader takes its paths or input, so the
tests feed it fixtures."""

from __future__ import annotations

import json
import logging
import pwd
import shlex
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("pi-panel-welcome")

RELEASE_FILE = Path("/etc/pi-panel-release")
BOOTLOADER_DT = Path("/proc/device-tree/chosen/bootloader")
AUTOBOOT = Path("/bootfs/autoboot.txt")

# Interfaces nobody would ssh to the panel through.
_SKIP_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "podman", "tailscale")
MAX_ADDRESSES = 4


@dataclass(frozen=True, slots=True)
class Address:
    interface: str
    address: str
    family: str          # "inet" | "inet6"


@dataclass(frozen=True, slots=True)
class Release:
    version: str | None                  # None: not an image build
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.version or "development build"


@dataclass(frozen=True, slots=True)
class Info:
    hostname: str
    addresses: tuple[Address, ...]
    release: Release
    slot: str | None                     # "A", "B", "B (trial)"; None: not A/B
    user: str | None                     # who to ssh in as


def hostname() -> str:
    return socket.gethostname().split(".")[0]


def parse_addresses(ip_json: str) -> tuple[Address, ...]:
    """`ip -j addr show scope global` → the addresses worth showing, IPv4
    first, at most MAX_ADDRESSES."""
    try:
        links = json.loads(ip_json or "[]")
    except json.JSONDecodeError:
        return ()
    found: list[Address] = []
    for link in links if isinstance(links, list) else []:
        name = str(link.get("ifname", ""))
        if not name or name.startswith(_SKIP_PREFIXES) or "UP" not in link.get("flags", []):
            continue
        for a in link.get("addr_info", []):
            family = a.get("family")
            if family not in ("inet", "inet6") or a.get("scope") != "global":
                continue
            if a.get("tentative") or a.get("deprecated"):
                continue
            found.append(Address(name, str(a.get("local")), family))
    found.sort(key=lambda a: a.family != "inet")      # stable: IPv4 first
    return tuple(found[:MAX_ADDRESSES])


def addresses() -> tuple[Address, ...]:
    try:
        out = subprocess.run(["ip", "-j", "addr", "show", "scope", "global"],
                             capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("cannot list addresses: %s", exc)
        return ()
    return parse_addresses(out)


def parse_release(text: str) -> Release:
    """os-release style: KEY=value lines, values optionally shell-quoted."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            parts = shlex.split(value)
        except ValueError:
            continue
        fields[key.strip()] = parts[0] if parts else ""
    return Release(fields.get("VERSION") or None, fields)


def release(path: Path = RELEASE_FILE) -> Release:
    try:
        return parse_release(path.read_text(encoding="utf-8"))
    except OSError:
        return Release(None)


def active_slot(dt: Path = BOOTLOADER_DT, autoboot: Path = AUTOBOOT) -> str | None:
    """The A/B slot booted, on an image-rota layout (autoboot.txt with
    tryboot_a_b=1, boot_a = partition 2, boot_b = partition 3). The firmware
    reports the partition it booted, and whether this is a tryboot trial, in
    the device tree. None anywhere else, e.g. a single-slot dev panel."""
    try:
        if "tryboot_a_b=1" not in autoboot.read_text(encoding="utf-8").replace(" ", ""):
            return None
        partition = int.from_bytes((dt / "partition").read_bytes()[:4], "big")
    except (OSError, ValueError):
        return None
    slot = {2: "A", 3: "B"}.get(partition)
    if slot is None:
        return None
    try:
        trial = int.from_bytes((dt / "tryboot").read_bytes()[:4], "big") != 0
    except OSError:
        trial = False
    return f"{slot} (trial)" if trial else slot


def login_user(entries: list[pwd.struct_passwd] | None = None) -> str | None:
    """The first ordinary account: who to ssh in as."""
    for e in sorted(entries if entries is not None else pwd.getpwall(), key=lambda e: e.pw_uid):
        if 1000 <= e.pw_uid < 60000 and not e.pw_shell.endswith(("nologin", "false")):
            return e.pw_name
    return None


def gather() -> Info:
    return Info(hostname(), addresses(), release(), active_slot(), login_user())
