# pi-panel-core-welcome

The welcome screen. See the README for what it shows; the root
[CLAUDE.md](../CLAUDE.md) has the layout contract.

## Design points worth preserving

- **GTK only under `ui/`.** `info.py` (device facts), `status.py` (what to say) and `ctlclient.py` (ctl over Varlink) import no GTK and are tested offline. Readers take their paths or input, so tests use fixtures.
- **ctl owns when it shows.** The program never asks to be shown: ctl switches to the slot `welcome` when its engine has nothing to show. The program only knows it is on screen from `GetState.showing` being null, which it uses to re-run the config check every 30 s.
- **No `Gtk.Application`**: it wants a session bus. `Gtk.init_check()` plus a `GLib.MainLoop`, as in the GTK apps. Varlink runs on its own asyncio thread and hands results to GTK with `GLib.idle_add`.
- **Plan 5's update and OTA status** goes in `WelcomeWindow.updates`, the reserved band along the bottom.
