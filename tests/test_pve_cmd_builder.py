"""Tests for the spoke-side Proxmox command builder (agent-rework #4).

The spoke now constructs pvesh/qm/pct command STRINGS and sends them to the dumb
Agent as RUN_COMMAND; the Agent just runs them + returns {ok,rc,stdout,...}.
These tests pin (1) the command strings the spoke builds (golden compare vs the
pvesh path the Agent used to run locally) and (2) the spoke's parsing of the
Agent's RUN_COMMAND response into the shape the spoke's aggregator expects.

Self-contained: puts pxmx/src on sys.path and imports the flat ``pve_cmd_builder``
+ ``proxmox_spoke`` modules the spoke uses itself. Runs on Python 3.9 (no
core.src.simulations.routes import).
"""
import os
import sys
from pathlib import Path

_PXMX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PXMX / "src"))
sys.path.insert(0, str(Path("/Users/lbockenstedt/vscode/lm/core/src")))

import asyncio  # noqa: E402

import pve_cmd_builder  # noqa: E402
from proxmox_spoke import ProxmoxSpoke  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _runner(stdout="", rc=0, stderr="", ok=True, error=""):
    """The exact shape run_local_command returns (the AGENT_RESPONSE data the
    spoke's send_to_agent future resolves to for a RUN_COMMAND)."""
    return {"ok": ok, "rc": rc, "stdout": stdout, "stderr": stderr,
            "truncated": False, "error": error, "mode": "shell"}


# ── command-string construction (golden: what the Agent used to run) ──────────

def test_list_pools_cmd_is_pvesh_get_pools():
    # The Agent's list_pools did `_pvesh("/pools")` → `pvesh get /pools`.
    assert pve_cmd_builder.list_pools_cmd() == "pvesh get /pools --output-format json"


def test_pvesh_get_quotes_path():
    # Node/storage names are safe but the path is shell-quoted (runs via bash -lc).
    assert pve_cmd_builder.pvesh_get("/nodes/edge01/storage") == \
        "pvesh get /nodes/edge01/storage --output-format json"
    # A path with a shell metachar is quoted so it can't break the command.
    assert pve_cmd_builder.pvesh_get("/pools/a b") == "pvesh get '/pools/a b' --output-format json"


# ── result parsing ────────────────────────────────────────────────────────────

def test_parse_pools_extracts_poolid_and_comment():
    r = _runner(stdout='[{"poolid":"dev","comment":"dev pool"},'
                       ' {"poolid":"prod","comment":""}]')
    pools = pve_cmd_builder.parse_pools(r)
    assert pools == [{"poolid": "dev", "comment": "dev pool"},
                     {"poolid": "prod", "comment": ""}]


def test_parse_pools_skips_entries_without_poolid():
    r = _runner(stdout='[{"poolid":"ok"}, {"comment":"no id"}, "garbage"]')
    assert pve_cmd_builder.parse_pools(r) == [{"poolid": "ok", "comment": ""}]


def test_parse_pools_empty_on_run_failure():
    # rc!=0 (pvesh error) → empty list, never raises (read-only, non-fatal).
    assert pve_cmd_builder.parse_pools(_runner(rc=1, stderr="no access")) == []
    assert pve_cmd_builder.parse_pools(_runner(ok=False, error="binary not found")) == []
    assert pve_cmd_builder.parse_pools(_runner(stdout="not json")) == []
    assert pve_cmd_builder.parse_pools(_runner(stdout="")) == []
    # An object (not a list) → empty.
    assert pve_cmd_builder.parse_pools(_runner(stdout='{"x":1}')) == []


def test_parse_pools_tolerates_typed_envelope():
    # A spurious payload.data envelope (the typed-command shape) is unwrapped.
    r = {"payload": {"data": _runner(stdout='[{"poolid":"p"}]')}}
    assert pve_cmd_builder.parse_pools(r) == [{"poolid": "p", "comment": ""}]


# ── spoke aggregator: PXMX_LIST_POOLS over RUN_COMMAND ────────────────────────

class _FakeCP:
    """Records RUN_COMMAND calls per agent + returns a configured runner dict."""
    def __init__(self, agents, responses):
        self.connected_agents = agents
        self._responses = responses  # agent_id → runner dict (or None for error)
        self.calls = []

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=15.0):
        self.calls.append({"cmd": cmd, "data": dict(data), "agent_id": agent_id})
        resp = self._responses.get(agent_id, _runner())
        if resp is None:
            raise RuntimeError("agent unreachable")
        return resp


def test_list_pools_uses_run_command_and_aggregates_with_cluster():
    cp = _FakeCP(
        {"a-edge": {"cluster_name": "edge-cluster"},
         "a-prod": {"cluster_name": "prod-cluster"}},
        {"a-edge": _runner(stdout='[{"poolid":"dev","comment":"d"}]'),
         "a-prod": _runner(stdout='[{"poolid":"prod"}]')})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_POOLS", {}))
    assert res["status"] == "SUCCESS"
    # Both agents got RUN_COMMAND (not the typed PXMX_LIST_POOLS) with the
    # golden command + allow_shell.
    assert [c["cmd"] for c in cp.calls] == ["RUN_COMMAND", "RUN_COMMAND"]
    for c in cp.calls:
        assert c["data"]["command"] == "pvesh get /pools --output-format json"
        assert c["data"]["allow_shell"] is True
    # Aggregated, each pool tagged with its cluster.
    pools = sorted(res["pools"], key=lambda p: p["poolid"])
    assert pools == [
        {"poolid": "dev", "comment": "d", "cluster": "edge-cluster"},
        {"poolid": "prod", "comment": "", "cluster": "prod-cluster"}]


