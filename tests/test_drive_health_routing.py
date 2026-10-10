"""PXMX_DRIVE_HEALTH routing across one-agent-per-server clusters.

Live shape: three Proxmox servers (mipbe-ssplm-pxmx01/02/03), each running an
agent, all behind ONE pxmx spoke. smartctl only sees local drives, and every
agent lists every cluster member in ``nodes``. The unscoped request used to go
to whichever agent was first in ``connected_agents`` (one server, rotating as
agents reconnected), and a named node resolved to that same first agent.
"""
import os
import sys
from pathlib import Path

_PXMX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PXMX / "src"))
sys.path.insert(0, os.environ.get("LM_CORE_SRC") or str(_PXMX.parent / "lm" / "core" / "src"))

import asyncio  # noqa: E402

from proxmox_spoke import ProxmoxSpoke  # noqa: E402

_MEMBERS = [{"node": "mipbe-ssplm-pxmx01"}, {"node": "mipbe-ssplm-pxmx02"},
            {"node": "mipbe-ssplm-pxmx03"}]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeCP:
    def __init__(self, agents, fail=()):
        self.connected_agents = agents
        self.fail = set(fail)
        self.calls = []

    async def send_to_agent(self, cmd, data, agent_id=None, timeout=15.0):
        self.calls.append((cmd, agent_id))
        if agent_id in self.fail:
            raise RuntimeError("agent unreachable")
        host = self.connected_agents[agent_id]["hostname"].split(".")[0]
        return {"payload": {"data": {"status": "SUCCESS", "node": host,
                                     "cluster": "agent-said",
                                     "drives": [{"device": "/dev/sda"}]}}}


def _cluster(**kw):
    return _FakeCP({
        "a1": {"hostname": "mipbe-ssplm-pxmx01", "cluster_name": "ssplm", "nodes": _MEMBERS},
        "a2": {"hostname": "MIPBE-SSPLM-PXMX02.lab.local", "cluster_name": "ssplm", "nodes": _MEMBERS},
        "a3": {"hostname": "mipbe-ssplm-pxmx03", "cluster_name": "ssplm", "nodes": _MEMBERS},
    }, **kw)


def test_unscoped_request_fans_out_to_every_agent():
    cp = _cluster()
    res = _run(ProxmoxSpoke("px-1", {}, control_plane=cp).handle_command("PXMX_DRIVE_HEALTH", {}))
    assert res["status"] == "SUCCESS"
    assert sorted(a for _, a in cp.calls) == ["a1", "a2", "a3"]
    assert sorted(n["node"].lower() for n in res["nodes"]) == [
        "mipbe-ssplm-pxmx01", "mipbe-ssplm-pxmx02", "mipbe-ssplm-pxmx03"]
    assert all(n["cluster"] == "ssplm" for n in res["nodes"])


def test_unscoped_one_agent_down_is_reported_not_fatal():
    cp = _cluster(fail={"a3"})
    res = _run(ProxmoxSpoke("px-1", {}, control_plane=cp).handle_command("PXMX_DRIVE_HEALTH", {}))
    by_node = {n["node"]: n for n in res["nodes"]}
    assert by_node["mipbe-ssplm-pxmx03"]["status"] == "ERROR"
    assert by_node["mipbe-ssplm-pxmx03"]["drives"] == []
    assert by_node["mipbe-ssplm-pxmx01"]["status"] == "SUCCESS"


def test_unscoped_no_agents_errors():
    res = _run(ProxmoxSpoke("px-1", {}, control_plane=_FakeCP({})).handle_command(
        "PXMX_DRIVE_HEALTH", {}))
    assert res["status"] == "ERROR"


def test_named_node_routes_to_the_agent_on_that_host():
    for node, want in (("mipbe-ssplm-pxmx01", "a1"), ("mipbe-ssplm-pxmx02", "a2"),
                       ("MIPBE-SSPLM-PXMX03", "a3")):
        cp = _cluster()
        res = _run(ProxmoxSpoke("px-1", {}, control_plane=cp).handle_command(
            "PXMX_DRIVE_HEALTH", {"node": node}))
        assert cp.calls == [("PXMX_DRIVE_HEALTH", want)], node
        assert res["node"].lower() == node.lower()


def test_agent_for_node_falls_back_to_membership_then_first():
    cp = _FakeCP({"a-edge": {"hostname": "edge-host", "nodes": [{"node": "edge01"}]},
                  "a-other": {"hostname": "other-host", "nodes": [{"node": "other02"}]}})
    sp = ProxmoxSpoke("px-1", {}, control_plane=cp)
    assert sp._agent_for_node("other02") == "a-other"
    assert sp._agent_for_node("ghost") == "a-edge"
