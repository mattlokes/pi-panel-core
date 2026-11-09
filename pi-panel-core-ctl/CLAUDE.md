# pi-panel-core-ctl

This is the central daemon of pi-panel (`pi-panel-ctld`), with its CLI and TUI.
It is also a uv workspace holding the shared libraries used by every other
pi-panel repo, and the MQTT bridge:
- `packages/pi-panel-varlink`
- `packages/pi-panel-tui-kit`
- `packages/pi-panel-mqtt`: `pi-panel-mqtt.service`, which ctl runs from panel.toml's `[mqtt]` (`reconcile_core_units`). It has its own CLAUDE.md.

The root [README](../README.md) has the architecture diagram and the install procedure.

```bash
uv sync --all-packages && uv run pytest      # offline: fake compositor + fake units
```

The jazz notes and the layout contract shared by every pi-panel repo are in
the root [CLAUDE.md](../CLAUDE.md).

## Design points worth preserving

- **One reconcile pass does the work.** Every input only calls `kick()`: compositor events, unit changes, Varlink calls and timers. `_pass()` then compares what the engine wants with what the compositor reports. Keep new behaviour inside that pass, not in event handlers; that is what makes missed or duplicate events harmless.
- **`engine.py` is pure**: no I/O, and the clock is injected. Every priority and timing rule is tested exactly there, including schedules that cross midnight, which belong to the day they *start*.
- **Never switch during a fade.** A new switch waits until the compositor reports `transitioning=false`; the `transition_finished` event kicks the next pass.
- **A hold on an unmapped app waits.** The engine decides it; the daemon switches once the window maps. The rotation does not quietly take over.
- **Slots are re-registered on every compositor snapshot**, because a restarted compositor knows nothing.
- **Unit control is D-Bus with `Manager.Subscribe()`**, low level (`dbus_fast.Message`), with no introspection. `FakeUnits` mirrors its interface for tests. `--user-units` drives the user manager instead, for development.
- **Varlink `.varlink` files are served verbatim** and must parse with `varlinkctl validate-idl`; a test checks this. `org.varlink.service.ExpectedMore` is the standard error for a streaming method called without `more`.
- **The clock overlay is reconciled like everything else.** `_sync_clock()` runs in the pass and compares panel.toml's `[clock]` with the compositor's reported `status.clock`. `v_SetClock` only saves and kicks. A refused setting is remembered, so it is not resent on every pass.
- **panel.toml is merged, never rewritten.** People edit it by hand. `CtlConfig.save` takes the `base` from the last load or save and writes only the keys that changed since, into the tomlkit document read from disk, so comments and hand edits to other keys survive. `Reload` (Varlink, SIGHUP, `pi-panel-ctl reload`) applies hand edits; an invalid file is refused and the running config stays. Every load error, a TOML syntax error included, is a `ValidationError` naming the file.
- **The welcome screen is the fallback, not an app.** `reconcile_core_units` always registers the slot `welcome` (matched by `pi-panel-welcome.service`) and keeps that unit running. `_pass()` switches to it only when the engine decides `reason="none"`, so a power-off schedule or a hold on an unmapped app still wins. It is never in `packages`, `ListApps` or a rotation, and `GetState.showing` reports `null` while it is on screen. When the pass sees the compositor come back online, it re-runs `reconcile_packages`: `BindsTo=` stopped the Wayland clients, and systemd does not restart a unit it stopped on purpose.
- **`CheckConfig` runs the same checks as `pi-panel-ctl config check`** (`configdir.check`), off the loop in a thread, with one run at a time shared by every caller. It reads panel.toml from disk, so it reports a file that `Reload` refused.
- **Subscribers never block ctl.** `Broadcaster` drops a subscriber that falls behind; clients resubscribe and get a fresh snapshot.
