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
    _ud.USB_BOOT_FILE = f"{lib}/usb_boot_baseline.json"
    _ud.USB_KERNEL_FILE = f"{lib}/usb_kernel_events.json"
    # usb_device_controller memoizes positive lookups in a process-wide dict.
    # The module object is shared by every test here, so without this a test
    # that resolves bus "3-1" pins that answer for every later test using the
    # same bus — which made the unresolvable-path test pass alone and fail in
    # the full suite. Give each test a clean cache alongside its clean tmp dir.
    _ud._CTRL_CACHE.clear()
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


# ── disconnect-only evidence must produce a cause ────────────────────────────
# Regression: `disconnect` counts landed in the per-bus hit map but no branch
# read them, AND their presence suppressed the "no kernel evidence" fallback —
# so a host whose dongles only logged disconnects got an EMPTY cause list.
# Three of four lab hosts (23-140 disconnects) showed no probable cause at all.

def test_disconnect_only_produces_a_cause():
    got = _ud.correlate(_missing(), _kernel({"3-2": {"disconnect": 4}}),
                        [], [], {"supported": True})
    assert got, "disconnect-only evidence produced NO cause"
    assert got[0]["cause"] == "Clean disconnect (no link error)"


def test_disconnect_cause_points_at_power_not_driver():
    c = _ud.correlate(_missing(), _kernel({"3-2": {"disconnect": 9}}),
                      [], [], {"supported": True})[0]
    assert "power/PHY" in c["detail"]
    assert "uhubctl" in c["remedy"]


def test_disconnect_remedy_adapts_when_no_ppps_hub():
    c = _ud.correlate(_missing(), _kernel({"3-2": {"disconnect": 9}}),
                      [], [], {"supported": False})[0]
    assert "unbind" in c["remedy"] and "uhubctl -a cycle" not in c["remedy"]


def test_link_error_outranks_a_bare_disconnect():
    """A link error is the more specific diagnosis; disconnect must not
    duplicate it (svr-01 had both and should report the link error)."""
    causes = _causes(_missing(), _kernel({"3-2": {"link_error": 6, "disconnect": 3}}),
                     [], [], {})
    assert "Link / enumeration errors" in causes
    assert "Clean disconnect (no link error)" not in causes


def test_disconnect_on_another_bus_is_not_credited():
    causes = _causes(_missing("3-2"), _kernel({"9-9": {"disconnect": 50}}),
                     [], [], {"supported": True})
    assert "Clean disconnect (no link error)" not in causes
    assert any(c.startswith("Silent") for c in causes)


# ── boot baseline: the authoritative "what should be here" ───────────────────
# The roster spans reboots, so it mixes real losses with bus RENUMBERING and
# with controllers handed to VMs. Bus ids are stable WITHIN a boot, so a diff
# against a boot snapshot is unambiguous. Crucially the snapshot is taken at
# boot — BEFORE the VMs claim their PCI controllers — so the ~4 per host bound
# for passthrough are in it and must not later read as losses.

def _baseline(buses, boot="boot-a", trusted=True):
    return {"boot_id": boot, "captured_at": T0, "trusted": trusted,
            "count": len(buses), "buses": buses}


def test_lost_since_boot_is_the_simple_difference(tmp_path):
    ud = _setup(tmp_path)
    b = _baseline({"3-1": {"vidpid": "x", "pci_controller": "0000:80:14.0"},
                   "9-1": {"vidpid": "y", "pci_controller": "0000:80:14.0"}})
    out = ud.lost_since_boot(b, {"3-1": _dongle()}, set(), {})
    assert [m["bus_path"] for m in out] == ["9-1"]
    assert out[0]["passed_through"] is False


def test_passthrough_dongles_present_at_boot_are_not_losses(tmp_path):
    """The reported case: the baseline is captured BEFORE the VM takes the
    controller, so those dongles are in it and vanish later by design."""
    ud = _setup(tmp_path)
    b = _baseline({"3-1": {"vidpid": "x", "pci_controller": "0000:97:00.0"},
                   "3-2": {"vidpid": "x", "pci_controller": "0000:97:00.0"},
                   "9-1": {"vidpid": "y", "pci_controller": "0000:80:14.0"}})
    out = ud.lost_since_boot(b, {}, {"0000:97:00.0"}, {})
    by = {m["bus_path"]: m for m in out}
    assert by["3-1"]["passed_through"] is True
    assert by["3-2"]["passed_through"] is True
    assert by["9-1"]["passed_through"] is False      # the only real loss


