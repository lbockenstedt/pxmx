"""Regression: the Hypervisors view's Delete button must actually destroy a VM.

The agent-rework #4 migration moved every VM action from the Agent's typed
PXMX_VM_ACTION handler onto the spoke's RUN_COMMAND path (``_vm_action_one``)
but DROPPED the ``destroy``/``delete`` family. The Agent still implemented it
(agent/src/pve_cmds.py) but was never asked, so every Delete — single row AND
"select all → Delete" — came back ``unknown vm action: destroy`` and no VM was
ever removed. The hub/WebUI side (confirm dialog, delete-protection, bulk
results toast) was fully wired, so the failure was silent: the toast just read
"0 ok, N failed".

These tests pin the RUN_COMMAND sequence the spoke must emit:
``qm/pct stop <vmid>`` (best-effort — destroy FAILS on a running guest) then
``qm/pct destroy <vmid> --purge`` (disks + backup-job membership, not just the
config), mirroring the Agent's typed handler.
"""
import sys
from pathlib import Path

_PXMX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PXMX / "src"))

import asyncio  # noqa: E402

import proxmox_spoke as _ps  # noqa: E402
from proxmox_spoke import ProxmoxSpoke  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _runner(stdout="", rc=0, stderr="", ok=True, error=""):
    return {"ok": ok, "rc": rc, "stdout": stdout, "stderr": stderr,
            "truncated": False, "error": error, "mode": "shell"}


class _CP:
    """Records every RUN_COMMAND; returns a per-command-substring response."""

    def __init__(self, agents, by_cmd=None):
        self.connected_agents = agents
        self.by_cmd = by_cmd or {}
        self.cmds = []

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=15.0):
        c = data.get("command", "")
        self.cmds.append((agent_id, c))
        for frag, resp in self.by_cmd.items():
            if frag in c:
                if resp is None:
                    raise RuntimeError("agent unreachable")
                return resp
        return _runner()


_AGENTS = {"a-1": {"cluster_name": "PXMX", "nodes": ["pve1"]}}


# ── command builder ──────────────────────────────────────────────────────────

def test_destroy_cmd_is_purging_qm_destroy():
    assert _ps._vm_destroy_cmd(9001, "qemu") == "qm destroy 9001 --purge"


def test_destroy_cmd_uses_pct_for_containers():
    assert _ps._vm_destroy_cmd(9002, "lxc") == "pct destroy 9002 --purge"


def test_destroy_cmd_falls_back_when_core_builder_predates_it(monkeypatch):
    # Spoke and the vendored lm/core module deploy independently — a spoke that
    # updates first must not break on an older builder.
    monkeypatch.delattr(_ps.pve_cmd_builder, "vm_destroy_cmd", raising=False)
    assert _ps._vm_destroy_cmd(9003, "qemu") == "qm destroy 9003 --purge"


# ── single PXMX_VM_ACTION ────────────────────────────────────────────────────

def test_single_destroy_stops_then_purges():
    cp = _CP(_AGENTS)
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION", {
        "unique_id": "PXMX/pve1/9001", "vmid": 9001, "node": "pve1",
        "type": "qemu", "action": "destroy"}))
    assert res["status"] == "SUCCESS", res
    assert res.get("purged") is True
    assert [c for _, c in cp.cmds] == ["qm stop 9001", "qm destroy 9001 --purge"]


def test_single_destroy_accepts_delete_alias():
    cp = _CP(_AGENTS)
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION", {
        "unique_id": "PXMX/pve1/9001", "vmid": 9001, "node": "pve1",
        "type": "qemu", "action": "delete"}))
    assert res["status"] == "SUCCESS", res
    assert "qm destroy 9001 --purge" in [c for _, c in cp.cmds]


def test_single_destroy_ignores_stop_failure_on_stopped_guest():
    # `qm stop` on an already-stopped VM exits non-zero; destroy must still run.
    cp = _CP(_AGENTS, {"stop": _runner(rc=1, stderr="VM 9001 not running")})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION", {
        "vmid": 9001, "node": "pve1", "type": "qemu", "action": "destroy"}))
    assert res["status"] == "SUCCESS", res


def test_single_destroy_surfaces_real_destroy_failure():
    cp = _CP(_AGENTS, {"destroy": _runner(rc=1, stderr="VM is locked (backup)")})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION", {
        "vmid": 9001, "node": "pve1", "type": "qemu", "action": "destroy"}))
    assert res["status"] == "ERROR"
    assert "locked" in res["message"]


def test_unknown_action_still_rejected():
    cp = _CP(_AGENTS)
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION", {
        "vmid": 9001, "node": "pve1", "type": "qemu", "action": "frobnicate"}))
    assert res["status"] == "ERROR"
    assert "unknown vm action" in res["message"]


# ── bulk PXMX_VM_ACTION_BULK (the "select all → Delete" path) ────────────────

def test_bulk_destroy_purges_every_selected_vm():
    cp = _CP(_AGENTS)
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION_BULK", {
        "action": "destroy",
        "items": [{"unique_id": "PXMX/pve1/9001", "vmid": 9001, "type": "qemu"},
                  {"unique_id": "PXMX/pve1/9002", "vmid": 9002, "type": "lxc"}]}))
    assert res["status"] == "SUCCESS"
    assert all(r["ok"] for r in res["results"]), res["results"]
    issued = [c for _, c in cp.cmds]
    assert "qm destroy 9001 --purge" in issued
    assert "pct destroy 9002 --purge" in issued


def test_bulk_destroy_one_failure_does_not_sink_the_rest():
    cp = _CP(_AGENTS, {"destroy 9002": _runner(rc=1, stderr="locked")})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION_BULK", {
        "action": "destroy",
        "items": [{"unique_id": "PXMX/pve1/9001", "vmid": 9001, "type": "qemu"},
                  {"unique_id": "PXMX/pve1/9002", "vmid": 9002, "type": "qemu"}]}))
    rows = {r["vmid"]: r for r in res["results"]}
    assert rows[9001]["ok"] is True
    assert rows[9002]["ok"] is False
    assert "locked" in rows[9002]["error"]


def test_bulk_destroy_no_longer_reports_unknown_vm_action():
    # The exact symptom the operator saw: "select all → Delete" toasted
    # "0 ok, N failed" because every row came back unknown vm action: destroy.
    cp = _CP(_AGENTS)
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_VM_ACTION_BULK", {
        "action": "destroy",
        "items": [{"unique_id": "PXMX/pve1/900%d" % i, "vmid": 9000 + i,
                   "type": "qemu"} for i in range(1, 6)]}))
    assert res["results"] and not any(
        "unknown vm action" in str(r.get("error", "")) for r in res["results"])
    assert sum(1 for r in res["results"] if r["ok"]) == 5
