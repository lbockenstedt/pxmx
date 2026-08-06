"""The hostname audit must cover EVERY managed sim VM, not just USB (T2) ones.

Regression: the audit walked ``bus_to_vmid`` — which is literally how
compute_vm_tiers defines T2 — so T1/T3 PCI-passthrough VMs were never audited.
A mis-stamped T1 kept the template hostname, never matched its registered
client (the sim-tag map joins on VM name == client hostname), and so never got
sim tags. Observed in the field as a VM named `wmiller` whose guest still said
`sim-rpi-0000`.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import usb_provision as up  # noqa: E402


class _Agent:
    def __init__(self):
        self.config = {"client_simulation": {}}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def harness(monkeypatch):
    """Stub the Proxmox seams; record which VMIDs the audit actually probes."""
    probed = []
    state = {"bus_to_vmid": {"1-1": 90001}}          # one T2 dongle VM

    monkeypatch.setattr(up, "load_usb_state", lambda: state)
    monkeypatch.setattr(up, "save_usb_state", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(up, "_protected_vmids", lambda a: {1001})
    # vm_names.json: identities exist for the whole range, tier-agnostic
    monkeypatch.setattr(up, "_vm_name", lambda v: f"client-{v}")

    fake = types.SimpleNamespace()
    fake.list_qemu_vmids = lambda: _aval([1001, 90001, 90002, 90003, 5])
    fake.is_template = lambda v, k=None: _aval(v == 90003)   # 90003 IS a template

    async def _ping(v):
        probed.append(v)
        return False          # QGA "down" → audit stops there; we only assert SCOPE
    fake.qm_agent_ping = _ping
    monkeypatch.setattr(up, "pve_cmds", fake, raising=False)
    sys.modules.setdefault("src.pve_cmds", fake)
    return probed, state


def _aval(v):
    async def _c():
        return v
    return _c()


def test_audit_probes_non_usb_vms(harness, monkeypatch):
    probed, _ = harness
    _run(up.hostname_audit_and_restamp(_Agent()))
    assert 90001 in probed, "the T2 dongle VM must still be audited"
    assert 90002 in probed, "a non-USB (T1/T3) sim VM must NOW be audited"


def test_audit_skips_protected_below_floor_and_templates(harness, monkeypatch):
    probed, _ = harness
    _run(up.hostname_audit_and_restamp(_Agent()))
    assert 1001 not in probed, "protected vmid must never be audited"
    assert 5 not in probed, "below the 90000 floor must never be audited"
    assert 90003 not in probed, "a template must never be re-stamped or rebooted"




def test_audit_reboots_at_most_N_vms_per_pass(monkeypatch):
    """A per-pass cap keeps a backlog from becoming a fleet-wide reboot wave.

    Regression: fixing the always-true predicate in qm_guest_exec_shell made
    every silently-mismatched VM on a host actionable in the SAME pass, so the
    audit re-stamped and rebooted them together. The fleet went offline and,
    once the clients aged past the tag window, their sim tags were cleared —
    observed as whole servers losing their labels at once.
    """
    stamped = []
    state = {"bus_to_vmid": {f"1-{i}": 90000 + i for i in range(1, 8)}}
    monkeypatch.setattr(up, "load_usb_state", lambda: state)
    monkeypatch.setattr(up, "save_usb_state", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(up, "_protected_vmids", lambda a: {1001})
    monkeypatch.setattr(up, "_vm_name", lambda v: f"client-{v}")
    monkeypatch.setattr(up, "_hostname_stamp_script", lambda n: f"stamp {n}")

    fake = types.SimpleNamespace()
    fake.list_qemu_vmids = lambda: _aval([])
    fake.is_template = lambda v, k=None: _aval(False)
    fake.qm_agent_ping = lambda v: _aval(True)
    # EVERY VM is misnamed → without a cap all 7 would be rebooted in one pass.
    fake.qm_guest_exec_shell_out = lambda v, s, **k: _aval("sim-rpi-0000")

    async def _shell(v, script, **k):
        stamped.append(v)
        return True
    fake.qm_guest_exec_shell = _shell
    fake.qm_guest_exec = lambda v, c: _aval(True)
    # hostname_audit_and_restamp does `from . import pve_cmds` INSIDE the
    # function, which shadows any module-global patch. `from . import X` resolves
    # via getattr on the PACKAGE, so once another test has imported the real
    # module, patching sys.modules alone is not enough — the package attribute
    # wins and the test silently runs against the real `qm`. Patch both.
    import importlib
    monkeypatch.setitem(sys.modules, "src.pve_cmds", fake)
    monkeypatch.setattr(importlib.import_module("src"), "pve_cmds", fake, raising=False)
    monkeypatch.setattr(up, "pve_cmds", fake, raising=False)
    monkeypatch.setattr(up.asyncio, "sleep", lambda *_a, **_k: _aval(None))

    acted = _run(up.hostname_audit_and_restamp(_Agent()))
    assert acted == up._HOSTNAME_FIX_MAX_PER_PASS, acted
    assert len(set(stamped)) == up._HOSTNAME_FIX_MAX_PER_PASS, stamped
    assert up._HOSTNAME_FIX_MAX_PER_PASS < 7, "cap must be below the backlog size"


def _hostname_giveup_harness(monkeypatch, destroy_side_effect=None):
    """A VM that has already exhausted _HOSTNAME_FIX_MAX re-stamp attempts,
    ready for the audit's next pass. destroy_side_effect: None → success,
    else an Exception instance raised by the stubbed destroy_vm."""
    destroyed = []
    state = {
        "bus_to_vmid": {"1-1": 90001},
        "hostname_fix": {"90001": {"attempts": up._HOSTNAME_FIX_MAX, "last": 0,
                                   "expected": "client-90001", "actual": "sim-rpi-0000",
                                   "stamped": True}},
    }
    monkeypatch.setattr(up, "load_usb_state", lambda: state)
    monkeypatch.setattr(up, "save_usb_state", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(up, "_protected_vmids", lambda a: {1001})
    monkeypatch.setattr(up, "_vm_name", lambda v: f"client-{v}")

    pve = types.SimpleNamespace()
    pve.list_qemu_vmids = lambda: _aval([])
    pve.is_template = lambda v, k=None: _aval(False)
    monkeypatch.setitem(sys.modules, "src.pve_cmds", pve)
    monkeypatch.setattr(up, "pve_cmds", pve, raising=False)

    cs_sim = types.SimpleNamespace()

    async def _destroy(agent, vmid, **k):
        destroyed.append(vmid)
        if destroy_side_effect is not None:
            raise destroy_side_effect
        return {"ok": True}
    cs_sim.destroy_vm = _destroy
    # `from . import cs_sim` inside the function resolves via getattr on the
    # PACKAGE (see the per-pass-cap test above for the same gotcha with
    # pve_cmds) — patch both the module registry and the package attribute.
    import importlib
    monkeypatch.setitem(sys.modules, "src.cs_sim", cs_sim)
    monkeypatch.setattr(importlib.import_module("src"), "cs_sim", cs_sim, raising=False)
    return destroyed, state


def test_audit_destroys_vm_after_max_attempts(monkeypatch):
    """Past _HOSTNAME_FIX_MAX re-stamp attempts, re-stamping in place is
    abandoned — the audit used to just skip the VM forever with nothing ever
    surfacing it. It must now destroy the VM so the provisioning loop reclones
    a fresh one into the freed slot, and clear the exhausted strike record."""
    destroyed, state = _hostname_giveup_harness(monkeypatch)
    _run(up.hostname_audit_and_restamp(_Agent()))
    assert destroyed == [90001]
    assert "90001" not in state["hostname_fix"], "strike record must clear on a successful destroy"


def test_audit_retries_destroy_after_a_failed_attempt(monkeypatch):
    """A destroy_vm failure (e.g. a transient PVE API error) must NOT leave the
    VM permanently stuck — the strike record's `last` is bumped so the next
    pass (after cooldown) tries destroying it again, instead of the top-of-loop
    attempts>=MAX check skipping it forever."""
    destroyed, state = _hostname_giveup_harness(monkeypatch, destroy_side_effect=RuntimeError("qm destroy failed"))
    _run(up.hostname_audit_and_restamp(_Agent()))
    assert destroyed == [90001], "a failed destroy must still be attempted"
    rec = state["hostname_fix"].get("90001")
    assert rec is not None, "the record must survive a failed destroy so it's retried, not abandoned"
    assert rec["attempts"] == up._HOSTNAME_FIX_MAX
    assert rec["last"] > 0, "`last` must be bumped so the retry respects the cooldown, not fire every pass"
