# pi-panel-core-pluginman

The package manager (`pi-panel-pkg`, CLI and TUI), the launcher
(`pi-panel-run`), and the two unit templates that run every package
(`pi-panel-app@`, `pi-panel-plugin@`). The README covers usage; `docs/` holds
the manifest format and the app contract.

```bash
uv sync && uv run pytest        # offline, no Pi, no ctl: everything is faked or local
```

The shared libraries come from `../pi-panel-core-ctl/packages/` by relative path. The layout contract
shared by every pi-panel repo is in the root [CLAUDE.md](../CLAUDE.md).

## Design points worth preserving

- **No per-package unit files.** Both templates run `pi-panel-run <kind> <name>`, which reads the manifest and `exec`s. That is why installing needs no root or `daemon-reload`. Keep `pi-panel-run` an `exec`, never a supervisor, so systemd sees the real process.
- **Install location is part of the dependency story.** Packages go to `~/pi-panel-apps/<name>` and `~/pi-panel-plugin/<name>`, next to `~/pi-panel-core`, so `../../pi-panel-core/pi-panel-core-ctl/packages/pi-panel-varlink` resolves for every package. Moving them breaks that path. The relative path, rather than a git URL, is deliberate: it works offline and ties every package to the core actually installed.
- **Installs are staged, and builds roll back.** `Manager.install` moves the old tree aside, builds in place, and restores it on failure. The tests cover first installs, updates, and failed builds.
- **Tarballs use `extractall(filter="data")`.** Don't replace it with a hand-rolled check.
- **`packages.json` is read by pi-panel-ctl** (`pi_panel_ctl/packages.py`). The fields ctl uses are `name`, `kind`, `version`, `description`, `path` and `varlink`. Change them in both places.
- **`PI_PANEL_*` variables set by `pi-panel-run` beat the manifest's `[run].env`**, on purpose.
- **ctl is optional here.** `pi-panel-pkg` must work when ctl is down, e.g. on a first install before the panel runs. ctl calls are best-effort and print a note.
- **The TUI shares `pi-panel-tui-kit` with `pi-panel-ctl tui`.** Layout and key conventions live in the kit, not here.
