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
#     /persistent/pi-panel/home       the service user's home (app caches)
#
# The panel runs as the system user pi-panel (group pi-panel, also in video,
# render and input), so the login user can be anyone. Login users join the
# pi-panel group: that gives them the sockets in /run/pi-panel, the config and
# state directories (setgid, group-writable) and, through polkit, the
# pi-panel-* units. Name the login user to add with PI_PANEL_LOGIN_USER; under
# sudo it defaults to the user who ran sudo.
#
# Also used in an image build's chroot: with no running systemd it writes the
# linger file itself and skips daemon-reload and the live migration steps.
#
# It expects the four core components built and synced first, in place (no
# root needed; venvs and the compositor's font path are not movable):
#
#     pi-panel-core-compositor   meson setup build && ninja -C build
#     pi-panel-core-ctl          uv sync --frozen --compile-bytecode --all-packages
#     pi-panel-core-pluginman    uv sync --frozen --compile-bytecode
#     pi-panel-core-welcome      uv venv --system-site-packages --python /usr/bin/python3 --allow-existing
#                                && uv sync --frozen --compile-bytecode
#
# Safe to run again after pulling updates: it only copies files, links the
# commands, creates missing directories and reloads systemd. It never starts
# or stops the running panel.
set -eu

PANEL_USER=pi-panel                         # the unit files hard-code it
LOGIN_USER=${PI_PANEL_LOGIN_USER:-${SUDO_USER:-}}
CORE=$(cd "$(dirname "$0")/.." && pwd)
OPT=/opt/pi-panel
PERSISTENT=/persistent/pi-panel
COMPOSITOR=$CORE/pi-panel-core-compositor
CTL=$CORE/pi-panel-core-ctl
PLUGINMAN=$CORE/pi-panel-core-pluginman
WELCOME=$CORE/pi-panel-core-welcome
PANEL_HOME=$PERSISTENT/home
# A running systemd (not a chroot or container build).
if [ -d /run/systemd/system ]; then LIVE=1; else LIVE=; fi

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0" >&2
    exit 1
fi
if [ -n "$LOGIN_USER" ] && ! getent passwd "$LOGIN_USER" >/dev/null; then
    echo "no such login user: $LOGIN_USER (PI_PANEL_LOGIN_USER)" >&2
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
need "$WELCOME/.venv/bin/pi-panel-welcome"     "in $WELCOME: uv venv --system-site-packages --python /usr/bin/python3 --allow-existing && uv sync --frozen --compile-bytecode"

# The unit files hard-code the layout and User=pi-panel.
if [ "$CORE" != "$OPT/pi-panel-core" ]; then
    echo "warning: this checkout is $CORE, but the unit files run $OPT/pi-panel-core" >&2
fi

echo "== service user $PANEL_USER"
install -d -m 0755 "$PERSISTENT"
if ! getent group "$PANEL_USER" >/dev/null; then
    groupadd --system "$PANEL_USER"
    echo "   created group $PANEL_USER"
fi
if ! getent passwd "$PANEL_USER" >/dev/null; then
    useradd --system --gid "$PANEL_USER" --home-dir "$PANEL_HOME" --no-create-home \
            --shell /usr/sbin/nologin --comment "pi-panel" "$PANEL_USER"
    echo "   created user $PANEL_USER"
fi
# video/render: DRM and the GPU; input: libinput for the touchscreen.
for g in video render input; do
    if getent group "$g" >/dev/null; then usermod -aG "$g" "$PANEL_USER"; fi
done
install -d -m 0750 -o "$PANEL_USER" -g "$PANEL_USER" "$PANEL_HOME"
if [ -n "$LOGIN_USER" ]; then
    usermod -aG "$PANEL_USER" "$LOGIN_USER"
    echo "   $LOGIN_USER is in group $PANEL_USER (takes effect at the next login)"
fi