def test_list_pools_agent_failure_is_skipped_not_fatal():
    cp = _FakeCP(
        {"a-ok": {"cluster_name": "c"}, "a-bad": {"cluster_name": "c"}},
        {"a-ok": _runner(stdout='[{"poolid":"p"}]'),
         "a-bad": None})  # raises → caught, skipped
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_POOLS", {}))
    assert res["status"] == "SUCCESS"
    assert [p["poolid"] for p in res["pools"]] == ["p"]


def test_list_pools_pvesh_error_yields_empty_for_that_agent():
    # rc!=0 from pvesh on one agent → empty list from it; the other still works.
    cp = _FakeCP(
        {"a-ok": {"cluster_name": "c"}, "a-err": {"cluster_name": "c"}},
        {"a-ok": _runner(stdout='[{"poolid":"p"}]'),
         "a-err": _runner(rc=1, stderr="permission denied")})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_POOLS", {}))
    assert res["status"] == "SUCCESS"
    assert [p["poolid"] for p in res["pools"]] == ["p"]


def test_list_pools_no_agents_returns_empty_success():
    sp = ProxmoxSpoke("px-1", {}, control_plane=_FakeCP({}, {}))
    res = _run(sp.handle_command("PXMX_LIST_POOLS", {}))
    assert res == {"status": "SUCCESS", "pools": []}


# ── PXMX_LIST_STORAGES (single-shot, node-scoped) ─────────────────────────────

def test_list_storages_cmd_is_pvesh_get_node_storage():
    assert pve_cmd_builder.list_storages_cmd("edge01") == "pvesh get /nodes/edge01/storage --output-format json"


def test_parse_storages_filters_by_content_and_shapes():
    r = _runner(stdout=(
        '[{"storage":"local","content":"iso,images","type":"dir","avail":100,'
        '"total":500,"shared":0},'
        '{"storage":"iso-only","content":"iso","type":"dir","avail":50,'
        '"total":200,"shared":1},'
        '{"storage":"local-lvm","content":"images","type":"lvm","avail":300,'
        '"total":800,"shared":0}]'))
    storages = pve_cmd_builder.parse_storages(r, "images")
    assert len(storages) == 2  # iso-only excluded (no images content)
    by = {s["storage"]: s for s in storages}
    assert by["local"]["type"] == "dir" and by["local"]["shared"] is False
    assert by["local-lvm"]["avail"] == 300 and by["local-lvm"]["total"] == 800


def test_parse_storages_empty_on_run_failure():
    assert pve_cmd_builder.parse_storages(_runner(rc=1, stderr="x")) == []
    assert pve_cmd_builder.parse_storages(_runner(ok=False, error="nofile")) == []
    assert pve_cmd_builder.parse_storages(_runner(stdout="not json")) == []


def test_list_storages_uses_run_command_and_returns_cluster():
    cp = _FakeCP(
        {"a-edge": {"cluster_name": "edge-cluster", "nodes": ["edge01"]}},
        {"a-edge": _runner(stdout=(
            '[{"storage":"local","content":"images","type":"dir","avail":1,'
            '"total":2,"shared":0}]'))})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_STORAGES", {"node": "edge01"}))
    assert res["status"] == "SUCCESS"
    assert res["node"] == "edge01"
    assert res["cluster"] == "edge-cluster"
    assert res["storages"] == [{"storage": "local", "type": "dir", "avail": 1,
                                "total": 2, "shared": False}]
    assert cp.calls[0]["cmd"] == "RUN_COMMAND"
    assert cp.calls[0]["data"]["command"] == "pvesh get /nodes/edge01/storage --output-format json"
    assert cp.calls[0]["data"]["allow_shell"] is True


def test_list_storages_resolves_agent_from_node_and_uses_content_filter():
    # No explicit agent_id → resolved via _agent_for_node (nodes list match).
    cp = _FakeCP(
        {"a-edge": {"cluster_name": "c", "nodes": ["edge01"]},
         "a-other": {"cluster_name": "c", "nodes": ["other02"]}},
        {"a-edge": _runner(stdout=(
            '[{"storage":"s","content":"images,iso","type":"dir","avail":1,'
            '"total":2,"shared":0}]'))})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_STORAGES",
                                 {"node": "edge01", "content": "iso"}))
    assert res["status"] == "SUCCESS"
    assert cp.calls[0]["agent_id"] == "a-edge"
    assert res["storages"] == [{"storage": "s", "type": "dir", "avail": 1,
                                "total": 2, "shared": False}]


def test_list_storages_no_agent_resolved_errors():
    sp = ProxmoxSpoke("px-1", {}, control_plane=_FakeCP({}, {}))
    res = _run(sp.handle_command("PXMX_LIST_STORAGES", {"node": "ghost"}))
    assert res["status"] == "ERROR" and "No agent resolved" in res["message"]


def test_list_storages_agent_failure_returns_empty_success():
    cp = _FakeCP({"a": {"cluster_name": "c", "nodes": ["n1"]}},
                 {"a": None})  # raises
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_STORAGES", {"node": "n1"}))
    assert res == {"status": "SUCCESS", "storages": [], "node": "n1", "cluster": "c"}


# ── PXMX_LIST_ISOS (multi-round-trip) ─────────────────────────────────────────

def test_list_iso_content_cmd():
    assert pve_cmd_builder.list_iso_content_cmd("edge01", "local") == \
        "pvesh get /nodes/edge01/storage/local/content --output-format json"


def test_storage_names_for_content_picks_iso_storages():
    r = _runner(stdout=(
        '[{"storage":"local","content":"iso,images"},'
        '{"storage":"iso-pool","content":"iso"},'
        '{"storage":"vztmpl","content":"vztmpl"}]'))
    assert pve_cmd_builder.storage_names_for_content(r, "iso") == ["local", "iso-pool"]


