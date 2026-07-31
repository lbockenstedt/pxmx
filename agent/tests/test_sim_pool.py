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
    def __init__(self, pool=None):
        usb = {"sim_pool": pool} if pool is not None else {}
        self.config = {"client_simulation": {"usb_config": usb}}


# ── config plumbing ──────────────────────────────────────────────────────────
def test_sim_pool_absent_is_none():
    assert up._sim_pool(_Agent()) is None
    assert up._sim_pool(_Agent("")) is None
    assert up._sim_pool(_Agent("   ")) is None


def test_sim_pool_read_and_trimmed():
    assert up._sim_pool(_Agent("  sim-clients  ")) == "sim-clients"


# ── listing ──────────────────────────────────────────────────────────────────
def test_list_pools_parses_poolid(monkeypatch):
    _stub_run(monkeypatch, 0, json.dumps(
        [{"poolid": "sim-clients"}, {"poolid": "lab"}, {"poolid": "sim-clients"}]))
    assert _run(pve_cmds.list_pools()) == ["lab", "sim-clients"]


@pytest.mark.parametrize("rc,out", [(1, ""), (0, ""), (0, "not json"), (0, "[]")])
def test_list_pools_degrades_to_empty(monkeypatch, rc, out):
    # A dropdown that can't load must show "no pools", never raise.
    _stub_run(monkeypatch, rc, out)
    assert _run(pve_cmds.list_pools()) == []


def test_schema_payload_is_not_mistaken_for_pools(monkeypatch):
    # THE bug: pvesh answered with the endpoint SCHEMA, and a naive parse turned
    # its keys into dropdown entries named "command" and "poolid".
    _stub_run(monkeypatch, 0, json.dumps({
        "command": {"description": "..."},
        "poolid": {"type": "string"},
        "info": {},
    }))
    assert _run(pve_cmds.list_pools()) == []


def test_bare_string_elements_are_ignored(monkeypatch):
    # A list of schema field NAMES must not become pools.
    _stub_run(monkeypatch, 0, json.dumps(["command", "poolid", "comment"]))
    assert _run(pve_cmds.list_pools()) == []


def test_data_envelope_is_unwrapped(monkeypatch):
    _stub_run(monkeypatch, 0, json.dumps({"data": [{"poolid": "sim-clients"}]}))
    assert _run(pve_cmds.list_pools()) == ["sim-clients"]


def test_single_pool_object(monkeypatch):
    _stub_run(monkeypatch, 0, json.dumps({"poolid": "sim-clients", "comment": "x"}))
    assert _run(pve_cmds.list_pools()) == ["sim-clients"]


def test_entries_without_a_poolid_are_dropped(monkeypatch):
    _stub_run(monkeypatch, 0, json.dumps(
        [{"poolid": "lab"}, {"comment": "no id here"}, {"poolid": ""}]))
    assert _run(pve_cmds.list_pools()) == ["lab"]


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
