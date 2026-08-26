"""The cs telemetry ``node`` summary ships the local node's image-capable
``storages`` so the hub's fleet template-refresh modal can offer a "Destination
storage" dropdown from cached telemetry (no live per-open round-trip, which
fails when there is no dedicated hypervisor spoke).

Covers the 120s TTL memo behaviour of ``Agent._local_node_storages`` (a
re-implementation exercised directly, same approach as test_telemetry_node_scope)
AND guards that the shipped ``_cs_telemetry_body`` still wires ``storages`` from
``self._last_storages`` — so the local re-impl can't pass against nothing.
"""
import asyncio
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"


class _Fake:
    """Minimal stand-in carrying the attrs ``_local_node_storages`` reads."""
    def __init__(self, storages, ttl=120.0):
        self._cs_storages_cache = None
        self._cs_storages_ts = 0.0
        self._cs_storages_ttl = ttl
        self.hostname = "svr-01"
        self._local_node = ""
        self._storages = storages
        self.calls = 0

    async def list_node_storages(self, node, content_filter="images"):
        self.calls += 1
        self.node_seen = node
        self.content_seen = content_filter
        return list(self._storages)


# Bind the shipped coroutine to the fake so we test the REAL logic.
def _load_method():
    import importlib.util
    src = (SRC / "agent.py").read_text()
    m = re.search(r"(    async def _local_node_storages\(self\).*?\n)(?=    async def |    def )",
                  src, re.S)
    assert m, "could not locate _local_node_storages in agent.py"
    body = "import time\n" + "\n".join(
        line[4:] if line.startswith("    ") else line
        for line in m.group(1).splitlines())
    ns = {}
    exec(compile(body, "<agent-extract>", "exec"), ns)
    return ns["_local_node_storages"]


_local_node_storages = _load_method()


def _run(fake):
    return asyncio.get_event_loop().run_until_complete(_local_node_storages(fake))


def test_returns_local_node_storages():
    fake = _Fake([{"storage": "local-lvm", "type": "lvmthin"}])
    out = _run(fake)
    assert out == [{"storage": "local-lvm", "type": "lvmthin"}]
    assert fake.node_seen == "svr-01"          # falls back to hostname
    assert fake.content_seen == "images"


def test_ttl_memo_avoids_a_second_fetch():
    fake = _Fake([{"storage": "a", "type": "dir"}])
    _run(fake)
    _run(fake)
    assert fake.calls == 1                      # second call served from memo


def test_ttl_expiry_refetches():
    fake = _Fake([{"storage": "a", "type": "dir"}], ttl=0.0)
    _run(fake)
    _run(fake)
    assert fake.calls == 2                      # 0s TTL always re-fetches


# ── the shipped body really carries the field ────────────────────────────────

def test_cs_telemetry_body_ships_storages():
    src = (SRC / "agent.py").read_text()
    body = src[src.index("def _cs_telemetry_body("):]
    body = body[:body.index("\n    def ", 1)] if "\n    def " in body[1:] else body
    assert '"storages"' in body, "node summary no longer ships storages"
    assert "_last_storages" in body, "storages no longer sourced from _last_storages"