def test_parse_iso_items_keeps_iso_only_and_stamps_storage():
    r = _runner(stdout=(
        '[{"volid":"local:iso/ubuntu-22.04.iso","size":12345},'
        '{"volid":"local:backup/foo.tar.zst","size":999},'
        '{"volid":"local:iso/debian-12.iso","size":222}]'))
    items = pve_cmd_builder.parse_iso_items(r, "local")
    assert len(items) == 2  # the .tar.zst backup is dropped
    assert items[0] == {"volid": "local:iso/ubuntu-22.04.iso",
                       "name": "ubuntu-22.04.iso", "storage": "local", "size": 12345}
    assert items[1]["name"] == "debian-12.iso"


class _FakeCPRoundRobin:
    """Returns a sequence of responses per agent_id, in order (for multi-trip)."""
    def __init__(self, agents, responses):
        self.connected_agents = agents
        self._responses = responses  # agent_id → list of runner dicts (consumed)
        self.calls = []

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=15.0):
        self.calls.append({"cmd": cmd, "data": dict(data), "agent_id": agent_id})
        seq = self._responses.get(agent_id, [])
        resp = seq[len([c for c in self.calls if c["agent_id"] == agent_id]) - 1] \
            if seq else _runner()
        return resp


def test_list_isos_two_round_trips_storage_then_content():
    storage_list = _runner(stdout=(
        '[{"storage":"local","content":"iso,images"},'
        '{"storage":"iso-pool","content":"iso"},'
        '{"storage":"vztmpl","content":"vztmpl"}]'))
    local_content = _runner(stdout=(
        '[{"volid":"local:iso/ubuntu.iso","size":100}]'))
    pool_content = _runner(stdout=(
        '[{"volid":"iso-pool:iso/debian.iso","size":200},'
        '{"volid":"iso-pool:backup/x.tar.zst","size":9}]'))
    cp = _FakeCPRoundRobin(
        {"a-edge": {"cluster_name": "c", "nodes": ["edge01"]}},
        {"a-edge": [storage_list, local_content, pool_content]})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_ISOS", {"node": "edge01"}))
    assert res["status"] == "SUCCESS"
    assert res["cluster"] == "c" and res["node"] == "edge01"
    # 3 RUN_COMMANDs: storage list + one content fetch per iso storage (vztmpl skipped).
    assert len(cp.calls) == 3
    assert all(c["cmd"] == "RUN_COMMAND" for c in cp.calls)
    assert cp.calls[0]["data"]["command"] == "pvesh get /nodes/edge01/storage --output-format json"
    assert cp.calls[1]["data"]["command"] == "pvesh get /nodes/edge01/storage/local/content --output-format json"
    assert cp.calls[2]["data"]["command"] == "pvesh get /nodes/edge01/storage/iso-pool/content --output-format json"
    volids = sorted(i["volid"] for i in res["isos"])
    assert volids == ["iso-pool:iso/debian.iso", "local:iso/ubuntu.iso"]


def test_list_isos_no_iso_storages_single_trip_empty():
    cp = _FakeCPRoundRobin(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": [_runner(stdout='[{"storage":"vztmpl","content":"vztmpl"}]')]})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_ISOS", {"node": "n1"}))
    assert res == {"status": "SUCCESS", "isos": [], "node": "n1", "cluster": "c"}
    assert len(cp.calls) == 1  # only the storage-list trip


def test_list_isos_one_storage_failure_doesnt_sink_others():
    # storage-list OK; local content raises; iso-pool content OK → only pool's ISO.
    storage_list = _runner(stdout=(
        '[{"storage":"local","content":"iso"},'
        '{"storage":"iso-pool","content":"iso"}]'))
    cp = _FakeCPRoundRobin(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": [storage_list, None, _runner(
            stdout='[{"volid":"iso-pool:iso/d.iso","size":1}]')]})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_ISOS", {"node": "n1"}))
    assert res["status"] == "SUCCESS"
    assert [i["volid"] for i in res["isos"]] == ["iso-pool:iso/d.iso"]


def test_list_isos_storage_list_failure_empty_success():
    cp = _FakeCP({"a": {"cluster_name": "c", "nodes": ["n1"]}}, {"a": None})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_ISOS", {"node": "n1"}))
    assert res == {"status": "SUCCESS", "isos": [], "node": "n1", "cluster": "c"}


def test_list_isos_no_agent_resolved_errors():
    sp = ProxmoxSpoke("px-1", {}, control_plane=_FakeCP({}, {}))
    res = _run(sp.handle_command("PXMX_LIST_ISOS", {"node": "ghost"}))
    assert res["status"] == "ERROR" and "No agent resolved" in res["message"]


# ── GET_NODE_STATS (multi-round-trip) ─────────────────────────────────────────

def test_cluster_resources_cmd_is_pvesh_get_cluster_resources():
    assert pve_cmd_builder.cluster_resources_cmd() == "pvesh get /cluster/resources --output-format json"


def test_nodes_list_cmd_is_pvesh_get_nodes():
    assert pve_cmd_builder.nodes_list_cmd() == "pvesh get /nodes --output-format json"


def test_node_status_cmd_is_pvesh_get_node_status():
    assert pve_cmd_builder.node_status_cmd("edge01") == "pvesh get /nodes/edge01/status --output-format json"


