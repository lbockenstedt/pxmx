"""HA maintenance mode + cluster detection used by the hub's OS-updates
Auto-reboot (pve_cmds.cluster_info / node_maintenance). No real pvesh."""
import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")
SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("pxmx_agent_src_nm")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_nm"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"pxmx_agent_src_nm.{name}", SRC / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("cs_guard")
pve_cmds = _load("pve_cmds")

CLUSTER = [{"type": "cluster", "name": "lab"},
           {"type": "node", "name": "pve1", "local": 1},
           {"type": "node", "name": "pve2", "local": 0}]
MANAGER = {"manager_status": {
    "node_status": {"pve1": "maintenance", "pve2": "online"},
    "service_status": {"vm:100": {"node": "pve1", "state": "migrate"},
                       "vm:101": {"node": "pve2", "state": "started"},
                       "vm:102": {"node": "pve1", "state": "stopped"}}}}


def _fake_run(outputs, calls):
    async def _run(argv, **_kw):
        calls.append(argv)
        key = argv[2] if argv[0] == "pvesh" else argv[0]
        rc, out = outputs.get(key, (0, ""))
        return rc, (out if isinstance(out, str) else json.dumps(out)).encode(), b"boom"
    return _run


def test_cluster_info_only_for_multi_node_clusters(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({"/cluster/status": (0, CLUSTER)}, calls))
    assert asyncio.run(pve_cmds.cluster_info()) == {"name": "lab", "node": "pve1", "nodes": 2}
    standalone = [{"type": "node", "name": "pve1", "local": 1}]
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({"/cluster/status": (0, standalone)}, calls))
    assert asyncio.run(pve_cmds.cluster_info()) == {}
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({"/cluster/status": (1, "")}, calls))
    assert asyncio.run(pve_cmds.cluster_info()) == {}


def test_maintenance_enable_reports_ha_guests_still_on_the_node(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({
        "/cluster/status": (0, CLUSTER),
        "/cluster/ha/status/manager_status": (0, MANAGER)}, calls))
    r = asyncio.run(pve_cmds.node_maintenance("enable"))
    assert ["ha-manager", "crm-command", "node-maintenance", "enable", "pve1"] in calls
    assert r == {"status": "SUCCESS", "node": "pve1", "maintenance": True, "ha_guests": ["vm:100"]}


def test_maintenance_failure_and_bad_action(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({
        "/cluster/status": (0, CLUSTER), "ha-manager": (2, "")}, calls))
    r = asyncio.run(pve_cmds.node_maintenance("disable"))
    assert r["status"] == "ERROR" and "boom" in r["message"]
    assert asyncio.run(pve_cmds.node_maintenance("reboot"))["status"] == "ERROR"