def test_baseline_controller_beats_a_missing_roster(tmp_path):
    """Classification must survive a roster reset — the baseline holds its own
    controller record precisely so it does not depend on the roster."""
    ud = _setup(tmp_path)
    b = _baseline({"3-1": {"vidpid": "x", "pci_controller": "0000:97:00.0"}})
    assert ud.lost_since_boot(b, {}, {"0000:97:00.0"}, {})[0]["passed_through"] is True


def test_roster_controller_used_when_baseline_predates_stamping(tmp_path):
    ud = _setup(tmp_path)
    b = _baseline({"3-1": {"vidpid": "x"}})          # no pci_controller
    ros = {"3-1": {"pci_controller": "0000:97:00.0"}}
    assert ud.lost_since_boot(b, {}, {"0000:97:00.0"}, ros)[0]["passed_through"] is True


def test_baseline_recaptured_only_on_reboot(tmp_path, monkeypatch):
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "boot_id", lambda: "boot-a")
    monkeypatch.setattr(ud, "_uptime_s", lambda: 60.0)
    first = ud.capture_boot_baseline({"3-1": _dongle()}, T0)
    assert first["count"] == 1 and first["trusted"] is True
    # Same boot, more dongles now — baseline must NOT move.
    again = ud.capture_boot_baseline({"3-1": _dongle(), "9-1": _dongle()}, T0 + 500)
    assert again["count"] == 1
    # Reboot → fresh capture.
    monkeypatch.setattr(ud, "boot_id", lambda: "boot-b")
    after = ud.capture_boot_baseline({"3-1": _dongle(), "9-1": _dongle()}, T0 + 900)
    assert after["count"] == 2 and after["boot_id"] == "boot-b"


def test_late_capture_is_marked_untrusted(tmp_path, monkeypatch):
    """Agent installed/restarted hours into a boot: a mid-life sample, not a
    boot snapshot, and must not be presented as one."""
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "boot_id", lambda: "boot-c")
    monkeypatch.setattr(ud, "_uptime_s", lambda: 40000.0)
    assert ud.capture_boot_baseline({"3-1": _dongle()}, T0)["trusted"] is False


# ── inventory qualifier: a briefly-attached dongle is not "missing" ──────────
# A dongle can legitimately be plugged in and removed. Without a dwell test any
# stick present for ten minutes became a permanent missing row the moment it was
# pulled, burying the real losses.

def test_dongle_seen_briefly_is_not_established(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle()}, T0)
    roster = ud.record_presence({"3-1": _dongle()}, T0 + 600)     # 10 min
    ud.record_presence({}, T0 + 700)
    out = ud.missing_dongles(ud._load_roster(), {}, T0 + 800)
    assert out[0]["established"] is False
    assert out[0]["observed_s"] == 600


def test_dongle_present_over_four_hours_is_inventory(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle()}, T0)
    ud.record_presence({"3-1": _dongle()}, T0 + ud.INVENTORY_MIN_AGE_S + 60)
    out = ud.missing_dongles(ud._load_roster(), {}, T0 + ud.INVENTORY_MIN_AGE_S + 600)
    assert out[0]["established"] is True


def test_exactly_the_threshold_counts(tmp_path):
    ud = _setup(tmp_path)
    ud.record_presence({"3-1": _dongle()}, T0)
    ud.record_presence({"3-1": _dongle()}, T0 + ud.INVENTORY_MIN_AGE_S)
    assert ud.missing_dongles(ud._load_roster(), {}, T0 + 99999)[0]["established"] is True


# ── purge ────────────────────────────────────────────────────────────────────

def test_purge_removes_roster_and_baseline(tmp_path, monkeypatch):
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "boot_id", lambda: "boot-a")
    monkeypatch.setattr(ud, "_uptime_s", lambda: 30.0)
    ud.record_presence({"3-1": _dongle()}, T0)
    ud.capture_boot_baseline({"3-1": _dongle()}, T0)
    assert ud._load_roster() and ud._load_boot_baseline()
    out = ud.purge_history()
    assert sorted(out["purged"]) == ["usb_boot_baseline.json", "usb_presence.json"]
    assert ud._load_roster() == {} and ud._load_boot_baseline() == {}