def test_parse_cluster_resource_nodes_filters_type_node_and_shapes():
    r = _runner(stdout=(
        '[{"type":"node","node":"edge01","status":"online","cpu":0.42,"maxcpu":8,'
        '"mem":4000,"maxmem":8000,"uptime":1234},'
        '{"type":"storage","storage":"local"},'
        '{"type":"vm","vmid":100,"node":"edge01"}]'))
    nodes = pve_cmd_builder.parse_cluster_resource_nodes(r, "edge-cluster")
    assert len(nodes) == 1
    n = nodes[0]
    assert n == {
        "cluster": "edge-cluster", "node": "edge01", "status": "online",
        "cpu_usage": 42.0, "cpu_cores": 8,
        "mem_used": 4000, "mem_total": 8000, "mem_pct": 50.0,
        "uptime": 1234, "proxmox_version": ""}


def test_parse_cluster_resource_nodes_empty_on_failure():
    assert pve_cmd_builder.parse_cluster_resource_nodes(_runner(rc=1, stderr="x"), "c") == []
    assert pve_cmd_builder.parse_cluster_resource_nodes(_runner(stdout="not json"), "c") == []
    assert pve_cmd_builder.parse_cluster_resource_nodes(_runner(stdout='{"x":1}'), "c") == []


def test_parse_pveversion_from_node_status():
    r = _runner(stdout='{"pveversion":"pve-manager/8.2/abc","memory":{"used":1}}')
    assert pve_cmd_builder.parse_pveversion(r) == "pve-manager/8.2/abc"
    assert pve_cmd_builder.parse_pveversion(_runner(rc=1)) == ""
    assert pve_cmd_builder.parse_pveversion(_runner(stdout="not json")) == ""


def test_parse_nodes_list_entries_minimal():
    r = _runner(stdout=(
        '[{"node":"edge01","status":"online","maxcpu":8,"mem":1,"maxmem":2,'
        '"uptime":7}, {"nope":1}]'))
    out = pve_cmd_builder.parse_nodes_list_entries(r)
    assert out == [{"node": "edge01", "status": "online", "maxcpu": 8,
                    "mem": 1, "maxmem": 2, "uptime": 7}]


def test_node_from_status_merges_status_with_nrec():
    stat = _runner(stdout=(
        '{"pveversion":"pve-8.2","cpu":0.5,"cpuinfo":{"cpus":4},'
        '"memory":{"used":30,"total":100},"uptime":99}'))
    nrec = {"node": "edge01", "status": "online", "maxcpu": 8,
            "mem": 1, "maxmem": 2, "uptime": 7}
    n = pve_cmd_builder.node_from_status(stat, nrec, "c")
    assert n == {
        "cluster": "c", "node": "edge01", "status": "online",
        "cpu_usage": 50.0, "cpu_cores": 4,
        "mem_used": 30, "mem_total": 100, "mem_pct": 30.0,
        "uptime": 99, "proxmox_version": "pve-8.2"}


def test_node_from_status_falls_back_to_nrec_on_failure():
    nrec = {"node": "edge01", "status": "online", "maxcpu": 8,
            "mem": 5, "maxmem": 10, "uptime": 7}
    n = pve_cmd_builder.node_from_status(_runner(rc=1, stderr="x"), nrec, "c")
    assert n == {
        "cluster": "c", "node": "edge01", "status": "online",
        "cpu_usage": 0.0, "cpu_cores": 8, "mem_used": 5, "mem_total": 10,
        "mem_pct": 50.0, "uptime": 7, "proxmox_version": ""}


def test_get_node_stats_primary_path_two_round_trips():
    # /cluster/resources yields 2 node rows → one first-node /status for pveversion.
    cluster_res = _runner(stdout=(
        '[{"type":"node","node":"edge01","status":"online","cpu":0.1,"maxcpu":4,'
        '"mem":100,"maxmem":200,"uptime":1},'
        '{"type":"node","node":"edge02","status":"online","cpu":0.0,"maxcpu":4,'
        '"mem":50,"maxmem":200,"uptime":2}]'))
    status = _runner(stdout='{"pveversion":"pve-8.2","memory":{}}')
    cp = _FakeCPRoundRobin(
        {"a": {"cluster_name": "edge-cluster", "nodes": ["edge01"]}},
        {"a": [cluster_res, status]})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_NODE_STATS", {"agent_id": "a"}))
    # Pinned path returns the Agent's shape verbatim ({nodes, cluster}, no status).
    assert res == {"nodes": [
        {"cluster": "edge-cluster", "node": "edge01", "status": "online",
         "cpu_usage": 10.0, "cpu_cores": 4, "mem_used": 100, "mem_total": 200,
         "mem_pct": 50.0, "uptime": 1, "proxmox_version": "pve-8.2"},
        {"cluster": "edge-cluster", "node": "edge02", "status": "online",
         "cpu_usage": 0.0, "cpu_cores": 4, "mem_used": 50, "mem_total": 200,
         "mem_pct": 25.0, "uptime": 2, "proxmox_version": "pve-8.2"}],
        "cluster": "edge-cluster"}
    assert len(cp.calls) == 2
    assert all(c["cmd"] == "RUN_COMMAND" for c in cp.calls)
    assert cp.calls[0]["data"]["command"] == "pvesh get /cluster/resources --output-format json"
    assert cp.calls[1]["data"]["command"] == "pvesh get /nodes/edge01/status --output-format json"
    assert all(c["data"]["allow_shell"] is True for c in cp.calls)


def test_get_node_stats_primary_without_pveversion_leaves_blank():
    # /status returns no pveversion → proxmox_version stays "" (best-effort).
    cluster_res = _runner(stdout=(
        '[{"type":"node","node":"n1","status":"online","cpu":0,"maxcpu":2,'
        '"mem":0,"maxmem":1,"uptime":0}]'))
    status = _runner(stdout='{"memory":{}}')  # no pveversion key
    cp = _FakeCPRoundRobin(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}}, {"a": [cluster_res, status]})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_NODE_STATS", {"agent_id": "a"}))
    assert res["nodes"][0]["proxmox_version"] == ""


