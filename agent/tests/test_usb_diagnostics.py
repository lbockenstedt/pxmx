"""Missing-dongle roster + probable-cause correlation (``usb_diagnostics``).

The roster is the only record that a dongle used to be on this host at all — a
device that falls off the bus leaves no sysfs trace, so without persistence a
loss accumulating over days is simply invisible. These lock in:

* a bus that stops appearing becomes "missing" with a stamped ``missing_since``;
* the stamp does NOT drift on later ticks (the age must keep growing);
* a reappearing dongle clears the stamp (a flapper isn't reported gone);
* the roster survives a reload (it is what makes multi-day decay visible);
* an entry expires after ROSTER_TTL_S (removed hardware stops being "missing");
* correlation ranks kernel evidence tied to the missing bus above host-wide
  signals, and reports a clean log and an UNREADABLE log as different outcomes —
  "no evidence" and "no data" imply different fixes.

Synthetic-package loader mirrors test_usb_quarantine_strikes so the module's
``from . import ...`` resolves. Tmp PXMLIB so no /var/lib write.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_ud")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_ud"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_ud.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_ud.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_ud = _load("usb_diagnostics", "usb_diagnostics.py")

T0 = 1_000_000.0
DAY = 86400.0


def _setup(tmp_path):
    lib = tmp_path / "pxmx"
    lib.mkdir()
    _ud.PXMLIB = str(lib)
    _ud.USB_PRESENCE_FILE = f"{lib}/usb_presence.json"
    return _ud


def _dongle(vidpid="0bda:8812", product="AC1200", typ="wireless"):
    return {"vidpid": vidpid, "product": product, "type": typ}


# ── roster ───────────────────────────────────────────────────────────────────

def test_absent_bus_becomes_missing_with_a_stamp(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle(), "3-2": _dongle()}, T0)
    roster = ud.record_presence({"3-1": _dongle()}, T0 + 60)

    missing = ud.missing_dongles(roster, {"3-1": _dongle()}, T0 + 60)
    assert [m["bus_path"] for m in missing] == ["3-2"]
    assert missing[0]["missing_since"] == T0 + 60
    assert missing[0]["missing_for_s"] == 0
    assert missing[0]["vidpid"] == "0bda:8812"


def test_missing_since_does_not_drift_so_the_age_grows(tmp_path):
    """The stamp must be set once. Re-stamping every tick would peg the age at
    ~0 forever and hide exactly the multi-day decay this exists to show."""
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle(), "3-2": _dongle()}, T0)
    ud.record_presence({"3-1": _dongle()}, T0 + 60)
    roster = ud.record_presence({"3-1": _dongle()}, T0 + 3 * DAY)

    missing = ud.missing_dongles(roster, {"3-1": _dongle()}, T0 + 3 * DAY)
    assert missing[0]["missing_since"] == T0 + 60
    assert missing[0]["missing_for_s"] == int(3 * DAY - 60)


def test_reappearing_dongle_clears_the_stamp(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle(), "3-2": _dongle()}, T0)
    ud.record_presence({"3-1": _dongle()}, T0 + 60)
    both = {"3-1": _dongle(), "3-2": _dongle()}
    roster = ud.record_presence(both, T0 + 120)

    assert ud.missing_dongles(roster, both, T0 + 120) == []
    assert roster["3-2"]["missing_since"] is None


def test_roster_persists_across_reload(tmp_path):
    """A reload must not forget the dongle — otherwise every agent restart
    resets the evidence and a slow decay never accumulates."""
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle(), "3-2": _dongle()}, T0)
    reloaded = ud._load_roster()
    assert set(reloaded) == {"3-1", "3-2"}
    assert reloaded["3-2"]["first_seen"] == T0


def test_entry_expires_after_ttl(tmp_path):
    """Hardware genuinely pulled from the host stops being reported missing."""
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle(), "3-2": _dongle()}, T0)
    roster = ud.record_presence({"3-1": _dongle()}, T0 + ud.ROSTER_TTL_S + DAY)
    assert "3-2" not in roster


def test_first_seen_is_preserved_on_every_update(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle()}, T0)
    roster = ud.record_presence({"3-1": _dongle()}, T0 + 5 * DAY)
    assert roster["3-1"]["first_seen"] == T0
    assert roster["3-1"]["last_seen"] == T0 + 5 * DAY


# ── correlation ──────────────────────────────────────────────────────────────

def _missing(bus="3-2"):
    return [{"bus_path": bus, "vidpid": "0bda:8812", "product": "AC1200",
             "missing_since": T0, "missing_for_s": 3600}]


def _kernel(by_bus=None, totals=None, available=True):
    return {"available": available, "window_s": 86400,
            "by_bus": by_bus or {}, "totals": totals or {}, "samples": []}


def _causes(*a, **kw):
    return [c["cause"] for c in _ud.correlate(*a, **kw)]


def test_no_missing_means_no_causes():
    assert _ud.correlate([], _kernel(), [], [], {}) == []


def test_over_current_on_the_missing_bus_is_high_confidence():
    got = _ud.correlate(_missing(), _kernel({"3-2": {"over_current": 4}}),
                        [], [], {})
    assert got[0]["cause"] == "Port over-current"
    assert got[0]["confidence"] == "high"


def test_link_errors_on_the_missing_bus():
    assert "Link / enumeration errors" in _causes(
        _missing(), _kernel({"3-2": {"link_error": 9}}), [], [], {})


def test_insufficient_power():
    assert "Bus power budget exhausted" in _causes(
        _missing(), _kernel({"3-2": {"insufficient_power": 2}}), [], [], {})


def test_controller_fault_is_host_wide_not_per_bus():
    assert "USB controller fault" in _causes(
        _missing(), _kernel(totals={"controller_fault": 3}), [], [],
        {"supported": False})


def test_autosuspend_is_inferred_from_survivors_and_never_high():
    power = [{"bus_path": "3-1", "autosuspend_enabled": True},
             {"bus_path": "3-3", "autosuspend_enabled": False}]
    got = _ud.correlate(_missing(), _kernel(), power, [], {})
    auto = [c for c in got if c["cause"] == "USB autosuspend"]
    assert auto and auto[0]["confidence"] == "medium"


def test_clean_log_reports_silent_disappearance():
    """No kernel evidence is a FINDING (points at power/PHY), not a shrug."""
    got = _ud.correlate(_missing(), _kernel(), [], [], {"supported": True})
    assert got[0]["cause"] == "Silent disappearance (no kernel evidence)"
    assert "uhubctl" in got[0]["remedy"]


def test_unreadable_log_is_not_reported_as_a_clean_log():
    got = _ud.correlate(_missing(), _kernel(available=False), [], [], {})
    assert got[0]["cause"] == "No kernel log available"
    assert got[0]["confidence"] == "unknown"


def test_no_ppps_steers_the_remedy_to_the_controller_path():
    got = _ud.correlate(_missing(), _kernel(), [], [], {"supported": False})
    silent = [c for c in got if c["cause"].startswith("Silent")][0]
    assert "unbinding the controller" in silent["remedy"]


def test_evidence_on_a_different_bus_does_not_explain_this_loss():
    """Errors on a bus that is still present must not be credited as the cause
    of an unrelated dongle's disappearance."""
    got = _causes(_missing("3-2"), _kernel({"3-9": {"link_error": 12}}), [], [], {})
    assert "Link / enumeration errors" not in got
    assert any(c.startswith("Silent") for c in got)


