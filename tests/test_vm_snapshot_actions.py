"""PXMX_VM_ACTION list_snapshots / delete_snapshot — used by the hub's shared
VM snapshot service (scheduled snapshots and pre-patch snapshots) to prune old
snapshots."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from test_vm_destroy_action import _AGENTS, _CP, _run, _runner  # noqa: E402
from proxmox_spoke import ProxmoxSpoke  # noqa: E402


def _act(cp, **data):
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    return _run(sp.handle_command("PXMX_VM_ACTION", {
        "unique_id": "PXMX/pve1/101", "vmid": 101, "node": "pve1", "type": "qemu", **data}))


def test_list_snapshots_returns_oldest_first_without_current():
    listing = [{"name": "current", "parent": "b"},
               {"name": "b", "snaptime": 200, "description": "lm-hub"},
               {"name": "a", "snaptime": 100}]
    cp = _CP(_AGENTS, {"/snapshot": _runner(stdout=json.dumps(listing))})
    res = _act(cp, action="list_snapshots")
    assert res["status"] == "SUCCESS", res
    assert [s["name"] for s in res["snapshots"]] == ["a", "b"]
    assert cp.cmds[-1][1] == "pvesh get /nodes/pve1/qemu/101/snapshot --output-format json"


def test_list_snapshots_surfaces_a_failure():
    cp = _CP(_AGENTS, {"/snapshot": _runner(rc=2, ok=False, stderr="no such VM")})
    res = _act(cp, action="list_snapshots")
    assert res["status"] == "ERROR"


def test_delete_snapshot_runs_delsnapshot():
    cp = _CP(_AGENTS)
    res = _act(cp, action="delete_snapshot", snapshot_name="lm-prepatch-202611010100")
    assert res["status"] == "SUCCESS", res
    assert cp.cmds[-1][1] == "qm delsnapshot 101 lm-prepatch-202611010100"


def test_delete_snapshot_needs_a_name():
    res = _act(_CP(_AGENTS), action="delete_snapshot")
    assert res["status"] == "ERROR" and "snapshot_name" in res["message"]
