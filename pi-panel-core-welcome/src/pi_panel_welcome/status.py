"""What the welcome screen says about the panel: why nothing else is showing,
what to do about it, and what is wrong with the config. Pure: it takes what
io.pipanel.Ctl reported (ListApps, CheckConfig) and returns text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CONFIG_DIR = "/persistent/pi-panel/config"
MAX_DETAIL_LINES = 4


@dataclass(frozen=True, slots=True)
class Problem:
    name: str
    severity: str        # "failed" | "warning"
    detail: str


@dataclass(frozen=True, slots=True)
class Status:
    headline: str
    lines: tuple[str, ...]
    problems: tuple[Problem, ...]


def _ssh(user: str | None, address: str | None) -> str:
    host = address or "<panel>"
    return f"ssh {user}@{host}" if user else f"ssh {host}"


def _short(detail: str) -> str:
    lines = [ln for ln in detail.strip().splitlines() if ln.strip()]
    if len(lines) > MAX_DETAIL_LINES:
        lines = lines[:MAX_DETAIL_LINES] + ["…"]
    return "\n".join(lines)


def problems(checks: list[dict[str, Any]] | None) -> tuple[Problem, ...]:
    return tuple(Problem(str(r.get("name")), str(r.get("status")), _short(str(r.get("detail", ""))))
                 for r in checks or [] if r.get("status") in ("failed", "warning"))


def summarize(*, ctl_online: bool, apps: list[dict[str, Any]] | None,
              checks: list[dict[str, Any]] | None,
              user: str | None, address: str | None) -> Status:
    ssh = _ssh(user, address)
    if not ctl_online:
        return Status("Starting…", (
            "pi-panel-ctl is not answering yet.",
            f"If this stays: {ssh}, then journalctl -u pi-panel-ctl",
        ), problems(checks))

    all_apps = [a for a in apps or [] if a.get("kind") == "app"]
    enabled = [a for a in all_apps if a.get("enabled")]
    if not all_apps:
        return Status("No apps installed", (
            f"{ssh}, then: pi-panel-pkg install <git-url>",
            "and: pi-panel-ctl enable <name>",
        ), problems(checks))
    if not enabled:
        names = ", ".join(sorted(str(a.get("name")) for a in all_apps))
        return Status("No apps enabled", (
            f"Installed: {names}",
            f"{ssh}, then: pi-panel-ctl tui",
            f"or edit enabled = [...] in {CONFIG_DIR}/panel.toml",
        ), problems(checks))

    waiting = []
    for a in sorted(enabled, key=lambda a: str(a.get("name"))):
        state = str(a.get("unit_state") or "unknown")
        if state in ("activating", "reloading") or (state == "active" and not a.get("mapped")):
            what = "starting"
        elif state == "active":
            what = "ready"
        else:
            what = state
        waiting.append(f"{a.get('name')}: {what}")
    return Status("Waiting for apps", (
        "  ·  ".join(waiting),
        f"Logs: {ssh}, then journalctl -u 'pi-panel-app@*'",
    ), problems(checks))
