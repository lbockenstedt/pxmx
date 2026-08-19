"""Proxmox resource pool for auto-provisioned sim clients."""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import pve_cmds, usb_provision as up  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _stub_run(monkeypatch, rc, out, capture=None):
    async def _fake(argv, **kw):
        if capture is not None:
            capture.append(argv)
        return rc, (out.encode() if isinstance(out, str) else out), b""
    monkeypatch.setattr(pve_cmds, "_run", _fake)


class _Agent:
    def __init__(self, pool=None, existing_pools=None, create_error=None):
        usb = {"sim_pool": pool} if pool is not None else {}
        self.config = {"client_simulation": {"usb_config": usb}}
        self._existing_pools = existing_pools or []
        self._create_error = create_error
        self.pvesh_calls = []
        self.pvesh_action_calls = []

    async def _pvesh(self, path):
        self.pvesh_calls.append(path)
        assert path == "/pools"
        return [{"poolid": p} for p in self._existing_pools]

    async def _pvesh_action(self, verb, path, *args):
        self.pvesh_action_calls.append((verb, path, *args))
        if self._create_error:
            raise RuntimeError(self._create_error)


# ── config plumbing ──────────────────────────────────────────────────────────
def test_sim_pool_absent_is_none():
    assert up._sim_pool(_Agent()) is None
    assert up._sim_pool(_Agent("")) is None
    assert up._sim_pool(_Agent("   ")) is None


def test_sim_pool_read_and_trimmed():
    assert up._sim_pool(_Agent("  sim-clients  ")) == "sim-clients"


# NOTE: pool LISTING is vm_inventory.list_pools (Proxmox API, pre-existing and
# already strict). A second shell-based lister here shadowed the existing
# PXMX_LIST_POOLS handler and turned a pvesh schema payload into pools named
# "command"/"poolid"; it was removed rather than fixed twice.


# ── clone places the VM in the pool ──────────────────────────────────────────
def test_qm_clone_passes_pool_flag(monkeypatch):
    argv = []
    _stub_run(monkeypatch, 0, "", argv)
    monkeypatch.setattr(pve_cmds, "assert_sim_vm", lambda v, p, **k: int(v))
    _run(pve_cmds.qm_clone(100, 90001, "client-90001", pool="sim-clients"))
    assert "--pool" in argv[0] and "sim-clients" in argv[0]


def test_qm_clone_omits_pool_when_unset(monkeypatch):
    argv = []
    _stub_run(monkeypatch, 0, "", argv)
    monkeypatch.setattr(pve_cmds, "assert_sim_vm", lambda v, p, **k: int(v))
    _run(pve_cmds.qm_clone(100, 90001, "client-90001"))
    assert "--pool" not in argv[0]


# ── retrofit ─────────────────────────────────────────────────────────────────
def test_pool_add_vms_builds_csv(monkeypatch):
    argv = []
    _stub_run(monkeypatch, 0, "", argv)
    r = _run(pve_cmds.pool_add_vms("sim-clients", [90001, 90002]))
    assert r["status"] == "SUCCESS" and r["added"] == 2
    assert argv[0][:3] == ["pvesh", "set", "/pools/sim-clients"]
    assert "90001,90002" in argv[0]


def test_pool_add_vms_reports_proxmox_error(monkeypatch):
    # A VM already in another pool errors — surfaced, not silently swallowed,
    # because moving a VM between pools is an operator decision.
    async def _fake(argv, **kw):
        return 2, b"", b"VM 90001 is already a member of pool other"
    monkeypatch.setattr(pve_cmds, "_run", _fake)
    r = _run(pve_cmds.pool_add_vms("sim-clients", [90001]))
    assert r["status"] == "ERROR" and "already a member" in r["message"]


def test_pool_add_vms_noop_without_pool_or_vms(monkeypatch):
    called = []
    _stub_run(monkeypatch, 0, "", called)
    assert _run(pve_cmds.pool_add_vms("", [90001]))["added"] == 0
    assert _run(pve_cmds.pool_add_vms("p", []))["added"] == 0
    assert not called, "must not shell out for a no-op"


# ── self-heal: create the sim pool on THIS host if it's missing ─────────────
# usb_config's sim_pool is one value pushed to every host uniformly, but a
# pool that exists on one Proxmox cluster can be entirely absent on another
# (or on a standalone host) — the reported bug: every clone attempt failed
# forever with "403 Permission check failed (pool 'X' does not exist)"
# because nothing ever created it there.

def _clear_verified_pools():
    up._verified_sim_pools.clear()


def test_ensure_sim_pool_noop_when_unset(monkeypatch):
    _clear_verified_pools()
    agent = _Agent(pool=None)
    assert _run(up._ensure_sim_pool(agent)) is None
    assert not agent.pvesh_calls and not agent.pvesh_action_calls


def test_ensure_sim_pool_noop_when_already_exists(monkeypatch):
    _clear_verified_pools()
    agent = _Agent(pool="Simulations", existing_pools=["Simulations", "Other"])
    assert _run(up._ensure_sim_pool(agent)) == "Simulations"
    assert agent.pvesh_calls == ["/pools"]
    assert not agent.pvesh_action_calls, "must not attempt to create a pool that already exists"


def test_ensure_sim_pool_creates_when_missing(monkeypatch):
    _clear_verified_pools()
    agent = _Agent(pool="Simulations", existing_pools=[])
    assert _run(up._ensure_sim_pool(agent)) == "Simulations"
    assert agent.pvesh_calls == ["/pools"]
    assert agent.pvesh_action_calls == [("create", "/pools", "--poolid", "Simulations")]


def test_ensure_sim_pool_caches_across_calls(monkeypatch):
    _clear_verified_pools()
    agent = _Agent(pool="Simulations", existing_pools=[])
    _run(up._ensure_sim_pool(agent))
    _run(up._ensure_sim_pool(agent))
    # Second call is served from the process-lifetime cache — no second /pools
    # read and no second (redundant) create attempt.
    assert agent.pvesh_calls == ["/pools"]
    assert agent.pvesh_action_calls == [("create", "/pools", "--poolid", "Simulations")]


def test_ensure_sim_pool_swallows_create_failure(monkeypatch):
    _clear_verified_pools()
    agent = _Agent(pool="Simulations", existing_pools=[], create_error="pool exists")
    # Never raises — best-effort; the clone call still surfaces its own error
    # same as before this fix if the create didn't actually help.
    assert _run(up._ensure_sim_pool(agent)) == "Simulations"
    assert agent.pvesh_action_calls == [("create", "/pools", "--poolid", "Simulations")]