def test_get_node_stats_fallback_path_nodes_then_per_node_status():
    # /cluster/resources yields 0 node rows → fallback /nodes → per-node /status.
    empty_cluster = _runner(stdout='[{"type":"storage","storage":"local"}]')
    nodes_list = _runner(stdout=(
        '[{"node":"n1","status":"online","maxcpu":4,"mem":0,"maxmem":0,"uptime":0},'
        '{"node":"n2","status":"online","maxcpu":2,"mem":0,"maxmem":0,"uptime":0}]'))
    s1 = _runner(stdout='{"pveversion":"pve-8","cpu":0.25,"cpuinfo":{"cpus":4},'
                         '"memory":{"used":10,"total":100},"uptime":5}')
    s2 = _runner(stdout='{"cpu":0.0,"cpuinfo":{"cpus":2},'
                         '"memory":{"used":0,"total":50},"uptime":6}')
    cp = _FakeCPRoundRobin(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": [empty_cluster, nodes_list, s1, s2]})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_NODE_STATS", {"agent_id": "a"}))
    by = {n["node"]: n for n in res["nodes"]}
    assert set(by) == {"n1", "n2"}
    assert by["n1"]["proxmox_version"] == "pve-8" and by["n1"]["cpu_cores"] == 4
    assert by["n2"]["proxmox_version"] == "" and by["n2"]["cpu_cores"] == 2
    # 4 round-trips: /cluster/resources → /nodes → n1/status → n2/status.
    assert len(cp.calls) == 4
    assert cp.calls[0]["data"]["command"] == "pvesh get /cluster/resources --output-format json"
    assert cp.calls[1]["data"]["command"] == "pvesh get /nodes --output-format json"
    assert cp.calls[2]["data"]["command"] == "pvesh get /nodes/n1/status --output-format json"
    assert cp.calls[3]["data"]["command"] == "pvesh get /nodes/n2/status --output-format json"


def test_get_node_stats_agent_unreachable_returns_error_shape():
    cp = _FakeCP({"a": {"cluster_name": "c", "nodes": ["n1"]}}, {"a": None})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_NODE_STATS", {"agent_id": "a"}))
    assert res == {"nodes": [], "error": "agent unreachable"}


def test_get_node_stats_aggregate_fallback_uses_run_command():
    # No agent_id, telemetry cache empty → broadcast becomes per-agent RUN_COMMAND.
    cluster_res = _runner(stdout=(
        '[{"type":"node","node":"n1","status":"online","cpu":0,"maxcpu":1,'
        '"mem":0,"maxmem":1,"uptime":0}]'))
    status = _runner(stdout='{"pveversion":"pve-8","memory":{}}')
    cp = _FakeCPRoundRobin(
        {"a": {"cluster_name": "c", "nodes": []}},  # empty telemetry nodes → fallback
        {"a": [cluster_res, status]})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_NODE_STATS", {}))
    assert res["status"] == "SUCCESS"
    assert len(res["nodes"]) == 1
    assert res["nodes"][0]["agent_id"] == "a"
    assert res["nodes"][0]["proxmox_version"] == "pve-8"
    assert cp.calls[0]["cmd"] == "RUN_COMMAND"


# ── PXMX_LIST_VMS (multi-round-trip + pool map + annotation) ──────────────────

def test_looks_like_mac():
    assert pve_cmd_builder._looks_like_mac("AA:BB:CC:DD:EE:01")
    assert pve_cmd_builder._looks_like_mac("aa-bb-cc-dd-ee-01")
    assert not pve_cmd_builder._looks_like_mac("not-a-mac")
    assert not pve_cmd_builder._looks_like_mac("")


def test_parse_pools_listing_for_members():
    r = _runner(stdout=(
        '[{"poolid":"dev","members":[{"vmid":100}]},'
        '{"poolid":"prod"}, {"nope":1}]'))
    out = pve_cmd_builder.parse_pools_listing_for_members(r)
    assert out == [{"poolid": "dev", "members": [{"vmid": 100}]},
                   {"poolid": "prod", "members": None}]


def test_pool_detail_cmd_and_members():
    assert pve_cmd_builder.pool_detail_cmd("dev") == "pvesh get /pools/dev --output-format json"
    r = _runner(stdout='{"poolid":"dev","members":[{"vmid":100},{"vmid":101}]}')
    assert [m["vmid"] for m in pve_cmd_builder.pool_detail_members(r)] == [100, 101]
    assert pve_cmd_builder.pool_detail_members(_runner(rc=1)) == []


def test_build_pool_map_inline_and_detail():
    listing = [{"poolid": "dev", "members": [{"vmid": 100}, {"vmid": 101}]},
               {"poolid": "prod", "members": None}]
    details = {"prod": [{"vmid": 200}]}
    pm = pve_cmd_builder.build_pool_map(listing, details)
    assert pm == {100: "dev", 101: "dev", 200: "prod"}


