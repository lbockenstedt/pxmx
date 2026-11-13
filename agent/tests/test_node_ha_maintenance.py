"""Hypervisors → Nodes "Maintenance" button: PXMX_NODE_MAINTENANCE runs
``ha-manager crm-command node-maintenance enable|disable <node>`` as an argv
list for a validated cluster member, and get_node_stats flags each node with
``maintenance`` from the HA manager status. No real pvesh or ha-manager."""
import os

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

import asyncio  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
from pathlib import Path  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"
_pkg = types.ModuleType("pxmx_agent_src_hm")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_hm"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "pxmx_agent_src_hm.agent", SRC / "agent.py", submodule_search_locations=[str(SRC)])
agent = importlib.util.module_from_spec(_spec)
sys.modules["pxmx_agent_src_hm.agent"] = agent
_spec.loader.exec_module(agent)
pve_cmds = agent.pve_cmds

CLUSTER = [{"type": "cluster", "name": "lab"},
           {"type": "node", "name": "pve1", "local": 1},
           {"type": "node", "name": "pve2", "local": 0}]
# Trimmed `pvesh get /cluster/ha/status/manager_status`: pve1 fully in
# maintenance, pve3's LRM still entering it, pve2 normal.
MANAGER = {"manager_status": {"master_node": "pve2", "timestamp": int(time.time()),
                              "service_status": {"vm:100": {"node": "pve2", "state": "started"}},
                              "node_status": {"pve1": "maintenance", "pve2": "online",
                                              "pve3": "online"}},
           "lrm_status": {"pve1": {"mode": "maintenance", "state": "wait_for_agent_lock"},
                          "pve2": {"mode": "active", "state": "active"},
                          "pve3": {"mode": "maintenance", "state": "active"}}}


def _fake_run(outputs, calls):
    """A pve_cmds._run stand-in keyed by pvesh path (or argv[0])."""
    async def _run(argv, **_kw):
        calls.append(argv)
        key = argv[2] if argv[0] == "pvesh" else argv[0]
        rc, out = outputs.get(key, (0, ""))
        return rc, (out if isinstance(out, str) else json.dumps(out)).encode(), b"no quorum"
    return _run


def test_argv_is_a_list_with_the_node_as_one_argument():
    assert pve_cmds.ha_maintenance_argv("pve1", True) == [
        "ha-manager", "crm-command", "node-maintenance", "enable", "pve1"]
    assert pve_cmds.ha_maintenance_argv("pve-2.lab", False)[3:] == ["disable", "pve-2.lab"]


def test_invalid_node_names_are_rejected():
    for bad in ["", "-pve1", "pve1; reboot", "pve1 pve2", "a" * 64, "$(id)", None, 5, "pve_1", "pve1\n"]:
        assert not pve_cmds.valid_node_name(bad), bad
    try:
        pve_cmds.ha_maintenance_argv("pve1;reboot", True)
    except ValueError:
        pass
    else:
        raise AssertionError("ha_maintenance_argv accepted a bad name")


def test_enable_runs_ha_manager_for_a_cluster_member(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run(
        {"/cluster/status": (0, CLUSTER), "/cluster/ha/status/manager_status": (0, MANAGER)}, calls))
    r = asyncio.run(pve_cmds.set_ha_node_maintenance("pve2", True))
    assert r["status"] == "SUCCESS" and r["maintenance"] is True, r
    assert calls[-1] == ["ha-manager", "crm-command", "node-maintenance", "enable", "pve2"]


def test_unknown_or_malformed_node_never_reaches_ha_manager(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({"/cluster/status": (0, CLUSTER)}, calls))
    assert asyncio.run(pve_cmds.set_ha_node_maintenance("pve9", True))["status"] == "ERROR"
    assert asyncio.run(pve_cmds.set_ha_node_maintenance("pve1;reboot", True))["status"] == "ERROR"
    assert not [c for c in calls if c[0] == "ha-manager"]


def test_ha_manager_failure_is_reported(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({
        "/cluster/status": (0, CLUSTER), "/cluster/ha/status/manager_status": (0, MANAGER),
        "ha-manager": (255, "")}, calls))
    r = asyncio.run(pve_cmds.set_ha_node_maintenance("pve1", False))
    assert r["status"] == "ERROR" and "no quorum" in r["message"], r


