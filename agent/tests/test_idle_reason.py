"""Auto-provision idle wording (``usb_provision.build_idle_reason``).

The fleet is meant to run EVERY dongle, so "nothing left to place a VM on"
is normally the GOAL state, not a shortage. Reporting it as "no eligible
dongles (assigned=[...])" made a fully-deployed host read as broken on the
Auto-Provisioning card.

Locks in: deployed-count wording whenever anything is in use, the fault wording
reserved for genuinely-nothing-to-work-with, and sidelined counts always
surviving so "8 in use; 2 quarantined" never reads the same as a clean 8.

The string is load-bearing — ``fleet_health_alert._eval_dongle_shed`` keys on
its prefix — so the prefixes are asserted explicitly.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"


def _load_build_idle_reason():
    """Exec just the helper out of usb_provision (importing the whole module
    pulls in the agent's package graph). Mirrors the source-extraction harness
    used by test_normalize_spoke_url."""
    src = (SRC / "usb_provision.py").read_text()
    marker = "def build_idle_reason("
    i = src.index(marker)
    j = src.index("\ndef ", i + 1)
    ns = {"Dict": dict, "List": list}
    exec(compile(src[i:j], "usb_provision.py", "exec"), ns)
    return ns["build_idle_reason"]


build_idle_reason = _load_build_idle_reason()


def _culled(assigned=(), quarantined=(), excluded=(), type_=()):
    return {"assigned": list(assigned), "quarantined": list(quarantined),
            "excluded": list(excluded), "type": list(type_)}


# ── the goal state ───────────────────────────────────────────────────────────

def test_everything_deployed_reads_as_success_with_a_count():
    r = build_idle_reason(_culled(assigned=[f"3-{i}" for i in range(11)]), True, "wireless")
    assert r == "all dongles deployed (11 in use)"
    assert "no eligible" not in r


def test_deployed_wording_does_not_leak_the_bus_list():
    """The old message pasted 11 bus ids into the card; the count is the signal."""
    r = build_idle_reason(_culled(assigned=["17-3", "3-4", "17-2.3"]), True, "wireless")
    assert "17-2.3" not in r and "3 in use" in r


# ── deployed, but with dongles sidelined ─────────────────────────────────────

def test_quarantined_count_rides_along():
    r = build_idle_reason(_culled(assigned=["3-1"] * 8,
                                  quarantined=["16-1(3/P)", "16-4(3/P)"]), True, "wireless")
    assert r == "all dongles deployed (8 in use; 2 quarantined)"


def test_all_three_sidelined_kinds_are_reported():
    r = build_idle_reason(_culled(assigned=["3-1"] * 4, quarantined=["a"],
                                  excluded=["b", "c"], type_=["d:wired"]),
                          True, "wireless")
    assert r.startswith("all dongles deployed (4 in use;")
    assert "1 quarantined" in r and "2 excluded" in r
    assert "1 wrong type for sim_phy=wireless" in r


def test_a_clean_eight_and_an_eight_with_quarantine_differ():
    clean = build_idle_reason(_culled(assigned=["x"] * 8), True, "wireless")
    dirty = build_idle_reason(_culled(assigned=["x"] * 8, quarantined=["q"]), True, "wireless")
    assert clean != dirty


# ── genuine faults keep the diagnosable wording ──────────────────────────────

def test_no_dongles_present_is_still_a_fault():
    r = build_idle_reason(_culled(), False, "wireless")
    assert r == "no eligible dongles (none present)"


def test_all_quarantined_none_deployed_is_still_a_fault():
    """Nothing in use and everything sidelined is a real problem — it must not
    be dressed up as 'all dongles deployed (0 in use)'."""
    r = build_idle_reason(_culled(quarantined=["16-1", "16-4"]), True, "wireless")
    assert r.startswith("no eligible dongles (")
    assert "quarantined=" in r


def test_all_wrong_type_none_deployed_is_still_a_fault():
    r = build_idle_reason(_culled(type_=["3-1:wired"]), True, "ethernet")
    assert r.startswith("no eligible dongles (")
    assert "type=" in r


def test_present_but_uncategorised_falls_back_to_sim_phy():
    r = build_idle_reason(_culled(), True, "ethernet")
    assert r == "no eligible dongles (none match sim_phy=ethernet)"


# ── the alert contract ───────────────────────────────────────────────────────

def test_both_prefixes_are_the_only_two_shapes():
    """fleet_health_alert._eval_dongle_shed matches on these two prefixes; if a
    third shape is ever added, that predicate must be updated too."""
    cases = [
        build_idle_reason(_culled(assigned=["a"]), True, "wireless"),
        build_idle_reason(_culled(assigned=["a"], quarantined=["b"]), True, "wireless"),
        build_idle_reason(_culled(), False, "wireless"),
        build_idle_reason(_culled(quarantined=["b"]), True, "wireless"),
        build_idle_reason(_culled(), True, "wireless"),
    ]
    for r in cases:
        assert r.startswith("all dongles deployed") or r.startswith("no eligible dongles"), r
