"""Post-clone settle reboot contract for the ``post_prov_reboot`` queue.

``usb_state_store.set_assignment`` (the shared choke point both clone paths —
first-clone ``usb_provision._clone_and_provision`` and reclone
``cs_sim._reclone_vm_core`` — hit) stamps ``post_prov_reboot[vmid] =
{cloned_at, reboot_at(=cloned_at+900), bus, image_num}``. The provision-loop
sweep ``usb_provision._run_post_prov_reboot_queue`` fires a graceful QGA
reboot once ``now >= reboot_at``, then pops the entry; it drops stale
(bus-mismatch) and gone (qm_config falsy) entries without rebooting.

Synthetic-package loader (mirrors test_usb_quarantine_strikes) so the
modules' ``from . import …`` resolves. Tmp PXMLIB so no /var/lib write.
``pve_cmds`` and ``cs_guard`` are stubbed in the synthetic package.
"""
import asyncio
import importlib.util
import os
import sys
import time
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_ppr")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_ppr"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_ppr.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_ppr.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_state = _load("usb_state_store", "usb_state_store.py")
_load("usb_quarantine", "usb_quarantine.py")  # usb_provision imports it
_load("usb_resource_gate", "usb_resource_gate.py")
_prov = _load("usb_provision", "usb_provision.py")


# ── stubs for the sweep's local imports ────────────────────────────────────
class _FakePveCmds:
    """Records calls; configurable qm_config / qm_guest_exec / vm_action_any
    behaviour per test."""

    def __init__(self, *, config_ok=True, guest_exec_raises=False,
                 guest_exec_rc=True):
        self.calls = []
        self._config_ok = config_ok
        self._guest_exec_raises = guest_exec_raises
        self._guest_exec_rc = guest_exec_rc

    async def qm_config(self, vmid):
        self.calls.append(("qm_config", vmid))
        return {"template": "0"} if self._config_ok else {}

    async def qm_guest_exec(self, vmid, *cmd, protected=None):
        self.calls.append(("qm_guest_exec", vmid, cmd, protected))
        if self._guest_exec_raises:
            raise RuntimeError("guest channel died")
        return self._guest_exec_rc

    async def vm_action_any(self, vmid, action, kind=None, snapshot_name=None,
                            backup_opts=None):
        self.calls.append(("vm_action_any", vmid, action))
        return {"vmid": vmid, "action": action, "started": True}


_pve = _FakePveCmds()
sys.modules["pxmx_agent_src_ppr.pve_cmds"] = _pve

# cs_guard.resolve_protected_vmids — _protected_vmids(agent) calls it.
_cg = types.ModuleType("pxmx_agent_src_ppr.cs_guard")
_cg.resolve_protected_vmids = lambda client_sim: set()
sys.modules["pxmx_agent_src_ppr.cs_guard"] = _cg


class _Agent:
    def __init__(self):
        self.config = {"client_simulation": {}}


def _setup_state(tmp_path, entries=None):
    """A fresh in-memory usb_state dict with the given post_prov_reboot
    entries plus a matching vmid_to_bus so the bus-mismatch check passes."""
    lib = tmp_path / "pxmx"
    lib.mkdir(exist_ok=True)
    _state.PXMLIB = str(lib)
    _state.USB_STATE_FILE = f"{lib}/usb_state.json"
    _state.ORPHAN_VMS_FILE = f"{lib}/orphan_vms.json"
    entries = entries or {}
    st = {"vmid_to_bus": {}, "bus_to_vmid": {}, "vmid_to_image": {},
          "excluded_buses": {}, "quarantined": {}, "missing_since": {},
          "vidpid_by_bus": {}, "post_prov_retry": {},
          "post_prov_reboot": dict(entries)}
    for v, e in entries.items():
        st["vmid_to_bus"][v] = e["bus"]
        st["bus_to_vmid"][e["bus"]] = v
    return st


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _entry(bus="3-1.2", cloned_at=None, reboot_at=None, image_num=1):
    cloned_at = time.time() if cloned_at is None else cloned_at
    reboot_at = cloned_at + 900 if reboot_at is None else reboot_at
    return {"cloned_at": cloned_at, "reboot_at": reboot_at,
            "bus": bus, "image_num": image_num}