def test_parse_cluster_resource_vms_filters_and_shapes():
    r = _runner(stdout=(
        '[{"type":"qemu","node":"n1","vmid":100,"name":"web","status":"running",'
        '"cpu":0.5,"maxcpu":2,"mem":100,"maxmem":200,"uptime":1,"maxdisk":1000000000,'
        '"tags":"t1;tenant-a","template":0},'
        '{"type":"lxc","node":"n1","vmid":200,"name":"ct","status":"stopped",'
        '"cpu":0,"maxcpu":1,"mem":0,"maxmem":50,"uptime":0,"maxdisk":5000000000,'
        '"tags":"","template":1},'
        '{"type":"storage","storage":"local"},'
        '{"type":"qemu","node":"n1"}]'))  # no vmid → skipped
    vms = pve_cmd_builder.parse_cluster_resource_vms(r, "c", {100: "dev"})
    assert len(vms) == 2
    by = {v["vmid"]: v for v in vms}
    assert by[100] == {
        "unique_id": "c/n1/100", "cluster": "c", "node": "n1", "vmid": 100,
        "type": "qemu", "name": "web", "status": "running", "template": 0,
        "cpu": 50.0, "mem_bytes": 100, "uptime": 1, "vcpus": 2, "disk_gb": 1.0,
        "pool": "dev", "tags": ["t1", "tenant-a"], "interfaces": [], "ips": []}
    assert by[200]["pool"] == ""  # not in pool map
    assert by[200]["template"] == 1 and by[200]["disk_gb"] == 5.0
    assert by[200]["tags"] == []


def test_node_qemu_lxc_cmds_and_node_names():
    assert pve_cmd_builder.node_qemu_cmd("n1") == "pvesh get /nodes/n1/qemu --output-format json"
    assert pve_cmd_builder.node_lxc_cmd("n1") == "pvesh get /nodes/n1/lxc --output-format json"
    r = _runner(stdout='[{"node":"n1"},{"node":"n2"},{"nope":1}]')
    assert pve_cmd_builder.node_names(r) == ["n1", "n2"]


def test_vm_guest_ifaces_and_config_cmds():
    assert pve_cmd_builder.vm_guest_ifaces_cmd("n1", 100, "qemu") == \
        "pvesh get /nodes/n1/qemu/100/agent/network-get-interfaces --output-format json"
    assert pve_cmd_builder.vm_guest_ifaces_cmd("n1", 200, "lxc") == \
        "pvesh get /nodes/n1/lxc/200/interfaces --output-format json"
    assert pve_cmd_builder.vm_config_cmd("n1", 100, "qemu") == \
        "pvesh get /nodes/n1/qemu/100/config --output-format json"
    assert pve_cmd_builder.vm_config_cmd("n1", 200, "lxc") == \
        "pvesh get /nodes/n1/lxc/200/config --output-format json"


def test_parse_guest_ifaces_qga_unwrap_and_filter():
    # QGA wrapped in {"result":[...]}: eth0 kept, lo + zero-MAC skipped, ipv6 skipped.
    r = _runner(stdout=(
        '{"result":[{"name":"eth0","hardware-address":"AA:BB:CC:DD:EE:01",'
        '"ip-addresses":[{"ip-address":"10.0.0.5","ip-address-type":"ipv4"},'
        '{"ip-address":"::1","ip-address-type":"ipv6"}]},'
        '{"name":"lo","hardware-address":"00:00:00:00:00:00"}]}'))
    out = pve_cmd_builder.parse_guest_ifaces(r)
    assert out == [{"name": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ips": ["10.0.0.5"]}]


def test_parse_guest_ifaces_lxc_inet():
    r = _runner(stdout=(
        '{"result":[{"name":"eth0","hwaddr":"BB:CC:DD:EE:FF:00","inet":"10.0.0.9/24"}]}'))
    out = pve_cmd_builder.parse_guest_ifaces(r)
    assert out == [{"name": "eth0", "mac": "bb:cc:dd:ee:ff:00", "ips": ["10.0.0.9"]}]


def test_parse_guest_ifaces_empty_on_failure():
    assert pve_cmd_builder.parse_guest_ifaces(_runner(rc=1)) == []
    assert pve_cmd_builder.parse_guest_ifaces(_runner(stdout="not json")) == []
    assert pve_cmd_builder.parse_guest_ifaces(_runner(stdout='{"result":"x"}')) == []


def test_parse_config_nets_qemu_model_mac():
    r = _runner(stdout=(
        '{"data":{"net0":"virtio=AA:BB:CC:DD:EE:01,bridge=vmbr0",'
        '"net1":"e1000=11:22:33:44:55:66,bridge=vmbr1","boot":"order=net0"}}'))
    out = pve_cmd_builder.parse_config_nets(r)
    assert out == [{"name": "net0", "mac": "aa:bb:cc:dd:ee:01", "ips": []},
                   {"name": "net1", "mac": "11:22:33:44:55:66", "ips": []}]


def test_parse_config_nets_lxc_hwaddr():
    r = _runner(stdout=(
        '{"data":{"net0":"name=eth0,bridge=vmbr0,hwaddr=BB:CC:DD:EE:FF:00"}}'))
    out = pve_cmd_builder.parse_config_nets(r)
    assert out == [{"name": "eth0", "mac": "bb:cc:dd:ee:ff:00", "ips": []}]


def test_parse_config_nets_empty_on_failure():
    assert pve_cmd_builder.parse_config_nets(_runner(rc=1)) == []
    assert pve_cmd_builder.parse_config_nets(_runner(stdout="not json")) == []


class _FakeCPByCmd:
    """Returns a fixed response per (agent_id, command-string) — concurrency-safe
    for gather's interleaved annotation calls (each gets the right response
    regardless of call order). Unmapped commands → empty success (parse → [])."""
    def __init__(self, agents, responses):
        self.connected_agents = agents
        self._responses = responses  # agent_id -> {cmd: resp}
        self.calls = []

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=15.0):
        self.calls.append({"cmd": cmd, "data": dict(data), "agent_id": agent_id})
        cmdstr = data.get("command")
        return self._responses.get(agent_id, {}).get(cmdstr, _runner())


def _vms_response():
    return _runner(stdout=(
        '[{"type":"qemu","node":"n1","vmid":100,"name":"web","status":"running",'
        '"cpu":0.5,"maxcpu":2,"mem":100,"maxmem":200,"uptime":1,"maxdisk":1000000000,'
        '"tags":"t1;tenant-a","template":0},'
        '{"type":"lxc","node":"n1","vmid":200,"name":"ct","status":"stopped",'
        '"cpu":0,"maxcpu":1,"mem":0,"maxmem":50,"uptime":0,"maxdisk":5000000000,'
        '"tags":"","template":0}]'))


def test_list_vms_primary_path_pool_map_and_annotation():
    pools = _runner(stdout='[{"poolid":"dev","members":[{"vmid":100}]}]')
    qga = _runner(stdout=(
        '{"result":[{"name":"eth0","hardware-address":"AA:BB:CC:DD:EE:01",'
        '"ip-addresses":[{"ip-address":"10.0.0.5","ip-address-type":"ipv4"}]}]}'))
    ct_cfg = _runner(stdout=(
        '{"data":{"net0":"name=eth0,bridge=vmbr0,hwaddr=BB:CC:DD:EE:FF:00"}}'))
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": pools,
               "pvesh get /cluster/resources --output-format json": _vms_response(),
               "pvesh get /nodes/n1/qemu/100/agent/network-get-interfaces --output-format json": qga,
               "pvesh get /nodes/n1/lxc/200/config --output-format json": ct_cfg}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_VMS", {"agent_id": "a"}))
    assert res["status"] == "SUCCESS"
    assert res["source"] == "pinned_agent" and res["agent_count"] == 1
    by = {v["vmid"]: v for v in res["vms"]}
    # VM 100: pool stamped from inline members; running → QGA guest ifaces.
    assert by[100]["pool"] == "dev"
    assert by[100]["interfaces"] == [{"name": "eth0", "mac": "aa:bb:cc:dd:ee:01",
                                      "ips": ["10.0.0.5"]}]
    assert by[100]["ips"] == ["10.0.0.5"]
    # VM 200: not in pool map → pool=""; stopped → config MACs (no guest IPs).
    assert by[200]["pool"] == ""
    assert by[200]["interfaces"] == [{"name": "eth0", "mac": "bb:cc:dd:ee:ff:00",
                                      "ips": []}]
    assert by[200]["ips"] == []
    # Every spoke→agent call was RUN_COMMAND with allow_shell.
    assert all(c["cmd"] == "RUN_COMMAND" and c["data"]["allow_shell"] is True
               for c in cp.calls)


