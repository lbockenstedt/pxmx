"""The shared post-clone finish (``usb_provision.finish_clone``) that USB
auto-provision, T1/T3 PCI auto-provision and reclone all run: settle-reboot
queued, hostname stamped, sim_phy for USB only, reboot, update.sh, and a
post-prov retry entry when the guest doesn't come back."""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_fc")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_fc"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_fc.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_fc.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("usb_state_store", "usb_state_store.py")
_load("usb_quarantine", "usb_quarantine.py")
_load("usb_resource_gate", "usb_resource_gate.py")
_prov = _load("usb_provision", "usb_provision.py")

_cg = types.ModuleType("pxmx_agent_src_fc.cs_guard")
_cg.resolve_protected_vmids = lambda client_sim: set()
sys.modules["pxmx_agent_src_fc.cs_guard"] = _cg


class _Pve:
    def __init__(self, comes_back=True):
        self.shell = []
        self.exec = []
        self.comes_back = comes_back
        self.rebooted = False

    async def qm_agent_ping(self, vmid, **kw):
        return not self.rebooted or self.comes_back

    async def qm_guest_exec_shell(self, vmid, script, **kw):
        self.shell.append(script)
        return True

    async def qm_guest_exec(self, vmid, *cmd, **kw):
        self.exec.append(cmd)
        self.rebooted = True

    async def qm_config(self, vmid):
        return {"name": "x"}

    async def vm_action_any(self, vmid, action, **kw):
        self.exec.append(("vm_action_any", action))


class _Clock:
    """time.time() that jumps 60s per call so the 300s comeback wait ends fast."""
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        self.t += 60
        return self.t


class _Agent:
    config = {"client_simulation": {}}


def _run(monkeypatch, pve, **kw):
    async def _sleep(_s):
        return None
    monkeypatch.setitem(sys.modules, "pxmx_agent_src_fc.pve_cmds", pve)
    monkeypatch.setattr(_prov.asyncio, "sleep", _sleep)
    monkeypatch.setattr(_prov, "time", _Clock())
    monkeypatch.setattr(_prov, "remove_orphan_vm", lambda v: None)
    monkeypatch.setattr(_prov, "_hostname_stamp_script", lambda n: f"stamp {n}")
    state = {}
    came_back = asyncio.new_event_loop().run_until_complete(
        _prov.finish_clone(_Agent(), 90003, "kmcintyre", state, **kw))
    return state, came_back


def test_pci_clone_gets_the_full_usb_style_finish(monkeypatch):
    pve = _Pve()
    state, came_back = _run(monkeypatch, pve)
    assert came_back is True
    assert pve.shell[0] == "stamp kmcintyre"
    assert not any("sim_phy" in s for s in pve.shell)       # no USB dongle
    assert ("reboot",) in pve.exec
    assert pve.shell[-1] == "bash /usr/local/scripts/update.sh"
    assert state["post_prov_reboot"]["90003"]["bus"] is None
    assert "post_prov_retry" not in state


def test_usb_clone_writes_sim_phy(monkeypatch):
    pve = _Pve()
    state, _ = _run(monkeypatch, pve, bus="3-1.2", image_num=2, dtype="wired")
    assert "echo 'sim_phy=wired' > /usr/local/scripts/usb-phy-override.conf" in pve.shell
    assert state["post_prov_reboot"]["90003"]["bus"] == "3-1.2"
    assert state["post_prov_reboot"]["90003"]["image_num"] == 2


def test_guest_not_back_is_queued_for_post_prov_retry(monkeypatch):
    pve = _Pve(comes_back=False)
    state, came_back = _run(monkeypatch, pve)
    assert came_back is False
    assert "bash /usr/local/scripts/update.sh" not in pve.shell
    assert state["post_prov_retry"]["90003"]["bus"] is None


def test_pci_settle_reboot_entry_fires(monkeypatch):
    # A T1/T3 VM has no vmid_to_bus entry; its bus=None entry must still fire.
    pve = _Pve()
    monkeypatch.setitem(sys.modules, "pxmx_agent_src_fc.pve_cmds", pve)
    state = {"vmid_to_bus": {},
             "post_prov_reboot": {"90003": {"cloned_at": 0, "reboot_at": 1,
                                            "bus": None, "image_num": 1}}}
    asyncio.new_event_loop().run_until_complete(
        _prov._run_post_prov_reboot_queue(_Agent(), state))
    assert ("reboot",) in pve.exec
    assert state["post_prov_reboot"] == {}
