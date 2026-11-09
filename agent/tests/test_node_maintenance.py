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


async def _noop_drain(*_a):
    return None


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
    monkeypatch.setattr(pve_cmds, "_drain_node", _noop_drain)
    r = asyncio.run(pve_cmds.node_maintenance("enable"))
    assert ["ha-manager", "crm-command", "node-maintenance", "enable", "pve1"] in calls
    assert r["maintenance"] is True and r["ha_guests"] == ["vm:100"] and r["local_guests"] == []


def test_maintenance_failure_and_bad_action(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({
        "/cluster/status": (0, CLUSTER), "ha-manager": (2, "")}, calls))
    r = asyncio.run(pve_cmds.node_maintenance("disable"))
    assert r["status"] == "ERROR" and "boom" in r["message"]
    assert asyncio.run(pve_cmds.node_maintenance("reboot"))["status"] == "ERROR"


def test_drain_migrates_then_shuts_down_what_cannot_move(monkeypatch, tmp_path):
    monkeypatch.setattr(pve_cmds, "_MAINT_STATE", tmp_path / "m.json")
    calls = []

    async def _run(argv, **_kw):
        calls.append(argv)
        if argv[0] == "pvesh":
            out = {"/cluster/status": [CLUSTER[0], CLUSTER[1], {**CLUSTER[2], "online": 1}],
                   "/nodes/pve1/qemu": [{"vmid": 200, "status": "running"},
                                        {"vmid": 201, "status": "running"},
                                        {"vmid": 202, "status": "stopped"}],
                   "/nodes/pve1/lxc": []}.get(argv[2])
            return 0, json.dumps(out).encode(), b""
        # vm 201 has a local disk: its migration fails
        return (1 if argv[:3] == ["qm", "migrate", "201"] else 0), b"", b""

    monkeypatch.setattr(pve_cmds, "_run", _run)
    asyncio.run(pve_cmds._drain_node("pve1", True, set()))
    assert ["qm", "migrate", "200", "pve2", "--online"] in calls
    assert ["qm", "shutdown", "201", "--timeout", "120"] in calls
    assert not any(c[:3] == ["qm", "shutdown", "200"] for c in calls)
    assert pve_cmds._load_shutdown() == [{"vmid": 201, "kind": "qemu"}]
    # maintenance off starts only what was shut down
    calls.clear()
    assert asyncio.run(pve_cmds._restart_shutdown_guests()) == [201]
    assert calls == [["qm", "start", "201"]] and pve_cmds._load_shutdown() == []


def test_standalone_host_shuts_guests_down(monkeypatch, tmp_path):
    monkeypatch.setattr(pve_cmds, "_MAINT_STATE", tmp_path / "m.json")
    calls = []

    async def _run(argv, **_kw):
        calls.append(argv)
        rows = [{"vmid": 300, "status": "running"}] if argv[:3] == ["pvesh", "get", "/nodes/pve1/lxc"] else []
        return 0, json.dumps(rows).encode(), b""

    monkeypatch.setattr(pve_cmds, "_run", _run)
    asyncio.run(pve_cmds._drain_node("pve1", False, set()))
    assert ["pct", "shutdown", "300", "--timeout", "120"] in calls
    assert not any(c[1:2] == ["migrate"] for c in calls)
