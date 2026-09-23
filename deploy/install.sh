#!/bin/sh
# Install pi-panel's system files. The one step of a pi-panel install that
# needs root:
#
#     sudo ~/pi-panel-core/deploy/install.sh
#
# It expects the three core components built and synced first (as the user,
# no root):
#
#     ~/pi-panel-core/pi-panel-core-compositor   meson setup build && ninja -C build
#     ~/pi-panel-core/pi-panel-core-ctl          uv sync --frozen --compile-bytecode --all-packages
#     ~/pi-panel-core/pi-panel-core-pluginman    uv sync --frozen --compile-bytecode
#
# Safe to run again after pulling updates: it only copies files, links the
# commands, and reloads systemd. It never starts or stops the running panel.
set -eu

PANEL_USER=${PI_PANEL_USER:-pi}
CORE=$(cd "$(dirname "$0")/.." && pwd)
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
need "$CTL/.venv/bin/pi-panel-ctld"            "in $CTL: uv sync --frozen --compile-bytecode --all-packages"
need "$PLUGINMAN/.venv/bin/pi-panel-run"       "in $PLUGINMAN: uv sync --frozen --compile-bytecode"

# The unit files hard-code /home/pi and User=pi.
if [ "$PANEL_USER" != pi ] || [ "$USER_HOME" != /home/pi ]; then
    echo "warning: the unit files assume user pi with home /home/pi; edit them for $PANEL_USER" >&2
fi

echo "== systemd units"
for f in \
    "$CORE/deploy/systemd/pi-panel.target" \
    "$CORE/deploy/systemd/pi-panel.slice" \
    "$CTL/deploy/systemd/pi-panel-ctl.service" \
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

echo "== /etc/pi-panel/compositor.env"
install -d -m 0755 /etc/pi-panel
if [ ! -e /etc/pi-panel/compositor.env ]; then
    {
        echo "# Environment for pi-panel-compositor.service (WLR_RENDERER, WLR_DRM_DEVICES, ...)"
        # Pis without a GPU MMU need the software renderer; labwc-pi makes the same check.
        if command -v raspi-config >/dev/null 2>&1 && raspi-config nonint is_pi \
           && ! raspi-config nonint gpu_has_mmu 2>/dev/null; then
            echo "WLR_RENDERER=pixman"
        fi
    } > /etc/pi-panel/compositor.env
    echo "   written"
else
    echo "   kept existing"
fi

echo "== linger for $PANEL_USER (keeps /run/user/<uid> across compositor restarts)"
loginctl enable-linger "$PANEL_USER"

echo "== migrating from the old layout"
if systemctl is-enabled --quiet pi-panel-mqtt.service 2>/dev/null; then
    systemctl disable --now pi-panel-mqtt.service
    echo "   disabled pi-panel-mqtt.service (reinstall it as the mqtt plugin)"
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
