"""Tests for ProxmoxSpoke.handle_command INSTALL_CERT routing.

The pxmx spoke never touches Proxmox directly — it forwards INSTALL_CERT to the
per-node agent that owns the target node (resolved from `identifier`/`node` via
_agent_for_node), and the agent runs `pvenode cert set` on its local pveproxy.
These tests verify the spoke resolves the right agent and relays the hub's
payload (fullchain/privkey/identifier) unchanged, surfacing the agent's result.

Self-contained: puts lm/core/src (for base_spoke) + pxmx/src on sys.path and
imports the flat ``proxmox_spoke`` module the spoke uses itself.
"""
import os
import sys
from pathlib import Path

_PXMX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PXMX / "src"))
sys.path.insert(0, str(Path("/Users/lbockenstedt/vscode/lm/core/src")))

import asyncio  # noqa: E402

from proxmox_spoke import ProxmoxSpoke  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeCP:
    def __init__(self, agents, response):
        self.connected_agents = agents
        self._response = response
        self.last = None

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=15.0):
        self.last = {"cmd": cmd, "agent_id": agent_id, "data": dict(data)}
        return self._response


def test_install_cert_routes_to_the_node_owner_agent():
    cp = _FakeCP(
        {"a-edge": {"nodes": ["edge01"], "cluster_name": "c"},
         "a-other": {"nodes": ["other02"], "cluster_name": "c"}},
        {"payload": {"data": {"status": "SUCCESS",
                              "message": "cert installed on edge01"}}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("INSTALL_CERT", {
        "domain": "example.com", "fullchain": "FC", "privkey": "PK",
        "identifier": "edge01"}))
    assert res["status"] == "SUCCESS"
    assert cp.last["cmd"] == "INSTALL_CERT"
    # Routed to the agent whose `nodes` contains "edge01", not the first/other.
    assert cp.last["agent_id"] == "a-edge"
    # The hub's payload is relayed to the agent unchanged.
    assert cp.last["data"]["fullchain"] == "FC"
    assert cp.last["data"]["privkey"] == "PK"
    assert cp.last["data"]["identifier"] == "edge01"


def test_install_cert_uses_explicit_agent_id_when_given():
    cp = _FakeCP({"a-edge": {"nodes": ["edge01"]}},
                 {"payload": {"data": {"status": "SUCCESS", "message": "ok"}}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    _run(sp.handle_command("INSTALL_CERT", {
        "agent_id": "a-edge", "fullchain": "FC", "privkey": "PK"}))
    assert cp.last["agent_id"] == "a-edge"


def test_install_cert_no_connected_agent_errors():
    cp = _FakeCP({}, None)
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("INSTALL_CERT", {"identifier": "edge01"}))
    assert res["status"] == "ERROR"
    assert "No agent" in res["message"]
    assert cp.last is None  # never attempted the relay


def test_install_cert_non_dict_agent_result_becomes_error():
    cp = _FakeCP({"a-edge": {"nodes": ["edge01"]}}, None)
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    res = _run(sp.handle_command("INSTALL_CERT", {"identifier": "edge01"}))
    assert res["status"] == "ERROR"
    assert "no result" in res["message"]