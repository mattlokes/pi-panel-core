# Deployment files

The files that make the compositor the machine's boot session. They live
outside the build, so they are kept here to be version controlled — the target
Pi is the only other copy.

| File | Installs to | Purpose |
|---|---|---|
| `pi-panel-session` | `~/.local/bin/pi-panel-session` | Session launcher: picks the renderer, execs the compositor under `systemd-cat` |
| `bash_profile.example` | `~/.bash_profile` | Starts the session on tty1 only |
| `compositor.conf.example` | `~/.config/pi-panel/compositor.conf` | The views to launch |

## Install

```bash
mkdir -p ~/.local/bin ~/.config/pi-panel
install -m755 deploy/pi-panel-session       ~/.local/bin/pi-panel-session
install -m644 deploy/bash_profile.example   ~/.bash_profile
install -m644 deploy/compositor.conf.example ~/.config/pi-panel/compositor.conf
sudo systemctl set-default multi-user.target
sudo reboot
```

Raspberry Pi OS already autologins `pi` on tty1, and lightdm is only
`WantedBy=graphical.target`, so it stops starting by itself — no need to
disable it. To go back to the desktop:

```bash
sudo systemctl set-default graphical.target && sudo reboot
```

## Notes

- `~/.bash_profile` is read by bash *instead of* `~/.profile`, so it sources
  `~/.profile` first to keep `PATH` and `.bashrc` behaviour.
- It deliberately does **not** `exec` the launcher. Quitting the compositor
  then drops you to a shell on tty1 — the local recovery path — instead of
  ending the login and letting agetty respawn into a loop.
- It is guarded on `tty1` and on `SSH_CONNECTION` being unset, so an SSH login
  never tries to start a compositor.
- Logs go to the journal: `journalctl -t pi-panel -b` (`-f` to follow,
  `-p err` for errors only).
