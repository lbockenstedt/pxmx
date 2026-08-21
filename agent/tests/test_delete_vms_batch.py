"""Batch teardown contract for ``cs_sim._delete_vms`` — the server-side batch
behind the mass-delete UI.

A multi-VM delete is coalesced (by the cs spoke) into ONE ``delete_vms`` long-op
carrying every vmid for a host. This op must:
  * tear down every REQUESTED sim VM (idempotent ``destroy_vm`` per vmid);
  * emit a per-VM ``CS_PROGRESS`` (``vmid`` + ``result``) so the UI drops each
    row as it goes, then exactly ONE terminal ``CS_COMMAND_RESULT``;
  * treat a per-VM guard failure (protected / below the 90000 sim floor) as
    THAT vm's failure without aborting the batch;
  * let one destroy failure not sink the others (per-VM error isolation);
  * report a non-failed terminal as long as at least one VM succeeded.

Synthetic-package loader (mirrors test_post_prov_reboot) so the module's
``from . import …`` resolves; ``destroy_vm`` is monkeypatched so no pve/usb IO.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_dvb")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_dvb"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_dvb.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_dvb.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


# usb_provision import chain (cs_sim imports usb_provision at module load).
_load("usb_state_store", "usb_state_store.py")
_load("usb_quarantine", "usb_quarantine.py")
_load("usb_resource_gate", "usb_resource_gate.py")
_prov = _load("usb_provision", "usb_provision.py")


# pve_cmds stub — cs_sim does `from .pve_cmds import PveError`; destroy_vm is
# monkeypatched below so no real pve call is ever made.
_pve = types.ModuleType("pxmx_agent_src_dvb.pve_cmds")
class PveError(Exception):
    pass
_pve.PveError = PveError
sys.modules["pxmx_agent_src_dvb.pve_cmds"] = _pve

# Real cs_guard so assert_sim_vm's 90000 floor + protected-set guard runs.
_load("cs_guard", "cs_guard.py")

_cs = _load("cs_sim", "cs_sim.py")

# clear_recovery_state touches usb_state files — noop it (irrelevant here).
_prov.clear_recovery_state = lambda bus: None


class _Agent:
    def __init__(self):
        self.config = {"client_simulation": {}}
        self._cs_seen_cmds = {}
        self.events = []

    async def send_cs_event(self, kind, data):
        self.events.append((kind, dict(data)))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _install_destroy(fail_vmids=()):
    """Monkeypatch cs_sim.destroy_vm with a fake recording each teardown; the
    given vmids come back ok=False (orphaned)."""
    calls = []

    async def _fake(agent, vid, **kw):
        calls.append(vid)
        if vid in fail_vmids:
            return {"ok": False, "orphaned": True, "bus": None,
                    "kind": "qemu", "fails": 3}
        return {"ok": True, "orphaned": False, "bus": None, "kind": "qemu"}

    _cs.destroy_vm = _fake
    return calls


def _progress(agent):
    return [d for k, d in agent.events if k == "CS_PROGRESS"]


def _terminal(agent):
    terms = [d for k, d in agent.events if k == "CS_COMMAND_RESULT"]
    assert len(terms) == 1, f"expected exactly one terminal, got {len(terms)}"
    return terms[0]


def test_all_vms_deleted_emits_per_vm_progress_and_one_terminal():
    calls = _install_destroy()
    ag = _Agent()
    _run(_cs._delete_vms(ag, {"vmids": [90001, 90002, 90003]}, "cid1"))
    assert sorted(calls) == [90001, 90002, 90003]           # every VM torn down
    per_vm = [p for p in _progress(ag) if p.get("vmid") is not None]
    assert {p["vmid"] for p in per_vm} == {90001, 90002, 90003}
    assert all(p["result"] == "completed" for p in per_vm)
    term = _terminal(ag)
    assert term["status"] == "completed"
    assert term["deleted"] == 3 and term["failed"] == 0 and term["total"] == 3
    assert sorted(term["deleted_vmids"]) == [90001, 90002, 90003]


def test_one_destroy_failure_does_not_abort_batch():
    calls = _install_destroy(fail_vmids={90002})
    ag = _Agent()
    _run(_cs._delete_vms(ag, {"vmids": [90001, 90002, 90003]}, "cid2"))
    assert sorted(calls) == [90001, 90002, 90003]           # 90002 didn't block others
    term = _terminal(ag)
    assert term["status"] == "completed"                    # some succeeded
    assert term["deleted"] == 2 and term["failed"] == 1
    assert term["failed_vmids"] == [90002]


def test_guard_failure_counts_as_failed_without_aborting():
    # 100 is below the 90000 sim floor → guard-rejected, never destroyed, but
    # the valid VMs still delete.
    calls = _install_destroy()
    ag = _Agent()
    _run(_cs._delete_vms(ag, {"vmids": [90001, 100, 90002]}, "cid3"))
    assert sorted(calls) == [90001, 90002]                  # 100 never reached destroy
    term = _terminal(ag)
    assert term["deleted"] == 2 and term["failed"] == 1 and term["total"] == 3
    assert 100 in term["failed_vmids"]


def test_all_failed_marks_terminal_failed():
    _install_destroy(fail_vmids={90001, 90002})
    ag = _Agent()
    _run(_cs._delete_vms(ag, {"vmids": [90001, 90002]}, "cid4"))
    term = _terminal(ag)
    assert term["status"] == "failed"
    assert term["deleted"] == 0 and term["failed"] == 2


def test_empty_list_completes_noop():
    calls = _install_destroy()
    ag = _Agent()
    _run(_cs._delete_vms(ag, {"vmids": []}, "cid5"))
    assert calls == []
    term = _terminal(ag)
    assert term["status"] == "completed" and term["total"] == 0


def test_duplicate_vmid_deduped():
    calls = _install_destroy()
    ag = _Agent()
    _run(_cs._delete_vms(ag, {"vmids": [90001, 90001, 90002]}, "cid6"))
    assert sorted(calls) == [90001, 90002]                  # 90001 torn down once
    term = _terminal(ag)
    assert term["total"] == 2 and term["deleted"] == 2


def test_delete_vms_registered_as_batch_long_op():
    assert "delete_vms" in _cs.LONG_ACTIONS
    assert "delete_vms" in _cs._BATCH_LONG_OPS
    assert _cs._HANDLERS.get("delete_vms") is _cs._delete_vms
