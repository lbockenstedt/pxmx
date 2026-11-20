"""The pxmx agent runs RUN_COMMAND / WRITE_FILE as root, so after the handshake
it must act only on frames signed with its own secret. ``_verified_frame`` (called
by the ``_connect_once`` command loop for every frame) returns None for an
unsigned frame, a frame signed with another key, or any frame while the agent
holds no secret, and the parsed body for a correctly signed frame.
"""
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"
# Synthetic package so agent.py's relative imports resolve (mirrors
# test_command_progress_keepalive.py).
_pkg = types.ModuleType("pxmx_agent_src_sig")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_sig"] = _pkg
for _name in ("security_utils", "agent"):
    _spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_sig.{_name}", SRC / f"{_name}.py",
        submodule_search_locations=[str(SRC)])
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"pxmx_agent_src_sig.{_name}"] = _mod
    _spec.loader.exec_module(_mod)

_agent_mod = sys.modules["pxmx_agent_src_sig.agent"]
_sec = sys.modules["pxmx_agent_src_sig.security_utils"]
MessageSigner, encode_frame = _sec.MessageSigner, _sec.encode_frame

_MSG = {"header": {"correlation_id": "c1"},
        "payload": {"type": "RUN_COMMAND", "data": {"command": "id"}}}


class _Stub:
    """Just the state _verified_frame reads: secret + signer."""

    def __init__(self, secret):
        """Hold a secret and a signer built from it."""
        self.secret = secret
        self.signer = MessageSigner(secret or "")

    _verified_frame = _agent_mod.ProxmoxAgent._verified_frame


@pytest.mark.parametrize("wire", [
    json.dumps(_MSG, separators=(",", ":")),
    "." + json.dumps(_MSG, separators=(",", ":")),
    encode_frame(MessageSigner("attacker-key"), _MSG),
])
def test_unsigned_or_foreign_frames_are_dropped(wire):
    assert _Stub("s3cret")._verified_frame(wire) is None


def test_frame_dropped_when_agent_has_no_secret():
    assert _Stub("")._verified_frame(encode_frame(MessageSigner(""), _MSG)) is None


def test_validly_signed_frame_is_parsed():
    a = _Stub("s3cret")
    assert a._verified_frame(encode_frame(a.signer, _MSG)) == _MSG


def test_command_loop_uses_the_verifier():
    # The loop must route every frame through _verified_frame (no inline
    # parse-then-maybe-verify path left behind).
    src = (SRC / "agent.py").read_text()
    loop = src[src.index("async for message in websocket:"):]
    loop = loop[:loop.index('payload  = msg_data.get("payload", {})')]
    assert "self._verified_frame(message)" in loop
    assert "json.loads" not in loop