def test_purge_is_idempotent(tmp_path):
    ud = _setup(tmp_path)
    ud.purge_history()
    assert ud.purge_history()["purged"] == []


# ── bus renumbering must not invent dongles ──────────────────────────────────
# Field reference: a host with 14 T2 + 4 T1 = 18 physical dongles and NO swaps
# reported "23 ever seen" after two reboots. Kernel bus NUMBERS are assigned in
# controller enumeration order and shift between boots; the port chain does not.
# Keying history on the raw bus path invented a new dongle each time.

def _p(vidpid="2357:012d"):
    return {"vidpid": vidpid, "product": "802.11ac nic", "type": "wireless"}


def test_renumbered_bus_does_not_inflate_the_roster(tmp_path, monkeypatch):
    ud = _setup(tmp_path)
    # Boot 1: one dongle on controller X port 1, seen as bus 3.
    monkeypatch.setattr(ud, "usb_device_controller", lambda b: "0000:80:14.0")
    ud.record_presence({"3-1": _p()}, T0)
    # Boot 2: same physical port, kernel now calls it bus 5.
    roster = ud.record_presence({"5-1": _p()}, T0 + 86400)
    assert len(roster) == 1, f"roster inflated to {len(roster)}: {sorted(roster)}"
    assert "5-1" in roster and "3-1" not in roster


def test_renumbering_preserves_first_seen_so_dwell_survives(tmp_path, monkeypatch):
    """The merged entry must keep the ORIGINAL first_seen, or a long-established
    dongle would look brand new after every reboot and drop out of inventory."""
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "usb_device_controller", lambda b: "0000:80:14.0")
    ud.record_presence({"3-1": _p()}, T0)
    roster = ud.record_presence({"5-1": _p()}, T0 + 86400)
    assert roster["5-1"]["first_seen"] == T0
    assert ud._is_established(roster["5-1"]) is True


def test_eighteen_dongles_stay_eighteen_across_a_reboot(tmp_path, monkeypatch):
    """The reported fleet shape: 18 physical dongles, renumbered, no swaps."""
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "usb_device_controller", lambda b: "0000:80:14.0")
    boot1 = {f"{3 + i}-1": _p() for i in range(18)}
    ud.record_presence(boot1, T0)
    boot2 = {f"{21 + i}-1": _p() for i in range(18)}      # every bus renumbered
    roster = ud.record_presence(boot2, T0 + 86400)
    assert len(roster) == 18, f"expected 18, got {len(roster)}"
    assert ud.missing_dongles(roster, boot2, T0 + 86400) == []


def test_different_port_is_a_different_dongle(tmp_path, monkeypatch):
    """A genuinely new port must still register as new — the merge keys on the
    PORT chain, not merely on the controller."""
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "usb_device_controller", lambda b: "0000:80:14.0")
    ud.record_presence({"3-1": _p()}, T0)
    roster = ud.record_presence({"3-2": _p()}, T0 + 60)
    assert len(roster) == 2


def test_same_port_on_a_different_controller_is_distinct(tmp_path, monkeypatch):
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "usb_device_controller",
                        lambda b: "0000:80:14.0" if b.startswith("3") else "0000:97:00.0")
    ud.record_presence({"3-1": _p()}, T0)
    roster = ud.record_presence({"9-1": _p()}, T0 + 60)
    assert len(roster) == 2


def test_no_controller_means_no_merge(tmp_path, monkeypatch):
    """Without a controller there is no stable identity — never guess."""
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "usb_device_controller", lambda b: "")
    ud.record_presence({"3-1": _p()}, T0)
    roster = ud.record_presence({"5-1": _p()}, T0 + 60)
    assert len(roster) == 2


def test_a_still_present_old_bus_is_never_absorbed(tmp_path, monkeypatch):
    """Two dongles genuinely on the same port chain of the same controller
    cannot both be present; if the old path IS still present it is a real second
    device and must not be merged away."""
    ud = _setup(tmp_path)
    monkeypatch.setattr(ud, "usb_device_controller", lambda b: "0000:80:14.0")
    ud.record_presence({"3-1": _p()}, T0)
    roster = ud.record_presence({"3-1": _p(), "5-1": _p()}, T0 + 60)
    assert len(roster) == 2
