"""Agent-side keepalive emitter for slow commands.

On a big Proxmox cluster, GET_VM_LIST / GET_NODE_STATS / RUN_COMMAND can run long
enough to blow the spoke's send_to_agent timeout (and the hub's request_response,
30s) — the hub logged ``Request Timeout: [GET_NODE_STATS]``/``[PXMX_LIST_VMS]``
and VMs never populated. While one of these runs the agent now emits periodic
AGENT_PROGRESS frames (correlation_id == the command's corr_id) so the upstream
deadlines are extended instead of the request being killed.

These tests drive the real ``_emit_command_progress`` against a recording
websocket and assert: it emits correlated AGENT_PROGRESS frames on its interval,
cancelling it stops the stream, and the slow-command allowlist is exactly the
set of commands that can stall.
"""
import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

import importlib.util  # noqa: E402
import sys  # noqa: E402
import types  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"
# Synthetic package so agent.py's relative imports resolve (mirrors
# test_agent_tls_context.py). Load once; the emitter touches no instance state
# beyond agent_id + signer, so we bind it to a tiny stub.
_pkg = types.ModuleType("pxmx_agent_src_prog")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_prog"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "pxmx_agent_src_prog.agent", SRC / "agent.py",
    submodule_search_locations=[str(SRC)])
_agent_mod = importlib.util.module_from_spec(_spec)
sys.modules["pxmx_agent_src_prog.agent"] = _agent_mod
_spec.loader.exec_module(_agent_mod)

ProxmoxAgent = _agent_mod.ProxmoxAgent


class _Signer:
    """Just enough for encode_frame(signer, msg) → '<sig>.<body>'."""

    def sign_bytes(self, b):
        return "0" * 64

    def sign(self, msg):
        return "0" * 64


class _RecordWS:
    def __init__(self):
        self.frames = []

    async def send(self, wire):
        body = wire.split(".", 1)[1] if "." in wire else wire
        self.frames.append(json.loads(body))


class _Stub:
    def __init__(self):
        self.agent_id = "pxmx-test-agent"
        self.signer = _Signer()
        self._emit_command_progress = ProxmoxAgent._emit_command_progress.__get__(self)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_emitter_sends_correlated_progress_frames_on_interval():
    stub = _Stub()
    ws = _RecordWS()

    async def scenario():
        task = asyncio.create_task(
            stub._emit_command_progress(ws, "corr-xyz", "GET_VM_LIST", interval=0.05))
        await asyncio.sleep(0.17)  # allow ~3 intervals
        task.cancel()
        await asyncio.sleep(0)  # let cancellation settle
        return len(ws.frames)

    n = _run(scenario())
    assert n >= 2, f"expected multiple keepalives, got {n}"
    for f in ws.frames:
        assert f["payload"]["type"] == "AGENT_PROGRESS"
        assert f["header"]["correlation_id"] == "corr-xyz"
        assert f["payload"]["data"]["command"] == "GET_VM_LIST"


def test_cancelling_emitter_stops_the_stream():
    stub = _Stub()
    ws = _RecordWS()

    async def scenario():
        task = asyncio.create_task(
            stub._emit_command_progress(ws, "c", "RUN_COMMAND", interval=0.05))
        await asyncio.sleep(0.12)
        task.cancel()
        await asyncio.sleep(0)
        before = len(ws.frames)
        await asyncio.sleep(0.15)  # no more frames after cancel
        return before, len(ws.frames)

    before, after = _run(scenario())
    assert after == before


def test_keepalive_command_allowlist():
    """The allowlist must cover exactly the commands that can stall on a large
    backend (the ones the hub timed out on) — no more, no less."""
    assert _agent_mod._KEEPALIVE_CMDS == frozenset({
        "GET_VM_LIST", "GET_NODE_STATS", "GET_SYSTEM_STATS", "RUN_COMMAND"})
