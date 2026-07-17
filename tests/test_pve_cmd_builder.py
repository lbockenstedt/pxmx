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
    assert pve_cmd_builder.list_pools_cmd() == "pvesh get /pools"


def test_pvesh_get_quotes_path():
    # Node/storage names are safe but the path is shell-quoted (runs via bash -lc).
    assert pve_cmd_builder.pvesh_get("/nodes/edge01/storage") == \
        "pvesh get /nodes/edge01/storage"
    # A path with a shell metachar is quoted so it can't break the command.
    assert pve_cmd_builder.pvesh_get("/pools/a b") == "pvesh get '/pools/a b'"


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
        assert c["data"]["command"] == "pvesh get /pools"
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