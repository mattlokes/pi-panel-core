import json
import pwd

from pi_panel_welcome import info

IP_JSON = json.dumps([
    {"ifname": "lo", "flags": ["LOOPBACK", "UP"],
     "addr_info": [{"family": "inet", "local": "127.0.0.1", "scope": "host"}]},
    {"ifname": "eth0", "flags": ["BROADCAST", "UP", "LOWER_UP"], "addr_info": [
        {"family": "inet6", "local": "fd3c::1", "scope": "global"},
        {"family": "inet6", "local": "fe80::1", "scope": "link"},
        {"family": "inet", "local": "192.168.4.30", "scope": "global"}]},
    {"ifname": "wlan0", "flags": ["BROADCAST", "UP"], "addr_info": [
        {"family": "inet", "local": "192.168.4.31", "scope": "global"},
        {"family": "inet6", "local": "fd3c::2", "scope": "global", "tentative": True}]},
    {"ifname": "docker0", "flags": ["UP"],
     "addr_info": [{"family": "inet", "local": "172.17.0.1", "scope": "global"}]},
    {"ifname": "eth1", "flags": ["BROADCAST"],
     "addr_info": [{"family": "inet", "local": "10.0.0.9", "scope": "global"}]},
])


def test_addresses_ipv4_first_and_skip_the_uninteresting():
    got = info.parse_addresses(IP_JSON)
    assert [(a.interface, a.address) for a in got] == [
        ("eth0", "192.168.4.30"), ("wlan0", "192.168.4.31"), ("eth0", "fd3c::1")]


def test_addresses_tolerate_garbage():
    assert info.parse_addresses("") == ()
    assert info.parse_addresses("not json") == ()
    assert info.parse_addresses("{}") == ()


def test_release_file(tmp_path):
    f = tmp_path / "pi-panel-release"
    f.write_text('# pi-panel\nVERSION="2026.11.1"\nCORE_COMMIT=abc123\nBUILD_DATE=\'2026-11-20\'\n')
    r = info.release(f)
    assert r.version == "2026.11.1" and r.label == "2026.11.1"
    assert r.fields["CORE_COMMIT"] == "abc123" and r.fields["BUILD_DATE"] == "2026-11-20"


def test_no_release_file_is_a_development_build(tmp_path):
    r = info.release(tmp_path / "missing")
    assert r.version is None and r.label == "development build"


def _bootloader(tmp_path, partition: int, tryboot: int, autoboot: str):
    dt = tmp_path / "bootloader"
    dt.mkdir()
    (dt / "partition").write_bytes(partition.to_bytes(4, "big"))
    (dt / "tryboot").write_bytes(tryboot.to_bytes(4, "big"))
    ab = tmp_path / "autoboot.txt"
    ab.write_text(autoboot)
    return dt, ab


AB = "[all]\ntryboot_a_b=1\nboot_partition=2\n[tryboot]\nboot_partition=3\n"


def test_slot_a(tmp_path):
    assert info.active_slot(*_bootloader(tmp_path, 2, 0, AB)) == "A"


def test_slot_b_on_trial(tmp_path):
    assert info.active_slot(*_bootloader(tmp_path, 3, 1, AB)) == "B (trial)"


def test_no_slot_without_ab_autoboot(tmp_path):
    assert info.active_slot(*_bootloader(tmp_path, 2, 0, "boot_partition=2\n")) is None
    assert info.active_slot(tmp_path / "nope", tmp_path / "nope.txt") is None


def _pw(name, uid, shell="/bin/bash"):
    return pwd.struct_passwd((name, "x", uid, uid, "", f"/home/{name}", shell))


def test_login_user_is_the_first_ordinary_account():
    entries = [_pw("root", 0), _pw("nobody", 65534, "/usr/sbin/nologin"),
               _pw("pi-panel", 999, "/usr/sbin/nologin"), _pw("matt", 1001), _pw("pi", 1000)]
    assert info.login_user(entries) == "pi"
    assert info.login_user([_pw("root", 0)]) is None
