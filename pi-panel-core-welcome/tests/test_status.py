from pi_panel_welcome.status import summarize


def app(name, enabled=True, unit_state="inactive", mapped=False, kind="app"):
    return {"name": name, "kind": kind, "enabled": enabled, "unit_state": unit_state,
            "mapped": mapped}


def s(**kw):
    base = {"ctl_online": True, "apps": [], "checks": None, "user": "pi",
            "address": "192.168.4.30"}
    return summarize(**{**base, **kw})


def test_ctl_down():
    st = s(ctl_online=False, apps=None)
    assert st.headline == "Starting…"
    assert any("journalctl -u pi-panel-ctl" in ln for ln in st.lines)


def test_nothing_installed():
    st = s(apps=[app("mqtt", kind="plugin")])
    assert st.headline == "No apps installed"
    assert "ssh pi@192.168.4.30" in st.lines[0]


def test_nothing_enabled_lists_what_is_installed():
    st = s(apps=[app("immich", enabled=False), app("now-playing", enabled=False)])
    assert st.headline == "No apps enabled"
    assert st.lines[0] == "Installed: immich, now-playing"
    assert any("panel.toml" in ln for ln in st.lines)


def test_enabled_apps_that_are_not_showing():
    st = s(apps=[app("immich", unit_state="failed"), app("cam", unit_state="active"),
                 app("np", unit_state="active", mapped=True), app("off", enabled=False)])
    assert st.headline == "Waiting for apps"
    assert st.lines[0] == "cam: starting  ·  immich: failed  ·  np: ready"


def test_problems_come_from_failed_and_warning_checks():
    checks = [{"name": "panel.toml", "status": "ok", "detail": ""},
              {"name": "immich", "status": "failed", "detail": "a\nb\nc\nd\ne\nf"},
              {"name": "gone", "status": "warning", "detail": "enabled but not installed"}]
    st = s(apps=[app("immich", enabled=False)], checks=checks)
    assert [(p.name, p.severity) for p in st.problems] == [("immich", "failed"),
                                                           ("gone", "warning")]
    assert st.problems[0].detail == "a\nb\nc\nd\n…"


def test_no_user_or_address():
    st = s(apps=[], user=None, address=None)
    assert st.lines[0].startswith("ssh <panel>,")
