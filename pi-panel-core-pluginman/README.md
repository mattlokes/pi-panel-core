# pi-panel-core-pluginman

The pi-panel package manager. It installs **apps** (things shown on screen)
and **plugins** (background helpers such as the MQTT bridge) from a git
repository, a tarball, or a local directory.

```bash
pi-panel-pkg install https://github.com/you/pi-panel-app-weather.git   # or url#tag
pi-panel-pkg install ./release-1.2.tar.gz
pi-panel-pkg list
pi-panel-pkg update [name]
pi-panel-pkg remove name [--purge]
pi-panel-pkg new app weather        # start a new package from the template
pi-panel-pkg tui                    # all of the above, interactively
```

Installing needs no root and no `daemon-reload`. After installing a package,
enable it with `pi-panel-ctl enable <name>`, or with `e` in the TUI.

## How a package runs

Every package has a `pi-panel.toml` at its root; see
[docs/manifest.md](docs/manifest.md). pluginman does not generate unit files.
Two generic templates run every package:

| Unit | Runs | Started by |
|---|---|---|
| `pi-panel-app@<name>.service` | an app, bound to the compositor | pi-panel-ctl, when enabled |
| `pi-panel-plugin@<name>.service` | a plugin | pi-panel-ctl, when enabled |

Both units run `pi-panel-run <kind> <name>`. It reads the manifest, sets up the
environment, changes into the package directory, and `exec`s the package's
command. systemd therefore supervises the real program directly: its logs
appear under `journalctl -u pi-panel-app@<name>`, and signals reach it.

Apps must follow the [app contract](docs/app-contract.md): one fullscreen
Wayland window, exit promptly on SIGTERM, and optionally serve
`io.pipanel.App` for visibility and actions.

## Where things go

```
~/pi-panel-core/          the core (see its README)
~/pi-panel-apps/<name>/   installed apps
~/pi-panel-plugin/<name>/ installed plugins
~/.config/pi-panel/apps/<name>/     an app's config (from its templates; kept across updates)
~/.local/share/pi-panel/packages.json   the registry pi-panel-ctl reads
```

Installed packages sit next to the core so that one relative path reaches
the shared libraries on any machine:
`../../pi-panel-core/pi-panel-core-ctl/packages/pi-panel-varlink`.

## Install and update safety

- **Staged installs.** A package is fetched and checked in a scratch directory before anything is replaced.
- **Failed builds roll back.** If the build fails, the previous version is restored, or a first install is removed, so an app is never left half-installed.
- **Tarballs can't escape.** Tarballs are extracted with `filter="data"`: absolute paths, `..`, device files and links pointing out of the tree are refused.
- **Config survives updates.** A package's config files are never overwritten by an update.
- **Packages run as you.** A package's build command and program run as the panel user, so only install packages you trust.

## Development

```bash
uv sync
uv run pytest
```

The shared libraries (`pi-panel-varlink`, `pi-panel-tui-kit`) come from
`../pi-panel-core-ctl` in the same repo.
