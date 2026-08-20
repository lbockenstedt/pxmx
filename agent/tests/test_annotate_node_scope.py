"""annotate_vm_interfaces enriches only the LOCAL node's VMs.

get_vm_list sources /cluster/resources (cluster-WIDE), so on a multi-node
cluster the list also carries VMs owned by OTHER nodes. A guest-agent/config
probe for a remote VM makes pvesh proxy the call over SSH to that node — dozens
of SSH round-trips per tick that stall the whole vm_list phase (the "telemetry
collect exceeded 25s" symptom on a freshly-joined node). Each remote VM is
enriched by its own owning agent, so this agent must probe only its own node.

Fail OPEN: if the local node can't be resolved, or NO VM matches it, enrich the
full list — a slower tick beats blank IPs. Mirrors the _cs_telemetry_body node
filter.
"""
import asyncio
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import vm_inventory  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _Agent:
    def __init__(self, hostname, local_node=None):
        self.hostname = hostname
        if local_node is not None:
            self._local_node = local_node


def _vm(vmid, node, status="running"):
    return {"vmid": vmid, "node": node, "type": "qemu", "status": status,
            "interfaces": [], "ips": []}


@pytest.fixture
def probed(monkeypatch):
    """Record every (node, vmid) vm_interfaces is asked to enrich."""
    seen = []

    async def _fake(agent, node, vmid, rtype, status):
        seen.append((node, vmid))
        return [{"name": "net0", "mac": "aa:bb", "ips": ["10.0.0.1"]}]

    monkeypatch.setattr(vm_inventory, "vm_interfaces", _fake)
    return seen


def test_only_local_node_vms_are_probed(probed):
    agent = _Agent("pxmx-r05-cu")
    vms = [_vm(100, "pxmx-r05-cu"), _vm(200, "pxmx-r05-cu"),
           _vm(300, "other-node"), _vm(400, "another-node")]
    _run(vm_inventory.annotate_vm_interfaces(agent, vms))
    assert sorted(probed) == [("pxmx-r05-cu", 100), ("pxmx-r05-cu", 200)]
    # Remote VMs keep their empty interfaces (their own agent fills them).
    assert vms[2]["ips"] == [] and vms[3]["ips"] == []
    # Local VMs got enriched.
    assert vms[0]["ips"] == ["10.0.0.1"]


def test_local_node_prefers_local_node_over_hostname(probed):
    # _local_node wins when set (still resolves the same short node name).
    agent = _Agent("fallback", local_node="node-a")
    vms = [_vm(1, "node-a"), _vm(2, "node-b")]
    _run(vm_inventory.annotate_vm_interfaces(agent, vms))
    assert probed == [("node-a", 1)]


def test_fail_open_when_no_vm_matches_local_node(probed):
    # No VM reports node == local → enrich the FULL list rather than nothing.
    agent = _Agent("pxmx-r05-cu")
    vms = [_vm(1, "node-x"), _vm(2, "node-y")]
    _run(vm_inventory.annotate_vm_interfaces(agent, vms))
    assert sorted(probed) == [("node-x", 1), ("node-y", 2)]


def test_fail_open_when_local_node_unresolvable(probed):
    # hostname empty and no _local_node → cannot scope → enrich all.
    agent = _Agent("")
    vms = [_vm(1, "node-x"), _vm(2, "node-y")]
    _run(vm_inventory.annotate_vm_interfaces(agent, vms))
    assert sorted(probed) == [("node-x", 1), ("node-y", 2)]


def test_cache_prunes_to_local_targets(probed):
    agent = _Agent("node-a")
    vms = [_vm(1, "node-a"), _vm(2, "node-b")]
    _run(vm_inventory.annotate_vm_interfaces(agent, vms))
    # Only the local VM should be cached; the remote one is never probed/cached.
    assert set(agent._iface_cache.keys()) == {"1"}
