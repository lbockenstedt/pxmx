"""Reclone must resolve the clone source BEFORE destroying the VM.

Regression: a fleet "Reclone All" on a host whose configured template (vmid
100) did not exist destroyed every VM, then ``qm clone 100`` failed — the VMs
were gone and the UI only said "failed". The provision loop already resolved a
missing template to a cluster fallback (101) via ``_resolve_template_vmid``;
``_reclone_vm_core`` now uses the same resolver, refuses to destroy when nothing
resolves, and carries the real qm stderr / destroy reason in its error.

Synthetic-package loader mirrors test_delete_vms_batch.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_rtr")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_rtr"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_rtr.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_rtr.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("usb_state_store", "usb_state_store.py")
_load("usb_quarantine", "usb_quarantine.py")
_load("usb_resource_gate", "usb_resource_gate.py")
_prov = _load("usb_provision", "usb_provision.py")

_pve = types.ModuleType("pxmx_agent_src_rtr.pve_cmds")


class PveError(Exception):
    pass


_pve.PveError = PveError
sys.modules["pxmx_agent_src_rtr.pve_cmds"] = _pve
_load("cs_guard", "cs_guard.py")
_cs = _load("cs_sim", "cs_sim.py")

VID = 90010
BUS = "15-2.4"


class _Agent:
    def __init__(self):
        self.config = {"client_simulation": {"usb": {"image1_template_id": 100}}}
        self._cs_seen_cmds = {}

    async def send_cs_event(self, kind, data):
        pass


def _setup(monkeypatch, *, resolved, clone_exc=None):
    calls = {"destroy": 0, "clone": []}

    async def _resolve(configured):
        return resolved

    async def _destroy(agent, vid, **kw):
        calls["destroy"] += 1
        return {"ok": True, "orphaned": False, "bus": BUS, "kind": "qemu"}

    async def _clone(template, vid, name, **kw):
        calls["clone"].append(int(template))
        if clone_exc:
            raise clone_exc

    async def _noop(*a, **kw):
        return True

    monkeypatch.setattr(_prov, "_resolve_template_vmid", _resolve)
    monkeypatch.setattr(_prov, "bus_for_vmid", lambda vid: BUS)
    monkeypatch.setattr(_prov, "load_usb_state", lambda: {"vmid_to_image": {}})
    monkeypatch.setattr(_prov, "set_assignment", lambda *a: None)
    monkeypatch.setattr(_prov, "remove_orphan_vm", lambda *a: None)
    monkeypatch.setattr(_prov, "mark_recloning", lambda *a: None)
    monkeypatch.setattr(_prov, "_vm_name", lambda vid: "kuser")
    monkeypatch.setattr(_prov, "_hostname_stamp_script", lambda n: "true")
    monkeypatch.setattr(_cs, "_usb_cfg", lambda agent: {"image1_template_id": 100})
    monkeypatch.setattr(_cs, "destroy_vm", _destroy)
    monkeypatch.setattr(_cs.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(_pve, "qm_clone", _clone, raising=False)
    for fn in ("qm_set", "qm_start", "qm_agent_ping", "qm_guest_exec_shell"):
        monkeypatch.setattr(_pve, fn, _noop, raising=False)
    return calls


def _core():
    return asyncio.new_event_loop().run_until_complete(
        _cs._reclone_vm_core(_Agent(), VID, None, prot=set(), emit_progress=False))


def test_missing_template_falls_back_to_resolved_vmid(monkeypatch):
    calls = _setup(monkeypatch, resolved=101)
    r = _core()
    assert r["ok"] is True
    assert calls["clone"] == [101]


def test_unresolvable_template_never_destroys(monkeypatch):
    calls = _setup(monkeypatch, resolved=None)
    r = _core()
    assert r["ok"] is False
    assert calls["destroy"] == 0
    assert "not found" in r["error"] and "100" in r["error"]


def test_clone_failure_carries_qm_stderr(monkeypatch):
    _setup(monkeypatch, resolved=101,
           clone_exc=PveError("unable to find configuration file for VM 101"))
    r = _core()
    assert r["ok"] is False
    assert "cloning from template 101 failed" in r["error"]
    assert "unable to find configuration file" in r["error"]


def test_destroy_failure_carries_reason(monkeypatch):
    _setup(monkeypatch, resolved=101)

    async def _destroy_fail(agent, vid, **kw):
        return {"ok": False, "orphaned": False, "bus": BUS, "kind": "qemu",
                "fails": 1, "reason": "VM is locked (backup)"}

    monkeypatch.setattr(_cs, "destroy_vm", _destroy_fail)
    r = _core()
    assert r["ok"] is False
    assert "VM is locked (backup)" in r["error"]
