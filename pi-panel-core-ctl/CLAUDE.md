# pi-panel-core-ctl

This is the central daemon of pi-panel (`pi-panel-ctld`), with its CLI and TUI.
It is also a uv workspace holding the shared libraries used by every other
pi-panel repo:
- `packages/pi-panel-varlink`
- `packages/pi-panel-tui-kit`

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
- **Subscribers never block ctl.** `Broadcaster` drops a subscriber that falls behind; clients resubscribe and get a fresh snapshot.
