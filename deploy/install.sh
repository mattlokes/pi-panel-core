#!/bin/sh
# Install pi-panel's system files. The one step of a pi-panel install that
# needs root:
#
#     sudo /opt/pi-panel/pi-panel-core/deploy/install.sh
#
# The layout (the unit files and the code's defaults assume it):
#
#     /opt/pi-panel/pi-panel-core     this repo, built in place
#     /opt/pi-panel/pi-panel-apps     installed apps   (pi-panel-pkg)
#     /persistent/pi-panel/config     panel.toml, apps/<name>/config.toml, compositor.env
#     /persistent/pi-panel/state      packages.json, staging
#
# It expects the three core components built and synced first, in place (as
# the user, no root; venvs and the compositor's font path are not movable):
#
#     pi-panel-core-compositor   meson setup build && ninja -C build
#     pi-panel-core-ctl          uv sync --frozen --compile-bytecode --all-packages
#     pi-panel-core-pluginman    uv sync --frozen --compile-bytecode
#
# Safe to run again after pulling updates: it only copies files, links the
# commands, creates missing directories and reloads systemd. It never starts
# or stops the running panel.
set -eu

PANEL_USER=${PI_PANEL_USER:-pi}
CORE=$(cd "$(dirname "$0")/.." && pwd)
OPT=/opt/pi-panel
PERSISTENT=/persistent/pi-panel
COMPOSITOR=$CORE/pi-panel-core-compositor
CTL=$CORE/pi-panel-core-ctl
PLUGINMAN=$CORE/pi-panel-core-pluginman
USER_HOME=$(getent passwd "$PANEL_USER" | cut -d: -f6)

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi
if [ -z "$USER_HOME" ]; then
    echo "no such user: $PANEL_USER" >&2
    exit 1
fi

need() {
    if [ ! -e "$1" ]; then
        echo "missing: $1" >&2
        echo "  ($2)" >&2
        exit 1
    fi
}
need "$COMPOSITOR/build/pi-panel-compositor" "build the compositor: meson setup build && ninja -C build"
need "$COMPOSITOR/deploy/start"               "pull the latest pi-panel-core"
need "$CTL/.venv/bin/pi-panel-ctld"            "in $CTL: uv sync --frozen --compile-bytecode --all-packages"
need "$CTL/.venv/bin/pi-panel-mqtt"            "in $CTL: uv sync --frozen --compile-bytecode --all-packages"
need "$PLUGINMAN/.venv/bin/pi-panel-run"       "in $PLUGINMAN: uv sync --frozen --compile-bytecode"

# The unit files hard-code the layout and User=pi.
if [ "$CORE" != "$OPT/pi-panel-core" ]; then
    echo "warning: this checkout is $CORE, but the unit files run $OPT/pi-panel-core" >&2
fi
if [ "$PANEL_USER" != pi ]; then
    echo "warning: the unit files assume User=pi; edit them for $PANEL_USER" >&2
fi

echo "== $PERSISTENT (config and state, owned by $PANEL_USER)"
install -d -m 0755 "$PERSISTENT"
install -d -m 0700 -o "$PANEL_USER" -g "$PANEL_USER" "$PERSISTENT/config"
install -d -m 0755 -o "$PANEL_USER" -g "$PANEL_USER" "$PERSISTENT/state"

echo "== systemd units"
for f in \
    "$CORE/deploy/systemd/pi-panel.target" \
    "$CORE/deploy/systemd/pi-panel.slice" \
    "$CTL/deploy/systemd/pi-panel-ctl.service" \
    "$CTL/deploy/systemd/pi-panel-mqtt.service" \
    "$COMPOSITOR/deploy/systemd/pi-panel-compositor.service" \
    "$PLUGINMAN/deploy/systemd/pi-panel-app@.service" \
    "$PLUGINMAN/deploy/systemd/pi-panel-plugin@.service"
do
    need "$f" "pull the latest pi-panel-core"
    install -m 0644 "$f" /etc/systemd/system/
    echo "   $(basename "$f")"
done

echo "== PAM (the compositor's logind session on tty1)"
install -m 0644 "$COMPOSITOR/deploy/pam.d/pi-panel" /etc/pam.d/pi-panel

echo "== polkit (lets $PANEL_USER manage pi-panel-* units without a password)"
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$CORE/deploy/polkit/50-pi-panel.rules" /etc/polkit-1/rules.d/

echo "== commands on PATH (/usr/local/bin)"
# Symlinks into the venvs: uv's console scripts carry an absolute shebang, so
# they run from anywhere, and a later `uv sync` rewrites the targets in place,
# so the links never go stale. /usr/local/bin is on PATH for every shell,
# including one-shot `ssh host pi-panel-ctl status`, which reads no profile.
for pair in \
    "pi-panel-ctl:$CTL/.venv/bin/pi-panel-ctl" \
    "pi-panel-ctld:$CTL/.venv/bin/pi-panel-ctld" \
    "pi-panel-pkg:$PLUGINMAN/.venv/bin/pi-panel-pkg" \
    "pi-panel-run:$PLUGINMAN/.venv/bin/pi-panel-run"
do
    name=${pair%%:*}
    target=${pair#*:}
    link=/usr/local/bin/$name
    need "$target" "sync the venvs first (see the top of this script)"
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        echo "   skipped $name: $link exists and is not a symlink" >&2
        continue
    fi
    ln -sfn "$target" "$link"
    echo "   $name"
done

# No compositor.env is written: pi-panel-core-compositor/deploy/start picks
# the renderer at every start. Overrides go in $PERSISTENT/config/compositor.env
# (an old /etc/pi-panel/compositor.env is still read).

echo "== linger for $PANEL_USER (keeps /run/user/<uid> across compositor restarts)"
loginctl enable-linger "$PANEL_USER"

echo "== migrating from the old layout"
# The MQTT bridge used to be the plugin pi-panel-plugin@mqtt; it is now
# pi-panel-mqtt.service, which ctl runs from panel.toml's [mqtt]. Stop the old
# instance: a restart of pi-panel.target would restart it too (PartOf=), and
# it then fails in a loop, since mqtt is no longer an installed package.
if systemctl is-active --quiet pi-panel-plugin@mqtt.service 2>/dev/null \
   || systemctl is-failed --quiet pi-panel-plugin@mqtt.service 2>/dev/null; then
    systemctl stop pi-panel-plugin@mqtt.service
    systemctl reset-failed pi-panel-plugin@mqtt.service 2>/dev/null || true
    echo "   stopped pi-panel-plugin@mqtt.service (the bridge is now pi-panel-mqtt.service)"
fi
if [ -f "$USER_HOME/.bash_profile" ] && grep -q pi-panel-session "$USER_HOME/.bash_profile"; then
    mv "$USER_HOME/.bash_profile" "$USER_HOME/.bash_profile.pre-pi-panel"
    echo "   moved ~/.bash_profile aside (it started the old compositor on tty1)"
fi

echo "== enabling"
systemctl daemon-reload
systemctl set-default multi-user.target >/dev/null
systemctl enable pi-panel.target pi-panel-compositor.service pi-panel-ctl.service

cat <<EOF

Installed. Start the panel with a reboot, or now with:

    systemctl start pi-panel.target

(Starting it takes over tty1: any login there, including an old compositor
started from ~/.bash_profile, is ended.)

Then, as $PANEL_USER, install and enable apps:

    pi-panel-pkg install <git-url-or-tarball>
    pi-panel-ctl enable <name>

or do both interactively:

    pi-panel-pkg tui        packages
    pi-panel-ctl tui        the panel: what is shown, rotation, apps
EOF
