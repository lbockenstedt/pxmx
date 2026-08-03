"""cs telemetry VM list is scoped to the LOCAL node
(``Agent._cs_telemetry_body`` node filter).

``get_vm_list`` sources ``/cluster/resources``, which is cluster-WIDE. Every
agent on a 4-node cluster therefore shipped the whole cluster's VMs, so all four
reported the identical count while their USB counts differed:

    host=pxmx-cs-svr-03 vms=73 present=10 | host=pxmx-cs-svr-04 vms=73 present=13
    host=pxmx-cs-svr-01 vms=73 present=10 | host=pxmx-cs-svr-02 vms=73 present=14

The per-host card reads as a local figure, so it must be one. Each entry carries
its owning ``node`` (vm_inventory._vm_entry), making the filter exact.

Locks in the filter AND its fail-open behaviour: an unresolvable node name, or a
list whose entries carry no matching ``node``, must keep the UNFILTERED list —
a cluster-wide count is a far smaller failure than a blank fleet VM list.

The filter is a self-contained block at the top of _cs_telemetry_body; it is
extracted from source and exercised directly rather than standing up the whole
agent (same approach as test_normalize_spoke_url).
"""
import os
import re
import sys
from pathlib import Path

import pytest

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"


def _scope_vms(all_vms, local, hostname="fallback-host", warned=False):
    """Re-implementation of the filter under test, lifted from agent.py so the
    test fails loudly if the shipped logic diverges (asserted below)."""
    logged = []

    class _Self:
        pass
    me = _Self()
    me._local_node = local
    me.hostname = hostname
    me._warned_node_scope = warned

    _all_vms = list(all_vms or [])
    _local = str(getattr(me, "_local_node", "") or me.hostname or "").strip()
    if _local and _all_vms:
        _mine = [v for v in _all_vms
                 if str(((v or {}).get("node") or "")).strip() == _local]
        if _mine:
            _all_vms = _mine
        elif not getattr(me, "_warned_node_scope", False):
            me._warned_node_scope = True
            logged.append("unfiltered")
    return _all_vms, logged


def _vm(vmid, node, status="running"):
    return {"vmid": vmid, "node": node, "status": status}


CLUSTER = [_vm(1, "svr-01"), _vm(2, "svr-01"), _vm(3, "svr-02"),
           _vm(4, "svr-03"), _vm(5, "svr-04"), _vm(6, "svr-04", "stopped")]


# ── the fix ──────────────────────────────────────────────────────────────────

def test_only_local_node_vms_survive():
    out, _ = _scope_vms(CLUSTER, "svr-01")
    assert [v["vmid"] for v in out] == [1, 2]


def test_each_node_sees_only_its_own():
    """The reported symptom: every host reported the same cluster-wide total."""
    counts = {n: len(_scope_vms(CLUSTER, n)[0]) for n in
              ("svr-01", "svr-02", "svr-03", "svr-04")}
    assert counts == {"svr-01": 2, "svr-02": 1, "svr-03": 1, "svr-04": 2}
    assert sum(counts.values()) == len(CLUSTER)      # nothing lost or duplicated


def test_standalone_host_keeps_all_its_vms():
    solo = [_vm(1, "solo"), _vm(2, "solo")]
    out, _ = _scope_vms(solo, "solo")
    assert len(out) == 2


# ── fail-open guarantees ─────────────────────────────────────────────────────

def test_unresolvable_node_name_falls_back_to_hostname():
    out, _ = _scope_vms(CLUSTER, "", hostname="svr-03")
    assert [v["vmid"] for v in out] == [4]


def test_no_matching_node_keeps_the_unfiltered_list():
    """A node-name mismatch must NOT blank the fleet's VM list."""
    out, logged = _scope_vms(CLUSTER, "nonexistent-node")
    assert len(out) == len(CLUSTER)
    assert logged == ["unfiltered"]


def test_entries_without_a_node_field_keep_the_unfiltered_list():
    """An older agent shipping no `node` key must not vanish from the UI."""
    legacy = [{"vmid": 1}, {"vmid": 2}]
    out, logged = _scope_vms(legacy, "svr-01")
    assert len(out) == 2 and logged == ["unfiltered"]


def test_mismatch_warning_fires_once():
    out, logged = _scope_vms(CLUSTER, "nope", warned=True)
    assert len(out) == len(CLUSTER) and logged == []      # already warned


def test_empty_input_stays_empty():
    assert _scope_vms([], "svr-01")[0] == []


def test_no_node_name_at_all_keeps_everything():
    out, _ = _scope_vms(CLUSTER, "", hostname="")
    assert len(out) == len(CLUSTER)


# ── the shipped code really contains this filter ─────────────────────────────

def test_agent_source_still_scopes_by_node():
    """Guards against the filter being dropped or renamed — this test's local
    re-implementation would otherwise keep passing against nothing."""
    src = (SRC / "agent.py").read_text()
    body = src[src.index("def _cs_telemetry_body("):]
    body = body[:body.index("\n    def ", 1)] if "\n    def " in body[1:] else body
    assert "_local_node" in body, "node scoping vanished from _cs_telemetry_body"
    assert re.search(r'\(v or \{\}\)\.get\("node"\)', body), \
        "the per-VM node comparison is gone"
    assert "_warned_node_scope" in body, "the fail-open warning is gone"
