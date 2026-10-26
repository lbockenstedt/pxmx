"""Reclone of a T1/T3 (PCI passthrough) sim VM: it has no USB bus, so it is
recloned onto the same hostpci controller, which is held while in flight."""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_rpci")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_rpci"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_rpci.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_rpci.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("usb_state_store", "usb_state_store.py")
_load("usb_quarantine", "usb_quarantine.py")
_load("usb_resource_gate", "usb_resource_gate.py")
_prov = _load("usb_provision", "usb_provision.py")

_pve = types.ModuleType("pxmx_agent_src_rpci.pve_cmds")


class PveError(Exception):
    pass


_pve.PveError = PveError
sys.modules["pxmx_agent_src_rpci.pve_cmds"] = _pve
_load("cs_guard", "cs_guard.py")
_cs = _load("cs_sim", "cs_sim.py")

VID = 90003
PCI = "0000:05:00.0"


class _Agent:
    def __init__(self):
        self.config = {"client_simulation": {}}
        self._cs_seen_cmds = {}

    async def send_cs_event(self, kind, data):
        pass


def _setup(monkeypatch, cfg):
    calls = {"set": [], "assign": [], "held_during_destroy": None, "finish": []}

    async def _finish(agent, vid, name, state, **kw):
        calls["finish"].append((vid, name, state, kw))
        return True

    async def _destroy(agent, vid, **kw):
        calls["held_during_destroy"] = dict(_prov._pci_reclone_hold)
        return {"ok": True, "orphaned": False, "bus": None, "kind": "qemu"}

    async def _qm_config(vid):
        return cfg

    async def _qm_set(vid, *args, **kw):
        calls["set"].append(args)

    async def _noop(*a, **kw):
        return True

    async def _resolve(configured):
        return 101

    monkeypatch.setattr(_prov, "_resolve_template_vmid", _resolve)
    monkeypatch.setattr(_prov, "finish_clone", _finish)
    monkeypatch.setattr(_prov, "bus_for_vmid", lambda vid: None)
    monkeypatch.setattr(_prov, "load_usb_state", lambda: {"vmid_to_image": {}})
    monkeypatch.setattr(_prov, "set_assignment", lambda *a: calls["assign"].append(a))
    monkeypatch.setattr(_prov, "remove_orphan_vm", lambda *a: None)
    monkeypatch.setattr(_prov, "mark_recloning", lambda *a: None)
    monkeypatch.setattr(_prov, "_vm_name", lambda vid: "kmcintyre")
    monkeypatch.setattr(_prov, "_hostname_stamp_script", lambda n: "true")
    monkeypatch.setattr(_cs, "_usb_cfg", lambda agent: {"image1_template_id": 100})
    monkeypatch.setattr(_cs, "destroy_vm", _destroy)
    monkeypatch.setattr(_cs.os.path, "isdir", lambda p: False)  # no USB bus present
    monkeypatch.setattr(_pve, "qm_config", _qm_config, raising=False)
    monkeypatch.setattr(_pve, "qm_set", _qm_set, raising=False)
    for fn in ("qm_clone", "qm_start", "qm_agent_ping", "qm_guest_exec_shell"):
        monkeypatch.setattr(_pve, fn, _noop, raising=False)
    return calls


def _core():
    return asyncio.new_event_loop().run_until_complete(
        _cs._reclone_vm_core(_Agent(), VID, None, prot=set(), emit_progress=False))


def test_pci_vm_is_recloned_onto_the_same_controller(monkeypatch):
    calls = _setup(monkeypatch, {"hostpci0": f"{PCI},pcie=1", "name": "kmcintyre"})
    r = _core()
    assert r["ok"] is True and r["bus"] == PCI
    assert ("-hostpci0", f"{PCI},pcie=1") in calls["set"]
    assert not any(a and a[0] == "-usb0" for a in calls["set"])
    assert calls["assign"] == []  # T1/T3 VMs keep no bus assignment
    # ...but get the same post-clone finish as auto-provision (no USB phy).
    assert calls["finish"] == [(VID, "kmcintyre", None,
                                {"bus": None, "image_num": 1, "dtype": ""})]


def test_pci_controller_is_held_during_reclone_then_released(monkeypatch):
    calls = _setup(monkeypatch, {"hostpci0": f"{PCI},pcie=1"})
    _core()
    assert calls["held_during_destroy"] == {"05:00.0": VID}
    assert _prov._pci_reclone_hold == {}


def test_vm_with_neither_usb_nor_pci_still_fails_untouched(monkeypatch):
    calls = _setup(monkeypatch, {"name": "x"})
    r = _core()
    assert r["ok"] is False and "no present USB bus or PCI passthrough" in r["error"]
    assert calls["held_during_destroy"] is None


def test_held_controller_is_not_offered_to_the_provisioner(monkeypatch):
    async def _vmids():
        return []
    monkeypatch.setattr(_pve, "list_all_vmids", _vmids, raising=False)
    _prov.hold_pci_for_reclone(PCI, VID)
    try:
        used = asyncio.new_event_loop().run_until_complete(_prov._pci_addrs_in_use(None))
    finally:
        _prov.release_pci_hold(PCI)
    assert "05:00.0" in used
