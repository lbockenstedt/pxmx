"""The T2 allocator must never destroy a T1/T3 PCI VM.

Field regression (clean build, svr-01, four 1912:0015 controllers):
  15:50  T1 provisions 90001-90004 with all four cards   <- T1 DID go first
  ~15:5x T2 allocator walks from 90001, sees VMs with no usb0, calls them
         zombies, DESTROYS them and takes the VMIDs for dongles
  16:01  T1 re-provisions card 03:00.0 onto 90005 ...
  16:14  ... and again onto 90012, 90013, 90014, 90015
Only the last cycle survived, so from the outside it looked like "T1 was
provisioned last" — it went first every time and was eaten. Each cycle also cost
four clone+destroy pairs of real host load.

Cause: _vm_has_usb_passthrough was the allocator's only "is this a real VM"
test, and a T1/T3 VM is EXACTLY "a VM with no USB passthrough" (it carries
hostpciN and deliberately keeps no usb_state) — indistinguishable from a
half-cloned zombie.
"""
import asyncio
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import usb_provision as up  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _stub_cfg(monkeypatch, cfg):
    fake = types.SimpleNamespace()

    async def _qm_config(v):
        if cfg is None:
            raise RuntimeError("qm config failed")
        return cfg
    fake.qm_config = _qm_config
    monkeypatch.setitem(sys.modules, "src.pve_cmds", fake)
    import importlib
    monkeypatch.setattr(importlib.import_module("src"), "pve_cmds", fake, raising=False)
    monkeypatch.setattr(up, "pve_cmds", fake, raising=False)


def test_t1_vm_is_detected_as_pci(monkeypatch):
    _stub_cfg(monkeypatch, {"name": "client-90001",
                            "hostpci0": "0000:03:00.0,pcie=1"})
    assert _run(up._vm_has_pci_passthrough(90001)) is True


def test_t2_dongle_vm_is_not_pci(monkeypatch):
    _stub_cfg(monkeypatch, {"name": "client-90001", "usb0": "host=3-5"})
    assert _run(up._vm_has_pci_passthrough(90001)) is False


def test_bare_zombie_is_not_pci(monkeypatch):
    # A genuine half-cloned zombie: no passthrough of either kind. Must stay
    # reclaimable, or the allocator wedges on leftover configs.
    _stub_cfg(monkeypatch, {"name": "sim-rpi-0000"})
    assert _run(up._vm_has_pci_passthrough(90001)) is False


def test_unknown_config_fails_closed(monkeypatch):
    # qm config failing must NOT read as "no passthrough" — that is the path
    # that destroys a live VM. Unknown => treat as occupied.
    _stub_cfg(monkeypatch, None)
    assert _run(up._vm_has_pci_passthrough(90001)) is True


def test_multiple_hostpci_lines(monkeypatch):
    _stub_cfg(monkeypatch, {"hostpci1": "0000:04:00.0,pcie=1"})
    assert _run(up._vm_has_pci_passthrough(90002)) is True