echo "== $PERSISTENT/{config,state} ($PANEL_USER:$PANEL_USER, setgid, group-writable)"
for d in "$PERSISTENT/config" "$PERSISTENT/state"; do
    install -d -o "$PANEL_USER" -g "$PANEL_USER" "$d"
    # Hand over what an install as another user (pi, before the service user)
    # left behind, and keep everything group-shared: setgid directories, group
    # read/write on files. Others get nothing: panel.toml can hold passwords.
    chown -hR "$PANEL_USER:$PANEL_USER" "$d"
    chmod -R g+rwX,o-rwx "$d"
    find "$d" -type d -exec chmod g+s {} +
done
# The image registry is read by the service user (an older pluginman wrote it 0600).
if [ -f "$OPT/packages.json" ]; then chmod a+r "$OPT/packages.json"; fi

echo "== systemd units"
for f in \
    "$CORE/deploy/systemd/pi-panel.target" \
    "$CORE/deploy/systemd/pi-panel.slice" \
    "$CTL/deploy/systemd/pi-panel-ctl.service" \
    "$CTL/deploy/systemd/pi-panel-mqtt.service" \
    "$WELCOME/deploy/systemd/pi-panel-welcome.service" \
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

echo "== polkit (lets $PANEL_USER and its group manage pi-panel-* units without a password)"
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
if [ -n "$LIVE" ]; then
    loginctl enable-linger "$PANEL_USER"
else
    install -d -m 0755 /var/lib/systemd/linger
    touch "/var/lib/systemd/linger/$PANEL_USER"
fi

if [ -n "$LIVE" ]; then
    echo "== migrating from the old layout"
    # The MQTT bridge used to be the plugin pi-panel-plugin@mqtt; it is now
    # pi-panel-mqtt.service, which ctl runs from panel.toml's [mqtt]. Stop the
    # old instance: a restart of pi-panel.target would restart it too
    # (PartOf=), and it then fails in a loop, since mqtt is no longer an
    # installed package.
    if systemctl is-active --quiet pi-panel-plugin@mqtt.service 2>/dev/null \
       || systemctl is-failed --quiet pi-panel-plugin@mqtt.service 2>/dev/null; then
        systemctl stop pi-panel-plugin@mqtt.service
        systemctl reset-failed pi-panel-plugin@mqtt.service 2>/dev/null || true
        echo "   stopped pi-panel-plugin@mqtt.service (the bridge is now pi-panel-mqtt.service)"
    fi
    LOGIN_HOME=$( [ -n "$LOGIN_USER" ] && getent passwd "$LOGIN_USER" | cut -d: -f6 || true)
    if [ -n "$LOGIN_HOME" ] && [ -f "$LOGIN_HOME/.bash_profile" ] \
       && grep -q pi-panel-session "$LOGIN_HOME/.bash_profile"; then
        mv "$LOGIN_HOME/.bash_profile" "$LOGIN_HOME/.bash_profile.pre-pi-panel"
        echo "   moved ~$LOGIN_USER/.bash_profile aside (it started the old compositor on tty1)"
    fi
fi

echo "== enabling"
if [ -n "$LIVE" ]; then systemctl daemon-reload; fi
systemctl set-default multi-user.target >/dev/null
systemctl enable pi-panel.target pi-panel-compositor.service pi-panel-ctl.service

cat <<EOF

Installed. The panel runs as $PANEL_USER. Restart it to pick up the new user
(a running panel keeps its old one), with a reboot or:

    systemctl restart pi-panel.target

(Starting it takes over tty1: any login there, including an old compositor
started from ~/.bash_profile, is ended.)

Then, as a login user in the $PANEL_USER group (log in again after being
added), install and enable apps:

    pi-panel-pkg install <git-url-or-tarball>
    pi-panel-ctl enable <name>

or do both interactively:

    pi-panel-pkg tui        packages
    pi-panel-ctl tui        the panel: what is shown, rotation, apps
EOF
