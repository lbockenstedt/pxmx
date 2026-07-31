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
    monkeypatch.setitem(sys.modules, "src.cs_guard", _guard())
    _run(up.hostname_audit_and_restamp(_Agent()))
    assert 90001 in probed, "the T2 dongle VM must still be audited"
    assert 90002 in probed, "a non-USB (T1/T3) sim VM must NOW be audited"


def test_audit_skips_protected_below_floor_and_templates(harness, monkeypatch):
    probed, _ = harness
    monkeypatch.setitem(sys.modules, "src.cs_guard", _guard())
    _run(up.hostname_audit_and_restamp(_Agent()))
    assert 1001 not in probed, "protected vmid must never be audited"
    assert 5 not in probed, "below the 90000 floor must never be audited"
    assert 90003 not in probed, "a template must never be re-stamped or rebooted"


def _guard():
    m = types.ModuleType("src.cs_guard")

    def is_sim_vm(vmid, protected, sim_min=90000):
        try:
            v = int(vmid)
        except (TypeError, ValueError):
            return False
        return v >= sim_min and v not in set(protected)

    m.is_sim_vm = is_sim_vm
    return m
