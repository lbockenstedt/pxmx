"""5-strike permanent-quarantine contract for ``usb_quarantine.quarantine_bus``.

A strike is one quarantine EPISODE: ``quarantine_bus`` no-ops while
``fails >= QUARANTINE_MAX_FAILS`` (repeat triggers within one episode don't
double-count), increments ``strikes`` on a fresh quarantine, and at
``QUARANTINE_PERMANENT_STRIKES`` marks the entry ``permanent``. The recovery
sweep (in usb_provision) resets ``fails=0`` but preserves ``strikes``; we
simulate that here by resetting ``fails`` and re-quarantining.
``clear_quarantine`` pops the whole entry → strike history reset.

Synthetic-package loader (mirrors test_resolve_template_vmid) so the module's
``from . import usb_state_store`` resolves. Tmp PXMLIB so no /var/lib write.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_uq")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_uq"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_uq.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_uq.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_state = _load("usb_state_store", "usb_state_store.py")
_uq = _load("usb_quarantine", "usb_quarantine.py")


def _setup(tmp_path):
    """Point usb_quarantine at a tmp PXMLIB; return the module."""
    lib = tmp_path / "pxmx"
    lib.mkdir()
    _uq.PXMLIB = str(lib)
    _uq.USB_QUARANTINE_FILE = f"{lib}/usb_quarantine.json"
    _uq.DESTROY_FAILS_FILE = f"{lib}/destroy_fails.json"
    _uq.clear_quarantine()  # start clean
    return _uq


def _episode(q, bus, reason="never got IP"):
    """Simulate one quarantine+recovery cycle: quarantine, then reset fails=0
    (what the usb_provision recovery sweep does, preserving strikes)."""
    q.quarantine_bus(bus, reason)
    d = q._read_quarantine()
    e = d.get(bus) or {}
    e["fails"] = 0  # recovery sweep re-eligibles the bus, keeps strikes
    d[bus] = e
    q._save_quarantine(d)


def test_first_quarantine_strikes_one_not_permanent(tmp_path):
    q = _setup(tmp_path)
    q.quarantine_bus("3-1.2", "kernel USB errors (4 in 180s)")
    e = q._read_quarantine()["3-1.2"]
    assert e["fails"] == q.QUARANTINE_MAX_FAILS
    assert e["strikes"] == 1
    assert e["permanent"] is False
    assert e["reason"] == "kernel USB errors (4 in 180s)"
    assert "first_strike" in e and "last_strike" in e


def test_repeat_within_episode_does_not_double_count(tmp_path):
    q = _setup(tmp_path)
    for _ in range(4):
        q.quarantine_bus("3-1.2", "kernel USB errors (4 in 180s)")
    e = q._read_quarantine()["3-1.2"]
    # Still one strike — a single episode, no matter how many dmesg passes fire.
    assert e["strikes"] == 1
    assert e["permanent"] is False


def test_five_episodes_make_permanent(tmp_path):
    q = _setup(tmp_path)
    bus = "3-1.2"
    for _ in range(q.QUARANTINE_PERMANENT_STRIKES - 1):
        _episode(q, bus)
    e = q._read_quarantine()[bus]
    assert e["strikes"] == q.QUARANTINE_PERMANENT_STRIKES - 1
    assert e["permanent"] is False
    # The 5th quarantine tips it to permanent.
    q.quarantine_bus(bus, "never got IP")
    e = q._read_quarantine()[bus]
    assert e["strikes"] == q.QUARANTINE_PERMANENT_STRIKES
    assert e["permanent"] is True
    assert e["fails"] == q.QUARANTINE_MAX_FAILS


def test_permanent_bus_no_ops_on_further_quarantine(tmp_path):
    q = _setup(tmp_path)
    bus = "3-1.2"
    for _ in range(q.QUARANTINE_PERMANENT_STRIKES):
        _episode(q, bus)
    assert q._read_quarantine()[bus]["permanent"] is True
    # A subsequent trigger must not change anything (already as sidelined as
    # possible) — and must NOT raise.
    q.quarantine_bus(bus, "another never-got-IP")
    e = q._read_quarantine()[bus]
    assert e["strikes"] == q.QUARANTINE_PERMANENT_STRIKES
    assert e["permanent"] is True


def test_clear_resets_strike_history(tmp_path):
    q = _setup(tmp_path)
    bus = "3-1.2"
    for _ in range(q.QUARANTINE_PERMANENT_STRIKES):
        _episode(q, bus)
    assert q._read_quarantine()[bus]["permanent"] is True
    # Operator un-QT: clear_usb_quarantine pops the whole entry.
    q.clear_quarantine(bus)
    assert bus not in q._read_quarantine()
    # Next quarantine starts fresh at strike 1 (history was reset).
    q.quarantine_bus(bus, "kernel USB errors (3 in 180s)")
    e = q._read_quarantine()[bus]
    assert e["strikes"] == 1
    assert e["permanent"] is False


def test_clear_all_wipes_everything(tmp_path):
    q = _setup(tmp_path)
    q.quarantine_bus("3-1.2", "a")
    q.quarantine_bus("3-1.3", "b")
    q.clear_quarantine()  # no arg → wipe all
    assert q._read_quarantine() == {}


def test_legacy_entry_without_strikes_migrates_gracefully(tmp_path):
    """An entry written by the OLD code shape ({fails,since,reason}, no strikes)
    must not break the strike counter — the first quarantine after recovery
    starts at strikes=1 (not 0+1 from a missing key blowing up)."""
    q = _setup(tmp_path)
    bus = "3-1.2"
    # Hand-write a legacy entry at the threshold (old-format, no strikes key).
    q._save_quarantine({bus: {"fails": q.QUARANTINE_MAX_FAILS, "since": 1,
                              "reason": "legacy"}})
    # Recovery (sweep resets fails=0; legacy has no strikes → preserved as 0).
    d = q._read_quarantine()
    d[bus]["fails"] = 0
    q._save_quarantine(d)
    # Re-quarantine increments from 0 → 1.
    q.quarantine_bus(bus, "new episode")
    e = q._read_quarantine()[bus]
    assert e["strikes"] == 1
    assert e["permanent"] is False