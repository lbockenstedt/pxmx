"""reboot_vm, Reclone Clear and the other handled actions must reach their modules.

Function-local ``from . import pve_cmds, usb_provision`` lines further down handle_cs_command made
both names local to the whole function, so every action handled before that
line failed with "cannot access local variable" errors.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_vm")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_vm"] = _pkg

_spec = importlib.util.spec_from_file_location("pxmx_agent_src_vm.cs_commands",
                                               SRC / "cs_commands.py")
cs_commands = importlib.util.module_from_spec(_spec)
sys.modules["pxmx_agent_src_vm.cs_commands"] = cs_commands
_spec.loader.exec_module(cs_commands)


class _FakeAgent:
    cs_enabled = True
    config = {"client_simulation": {}}


def test_reboot_vm_reaches_pve_cmds(monkeypatch):
    calls = []

    async def _reboot(vmid, protected):
        calls.append(vmid)
        return {"vmid": vmid}

    monkeypatch.setattr(cs_commands.pve_cmds, "reboot_vm", _reboot)
    resp = asyncio.run(cs_commands.handle_cs_command(_FakeAgent(), "reboot_vm", {"vmid": 105}))
    assert resp["status"] == "SUCCESS", resp
    assert calls == [105]


def test_reclone_clear_reaches_usb_provision(monkeypatch):
    monkeypatch.setattr(cs_commands.usb_provision, "clear_reclone_state", lambda: True)
    resp = asyncio.run(cs_commands.handle_cs_command(_FakeAgent(), "proxmox_reclone_clear", {}))
    assert resp["cleared"] is True, resp