def test_not_due_keeps_entry_no_reboot(tmp_path):
    _pve.calls.clear()
    st = _setup_state(tmp_path, {"91001": _entry(reboot_at=time.time() + 600)})
    mutated = _run(_prov._run_post_prov_reboot_queue(_Agent(), st))
    assert mutated is False
    assert "91001" in st["post_prov_reboot"]
    assert _pve.calls == []  # nothing fired


def test_due_fires_qga_reboot_and_pops(tmp_path):
    _pve.calls.clear()
    st = _setup_state(tmp_path, {"91002": _entry(reboot_at=time.time() - 1)})
    mutated = _run(_prov._run_post_prov_reboot_queue(_Agent(), st))
    assert mutated is True
    assert "91002" not in st["post_prov_reboot"]
    kinds = [c[0] for c in _pve.calls]
    assert "qm_guest_exec" in kinds
    assert "vm_action_any" not in kinds  # QGA succeeded → no reset fallback


def test_qga_reboot_raises_falls_back_to_reset(tmp_path):
    _pve._guest_exec_raises = True
    _pve._guest_exec_rc = True
    _pve.calls.clear()
    try:
        st = _setup_state(tmp_path, {"91003": _entry(reboot_at=time.time() - 1)})
        mutated = _run(_prov._run_post_prov_reboot_queue(_Agent(), st))
        assert mutated is True
        assert "91003" not in st["post_prov_reboot"]
        kinds = [c[0] for c in _pve.calls]
        assert "qm_guest_exec" in kinds
        assert "vm_action_any" in kinds  # fallback fired
    finally:
        _pve._guest_exec_raises = False


def test_bus_mismatch_drops_without_reboot(tmp_path):
    _pve.calls.clear()
    e = _entry(bus="3-1.2", reboot_at=time.time() - 1)
    st = _setup_state(tmp_path, {"91004": e})
    # Re-point the VM to a different bus — entry is now stale.
    st["vmid_to_bus"]["91004"] = "9-1.4"
    _run(_prov._run_post_prov_reboot_queue(_Agent(), st))
    assert "91004" not in st["post_prov_reboot"]
    assert _pve.calls == []  # dropped, never touched pve_cmds


def test_vm_gone_drops_without_reboot(tmp_path):
    _pve._config_ok = False
    _pve.calls.clear()
    try:
        st = _setup_state(tmp_path, {"91005": _entry(reboot_at=time.time() - 1)})
        _run(_prov._run_post_prov_reboot_queue(_Agent(), st))
        assert "91005" not in st["post_prov_reboot"]
        kinds = [c[0] for c in _pve.calls]
        # qm_config was called (the gone check) but no reboot fired.
        assert "qm_config" in kinds
        assert "qm_guest_exec" not in kinds
        assert "vm_action_any" not in kinds
    finally:
        _pve._config_ok = True


def test_set_assignment_stamps_and_clear_removes(tmp_path):
    lib = tmp_path / "pxmx"
    lib.mkdir(exist_ok=True)
    _state.PXMLIB = str(lib)
    _state.USB_STATE_FILE = f"{lib}/usb_state.json"
    _state.ORPHAN_VMS_FILE = f"{lib}/orphan_vms.json"
    _state.save_usb_state(_state._new_usb_state())  # seed empty file

    _state.set_assignment(91010, "3-1.7", 2)
    st = _state.load_usb_state()
    e = st["post_prov_reboot"]["91010"]
    assert e["bus"] == "3-1.7"
    assert e["image_num"] == 2
    assert abs(e["reboot_at"] - e["cloned_at"] - 900) <= 2  # +15 min (default)

    _state.clear_assignment(91010)
    st = _state.load_usb_state()
    assert "91010" not in st.get("post_prov_reboot", {})
    # vmid maps also cleared (existing contract).
    assert "91010" not in st["vmid_to_bus"]


def test_empty_queue_noop(tmp_path):
    _pve.calls.clear()
    st = _setup_state(tmp_path)  # no entries
    mutated = _run(_prov._run_post_prov_reboot_queue(_Agent(), st))
    assert mutated is False
    assert _pve.calls == []