# ── PCI passthrough: absent but EXPECTED ─────────────────────────────────────
# ~4 dongles per host sit behind a USB controller card handed to a VM (T1/T3 PCI
# passthrough). The host correctly stops seeing them; counting those as losses
# would keep the panel permanently red during normal operation.

def test_passed_through_dongles_are_not_counted_missing(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle(), "9-1": _dongle()}, T0)
    # Stamp controllers as record_presence would have while they were present.
    r = ud._load_roster()
    r["3-1"]["pci_controller"] = "0000:97:00.0"      # card later given to a VM
    r["9-1"]["pci_controller"] = "0000:80:14.0"      # stays on the host
    ud._save_roster(r)

    out = ud.missing_dongles(ud._load_roster(), {}, T0 + 60, {"0000:97:00.0"})
    by_bus = {m["bus_path"]: m for m in out}
    assert by_bus["3-1"]["passed_through"] is True
    assert by_bus["9-1"]["passed_through"] is False


def test_unknown_controller_is_never_assumed_passed_through(tmp_path):
    """A roster entry predating controller stamping must read as a real loss,
    not be silently excused."""
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle()}, T0)
    r = ud._load_roster(); r["3-1"].pop("pci_controller", None); ud._save_roster(r)
    out = ud.missing_dongles(ud._load_roster(), {}, T0 + 60, {"0000:97:00.0"})
    assert out[0]["passed_through"] is False


def test_no_passthrough_set_means_everything_is_a_real_loss(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle()}, T0)
    r = ud._load_roster(); r["3-1"]["pci_controller"] = "0000:97:00.0"; ud._save_roster(r)
    assert ud.missing_dongles(ud._load_roster(), {}, T0 + 60, set())[0]["passed_through"] is False


def test_controller_path_is_parsed_from_sysfs_realpath(monkeypatch, tmp_path):
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud.os.path, "realpath",
                        lambda p: "/sys/devices/pci0000:80/0000:80:14.0/usb3/3-1")
    assert ud.usb_device_controller("3-1") == "0000:80:14.0"


def test_controller_path_unresolvable_is_empty(monkeypatch, tmp_path):
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud.os.path, "realpath", lambda p: "/sys/bus/usb/devices/3-1")
    assert ud.usb_device_controller("3-1") == ""