def test_maintenance_state_parsed_from_ha_status():
    assert pve_cmds.maintenance_nodes(MANAGER) == {"pve1", "pve3"}
    # Unknown, not "nobody in maintenance": no active CRM, stale or malformed status.
    ms = MANAGER["manager_status"]
    for bad in [None, {}, [], {"manager_status": {}},
                {"manager_status": {**ms, "master_node": None}},
                {"manager_status": {**ms, "node_status": None}},
                {"manager_status": {**ms, "timestamp": "x"}},
                {"manager_status": {**ms, "timestamp": time.time() - 600}},
                {"manager_status": {**ms, "service_status": {}}}]:
        assert pve_cmds.maintenance_nodes(bad) is None, bad


def test_node_stats_carry_the_maintenance_flag(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run(
        {"/cluster/ha/status/manager_status": (0, MANAGER)}, calls))

    class _Me:
        async def _collect_node_stats(self):
            return {"nodes": [{"node": "pve1"}, {"node": "pve2"}], "cluster": "lab"}
    res = asyncio.run(agent.ProxmoxAgent.get_node_stats(_Me()))
    assert [(n["node"], n["maintenance"]) for n in res["nodes"]] == [
        ("pve1", True), ("pve2", False)]


def test_node_stats_leave_the_flag_off_when_ha_status_is_unreadable(monkeypatch):
    monkeypatch.setattr(pve_cmds, "_run", _fake_run(
        {"/cluster/ha/status/manager_status": (1, "")}, []))

    class _Me:
        async def _collect_node_stats(self):
            return {"nodes": [{"node": "pve1"}], "cluster": "lab"}
    res = asyncio.run(agent.ProxmoxAgent.get_node_stats(_Me()))
    assert "maintenance" not in res["nodes"][0]


def test_enable_must_be_a_real_bool(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run({"/cluster/status": (0, CLUSTER)}, calls))
    for bad in [None, "false", "true", 0, 1]:
        r = asyncio.run(pve_cmds.set_ha_node_maintenance("pve1", bad))
        assert r["status"] == "ERROR" and "enable" in r["message"], (bad, r)
    assert calls == []


def test_unreadable_membership_is_not_called_unknown_node(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run(
        {"/cluster/status": (1, ""), "/nodes": (1, "")}, calls))
    r = asyncio.run(pve_cmds.set_ha_node_maintenance("pve1", True))
    assert r["status"] == "ERROR" and "could not read" in r["message"], r
    assert not [c for c in calls if c[0] == "ha-manager"]


def test_exit_on_this_host_during_os_update_drain_restarts_guests(monkeypatch, tmp_path):
    state = tmp_path / "pve_maintenance.json"
    state.write_text(json.dumps({"shutdown": [{"vmid": 101, "kind": "qemu"}]}))
    monkeypatch.setattr(pve_cmds, "_MAINT_STATE", state)
    # HA status unreadable: the guests must still be started again.
    monkeypatch.setattr(pve_cmds, "_run", _fake_run(
        {"/cluster/status": (0, CLUSTER), "/cluster/ha/status/manager_status": (1, "")}, []))

    async def _local():
        return "pve1"
    seen = []

    async def _full(action):
        seen.append(action)
        return {"status": "SUCCESS", "node": "pve1", "maintenance": False, "restarted": [101]}
    monkeypatch.setattr(pve_cmds, "local_node_name", _local)
    monkeypatch.setattr(pve_cmds, "node_maintenance", _full)
    r = asyncio.run(pve_cmds.set_ha_node_maintenance("pve1", False))
    assert seen == ["disable"] and r["status"] == "SUCCESS" and "1 guest" in r["message"], r
    # A remote node's exit stays a plain HA flag flip.
    asyncio.run(pve_cmds.set_ha_node_maintenance("pve2", False))
    assert seen == ["disable"]


def test_inactive_ha_is_an_error_not_a_queued_command(monkeypatch):
    calls = []
    monkeypatch.setattr(pve_cmds, "_run", _fake_run(
        {"/cluster/status": (0, CLUSTER), "/cluster/ha/status/manager_status": (0, {})}, calls))
    r = asyncio.run(pve_cmds.set_ha_node_maintenance("pve1", True))
    assert r["status"] == "ERROR" and "HA is not active" in r["message"], r
    assert not [c for c in calls if c[0] == "ha-manager"]


def test_unreadable_drain_record_still_counts_as_pending(monkeypatch, tmp_path):
    state = tmp_path / "pve_maintenance.json"
    state.write_text("{not json")
    monkeypatch.setattr(pve_cmds, "_MAINT_STATE", state)
    assert asyncio.run(pve_cmds._os_update_drain_pending()) is True
    monkeypatch.setattr(pve_cmds, "_MAINT_STATE", tmp_path / "missing.json")
    assert asyncio.run(pve_cmds._os_update_drain_pending()) is False