def test_list_vms_pool_detail_fetch_when_members_not_inline():
    pools = _runner(stdout='[{"poolid":"prod"}]')  # no inline members
    detail = _runner(stdout='{"poolid":"prod","members":[{"vmid":100}]}')
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": pools,
               "pvesh get /pools/prod --output-format json": detail,
               "pvesh get /cluster/resources --output-format json": _vms_response()}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_VMS", {"agent_id": "a"}))
    by = {v["vmid"]: v for v in res["vms"]}
    assert by[100]["pool"] == "prod"  # stamped from the per-pool detail fetch
    cmds = [c["data"]["command"] for c in cp.calls]
    assert "pvesh get /pools/prod --output-format json" in cmds


def test_list_vms_fallback_per_node_qemu_lxc():
    # /cluster/resources empty → /nodes → per-node /qemu + /lxc.
    qemu = _runner(stdout=(
        '[{"vmid":100,"node":"n1","name":"web","status":"running","cpu":0,'
        '"maxcpu":1,"mem":0,"maxmem":1,"uptime":0,"maxdisk":0,"tags":""}]'))
    lxc = _runner(stdout=(
        '[{"vmid":200,"node":"n1","name":"ct","status":"stopped","cpu":0,'
        '"maxcpu":1,"mem":0,"maxmem":1,"uptime":0,"maxdisk":0,"tags":""}]'))
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": _runner(stdout="[]"),
               "pvesh get /cluster/resources --output-format json": _runner(stdout="[]"),
               "pvesh get /nodes --output-format json": _runner(stdout='[{"node":"n1"}]'),
               "pvesh get /nodes/n1/qemu --output-format json": qemu,
               "pvesh get /nodes/n1/lxc --output-format json": lxc}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_VMS", {"agent_id": "a"}))
    assert res["status"] == "SUCCESS"
    assert {v["vmid"] for v in res["vms"]} == {100, 200}
    cmds = [c["data"]["command"] for c in cp.calls]
    assert "pvesh get /nodes --output-format json" in cmds
    assert "pvesh get /nodes/n1/qemu --output-format json" in cmds
    assert "pvesh get /nodes/n1/lxc --output-format json" in cmds


def test_list_vms_pinned_unreachable_surfaces_error():
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": {"status": "ERROR",
                                    "message": "Agent 'a' not connected"}}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_VMS", {"agent_id": "a"}))
    # Honest ERROR — not an empty "success" that masks the down agent.
    assert res["status"] == "ERROR"
    assert "not connected" in res["message"]


def test_list_vms_tag_filter_applies_on_pinned_path():
    pools = _runner(stdout="[]")
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": pools,
               "pvesh get /cluster/resources --output-format json": _vms_response()}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_VMS",
                                 {"agent_id": "a", "tag_filter": "tenant-a"}))
    assert res["status"] == "SUCCESS"
    assert [v["vmid"] for v in res["vms"]] == [100]  # VM 200 has no tags


