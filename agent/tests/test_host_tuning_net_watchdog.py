"""host_tuning.ensure_net_watchdog — the self-heal that keeps the gateway-loss
net-watchdog installed + enabled on hosts that only ever self-updated.

The net-watchdog units are installed by install_agent.sh Phase G, but the agent
SELF-UPDATE path only swaps the code tree + restarts — it never (re)installs the
units or enables the timer. So a host that got the net-watchdog release via
self-update alone had NO gateway-loss reboot (the cs-svr-05 symptom: offline for
a long time, never rebooting). ensure_net_watchdog re-asserts it every start.
"""
import pathlib
import sys

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import host_tuning  # noqa: E402


def _stub_units(tmp_path, monkeypatch, *, ship=True):
    """Point the source install dir + destinations at a tmp tree. Returns the
    {name: dest} map so tests can assert file contents."""
    src_dir = tmp_path / "install"
    src_dir.mkdir()
    dests = {}
    units = {}
    for name, (_dest, mode) in host_tuning.NET_WATCHDOG_UNITS.items():
        if ship:
            (src_dir / name).write_text(f"contents-of-{name}\n")
        dest = tmp_path / "sys" / name
        dests[name] = dest
        units[name] = (str(dest), mode)
    monkeypatch.setattr(host_tuning, "_install_dir", lambda: str(src_dir))
    monkeypatch.setattr(host_tuning, "NET_WATCHDOG_UNITS", units)
    return dests


def _record_run(calls, *, enabled=False, active=False):
    def _run(argv, timeout=20):
        calls.append(argv)
        if argv[:2] == ["systemctl", "is-enabled"]:
            return (0 if enabled else 1), ""
        if argv[:2] == ["systemctl", "is-active"]:
            return (0 if active else 1), ""
        return 0, ""
    return _run


def test_installs_units_and_enables_timer_when_missing(tmp_path, monkeypatch):
    dests = _stub_units(tmp_path, monkeypatch)
    calls = []
    # After enable, subsequent is-enabled/is-active report success.
    state = {"enabled": False, "active": False}

    def _run(argv, timeout=20):
        calls.append(argv)
        if argv[:3] == ["systemctl", "enable", "--now"]:
            state["enabled"] = state["active"] = True
            return 0, ""
        if argv[:2] == ["systemctl", "is-enabled"]:
            return (0 if state["enabled"] else 1), ""
        if argv[:2] == ["systemctl", "is-active"]:
            return (0 if state["active"] else 1), ""
        return 0, ""

    monkeypatch.setattr(host_tuning, "_run", _run)

    res = host_tuning.ensure_net_watchdog()

    # Every unit file was copied to its destination with the shipped contents.
    for name, dest in dests.items():
        assert dest.is_file(), f"{name} not installed"
        assert dest.read_text() == f"contents-of-{name}\n"
    assert res["enabled"] and res["active"]
    assert ["systemctl", "daemon-reload"] in calls          # units changed
    assert any(a[:3] == ["systemctl", "enable", "--now"] for a in calls)


def test_idempotent_when_present_and_enabled(tmp_path, monkeypatch):
    dests = _stub_units(tmp_path, monkeypatch)
    # Pre-stage identical destination files so nothing needs copying.
    for name, dest in dests.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"contents-of-{name}\n")
    calls = []
    monkeypatch.setattr(host_tuning, "_run",
                        _record_run(calls, enabled=True, active=True))

    res = host_tuning.ensure_net_watchdog()

    assert res["enabled"] and res["active"]
    assert res["copied"] == []                               # no drift → no copy
    assert ["systemctl", "daemon-reload"] not in calls
    assert not any(a[:3] == ["systemctl", "enable", "--now"] for a in calls)


def test_enables_timer_even_when_units_already_present(tmp_path, monkeypatch):
    """The core cs-svr-05 case: unit FILES exist (self-update dropped them into
    the install dir → dest) but the timer was never enabled. Must enable it."""
    dests = _stub_units(tmp_path, monkeypatch)
    for name, dest in dests.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"contents-of-{name}\n")
    calls = []
    state = {"enabled": False}

    def _run(argv, timeout=20):
        calls.append(argv)
        if argv[:3] == ["systemctl", "enable", "--now"]:
            state["enabled"] = True
            return 0, ""
        if argv[:2] == ["systemctl", "is-enabled"]:
            return (0 if state["enabled"] else 1), ""
        if argv[:2] == ["systemctl", "is-active"]:
            return (0 if state["enabled"] else 1), ""
        return 0, ""

    monkeypatch.setattr(host_tuning, "_run", _run)

    res = host_tuning.ensure_net_watchdog()

    assert res["copied"] == []                               # files already good
    assert any(a[:3] == ["systemctl", "enable", "--now"] for a in calls)
    assert res["enabled"] and res["active"]


def test_never_raises_and_skips_when_source_absent(tmp_path, monkeypatch):
    _stub_units(tmp_path, monkeypatch, ship=False)           # no source files
    calls = []
    monkeypatch.setattr(host_tuning, "_run",
                        _record_run(calls, enabled=True, active=True))
    # Should not copy anything (no source) but still verify/enable the timer.
    res = host_tuning.ensure_net_watchdog()
    assert res["copied"] == []