def test_list_vms_aggregate_live_query_concurrent():
    # No agent_id, empty telemetry → live query both agents concurrently.
    pools = _runner(stdout="[]")
    cp = _FakeCPByCmd(
        {"a1": {"cluster_name": "c1", "nodes": []},
         "a2": {"cluster_name": "c2", "nodes": []}},
        {"a1": {"pvesh get /pools --output-format json": pools,
                "pvesh get /cluster/resources --output-format json": _vms_response()},
         "a2": {"pvesh get /pools --output-format json": {"status": "ERROR", "message": "down"}}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("PXMX_LIST_VMS", {}))
    assert res["status"] == "SUCCESS"
    assert res["source"] == "live_query"
    # Only a1 contributed (a2 unreachable → skipped, not fatal).
    assert {v["agent_id"] for v in res["vms"]} == {"a1"}
    assert {v["vmid"] for v in res["vms"]} == {100, 200}


# ── GET_VM_INFO (broken-path fix: agent had no handler) ───────────────────────

def test_get_vm_info_single_from_unique_id_targeted():
    pools = _runner(stdout='[{"poolid":"dev","members":[{"vmid":100}]}]')
    qga = _runner(stdout=(
        '{"result":[{"name":"eth0","hardware-address":"AA:BB:CC:DD:EE:01",'
        '"ip-addresses":[{"ip-address":"10.0.0.5","ip-address-type":"ipv4"}]}]}'))
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": pools,
               "pvesh get /cluster/resources --output-format json": _vms_response(),
               "pvesh get /nodes/n1/qemu/100/agent/network-get-interfaces --output-format json": qga}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_VM_INFO", {"vm_id": "c/n1/100"}))
    assert res["status"] == "SUCCESS"
    # Flat VM record shape (ips/tags/pool + detail) — what the hub reads.
    assert res["vmid"] == 100 and res["node"] == "n1" and res["cluster"] == "c"
    assert res["pool"] == "dev"           # stamped from the short-circuit pool lookup
    assert res["ips"] == ["10.0.0.5"]     # running → QGA guest IPs
    assert res["tags"] == ["t1", "tenant-a"]
    cmds = [c["data"]["command"] for c in cp.calls]
    # Targeted: /pools (probe) + /cluster/resources + the one VM's guest-ifaces.
    assert "pvesh get /cluster/resources --output-format json" in cmds
    assert "pvesh get /nodes/n1/qemu/100/agent/network-get-interfaces --output-format json" in cmds
    # NOT a full LIST_VMS — VM 200's config/annotation is not fetched.
    assert not any("200" in c for c in cmds)


def test_get_vm_info_pool_from_detail_when_not_inline():
    pools = _runner(stdout='[{"poolid":"prod"}]')  # no inline members
    detail = _runner(stdout='{"poolid":"prod","members":[{"vmid":100}]}')
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": pools,
               "pvesh get /pools/prod --output-format json": detail,
               "pvesh get /cluster/resources --output-format json": _vms_response()}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_VM_INFO", {"vm_id": "c/n1/100"}))
    assert res["status"] == "SUCCESS" and res["pool"] == "prod"


def test_get_vm_info_uses_vmid_and_node_when_no_unique_id():
    # pxmx_vm.py calls {vm_id: str(vmid), vmid, node} — resolve via node→agent.
    pools = _runner(stdout="[]")
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": pools,
               "pvesh get /cluster/resources --output-format json": _vms_response()}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_VM_INFO", {"vm_id": "200", "vmid": 200, "node": "n1"}))
    assert res["status"] == "SUCCESS"
    assert res["vmid"] == 200 and res["pool"] == ""  # 200 not in any pool


def test_get_vm_info_not_found_returns_error():
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": _runner(stdout="[]"),
               "pvesh get /cluster/resources --output-format json": _vms_response(),
               "pvesh get /nodes/n1/qemu --output-format json": _runner(stdout="[]"),
               "pvesh get /nodes/n1/lxc --output-format json": _runner(stdout="[]")}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_VM_INFO", {"vm_id": "c/n1/999", "node": "n1"}))
    # Fail-closed ERROR — the hub 403s on an unattributable VM, not "success".
    assert res["status"] == "ERROR" and "not found" in res["message"]


def test_get_vm_info_unreachable_agent_returns_error():
    cp = _FakeCPByCmd(
        {"a": {"cluster_name": "c", "nodes": ["n1"]}},
        {"a": {"pvesh get /pools --output-format json": {"status": "ERROR", "message": "not connected"}}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_VM_INFO", {"vm_id": "c/n1/100"}))
    assert res["status"] == "ERROR" and "not connected" in res["message"]


def test_get_vm_info_all_returns_fleet_list():
    pools = _runner(stdout="[]")
    cp = _FakeCPByCmd(
        {"a1": {"cluster_name": "c1", "nodes": []},
         "a2": {"cluster_name": "c2", "nodes": []}},
        {"a1": {"pvesh get /pools --output-format json": pools,
                "pvesh get /cluster/resources --output-format json": _vms_response()},
         "a2": {"pvesh get /pools --output-format json": {"status": "ERROR", "message": "down"}}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("GET_VM_INFO", {"vm_id": "all"}))
    assert res["status"] == "SUCCESS"
    # Only a1 contributed (a2 down → skipped). Each VM tagged with agent_id.
    assert {v["vmid"] for v in res["vms"]} == {100, 200}
    assert {v["agent_id"] for v in res["vms"]} == {"a1"}


def test_get_vm_info_no_agent_resolved_errors():
    sp = ProxmoxSpoke("px-1", {}, control_plane=_FakeCPByCmd({}, {}))
    res = _run(sp.handle_command("GET_VM_INFO", {"vm_id": "ghost/n1/100"}))
    assert res["status"] == "ERROR" and "Cannot resolve agent" in res["message"]