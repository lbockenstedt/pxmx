"""vm_inventory.vm_pool_map — bounded, concurrent {vmid: poolid} reverse map.

/cluster/resources (the VM list source) carries no pool membership, so
vm_pool_map reads /pools and reverse-maps member vmid → poolid. Some PVE
versions inline members on the /pools listing; others require a per-pool
/pools/{pid} detail fetch.

Regression guard: the per-pool detail fetches used to run SERIALLY with no
aggregate deadline, so N pools whose /pools/{pid} proxies to a slow/remote node
could each ride the 15s _pvesh bound and blow the whole telemetry budget. They
now run CONCURRENTLY (cap 4) under an overall deadline, and a pool that times out
is simply absent from the map (never sinks the rest).
"""
import asyncio
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import vm_inventory  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


class _Agent:
    """Fake agent whose _pvesh serves a scripted /pools listing + per-pool
    detail responses. ``detail_delay`` (seconds) is applied to every
    /pools/{pid} detail call to simulate a slow/remote pmxcfs proxy.
    ``peak`` is the most detail calls that were in flight at once."""

    def __init__(self, listing, details=None, detail_delay=0.0):
        self._listing = listing
        self._details = details or {}
        self._detail_delay = detail_delay
        self.calls = []
        self.in_flight = 0
        self.peak = 0

    async def _pvesh(self, path):
        self.calls.append(path)
        if path == "/pools":
            return self._listing
        # /pools/{pid}
        pid = path.split("/pools/", 1)[1]
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            if self._detail_delay:
                await asyncio.sleep(self._detail_delay)
            return self._details.get(pid, {})
        finally:
            self.in_flight -= 1


def test_inline_members_need_no_detail_fetch():
    agent = _Agent([
        {"poolid": "sim", "members": [{"vmid": 100}, {"vmid": 101}]},
        {"poolid": "infra", "members": [{"vmid": 200}]},
    ])
    out = _run(vm_inventory.vm_pool_map(agent))
    assert out == {100: "sim", 101: "sim", 200: "infra"}
    # Only the /pools listing — no per-pool detail round-trips.
    assert agent.calls == ["/pools"]


def test_detail_fetch_when_members_absent():
    agent = _Agent(
        [{"poolid": "sim"}, {"poolid": "infra"}],
        details={
            "sim":   {"members": [{"vmid": 100}, {"vmid": 101}]},
            "infra": {"members": [{"vmid": 200}]},
        },
    )
    out = _run(vm_inventory.vm_pool_map(agent))
    assert out == {100: "sim", 101: "sim", 200: "infra"}
    assert "/pools/sim" in agent.calls and "/pools/infra" in agent.calls


def test_detail_fetches_run_concurrently_not_serially():
    # 5 pools need a detail fetch and the cap is 4, so exactly 4 are in flight
    # at once: a serial loop peaks at 1 and an uncapped fan-out at 5. Counting
    # calls in flight, not wall-clock time, means a busy CI runner can't flip
    # it (fleet rule 9.4).
    ids = [f"p{i}" for i in range(5)]
    agent = _Agent(
        [{"poolid": p} for p in ids],
        details={p: {"members": [{"vmid": 100 + i}]} for i, p in enumerate(ids)},
        detail_delay=0.05,
    )
    out = _run(vm_inventory.vm_pool_map(agent))
    assert out == {100 + i: p for i, p in enumerate(ids)}
    assert agent.peak <= 4, f"detail fetches exceeded the cap of 4 (peak {agent.peak})"
    assert agent.peak >= 4, f"detail fetches not concurrent under the cap of 4 (peak {agent.peak})"


def test_mixed_inline_and_detail():
    agent = _Agent(
        [
            {"poolid": "sim", "members": [{"vmid": 100}]},
            {"poolid": "infra"},
        ],
        details={"infra": {"members": [{"vmid": 200}]}},
    )
    out = _run(vm_inventory.vm_pool_map(agent))
    assert out == {100: "sim", 200: "infra"}
    # Only the members-less pool triggers a detail fetch.
    assert "/pools/infra" in agent.calls
    assert "/pools/sim" not in agent.calls


def test_pools_without_id_ignored():
    agent = _Agent([{"comment": "no id"}, {"poolid": "sim", "members": [{"vmid": 1}]}])
    assert _run(vm_inventory.vm_pool_map(agent)) == {1: "sim"}


def test_listing_failure_returns_empty():
    class _Boom:
        async def _pvesh(self, path):
            raise RuntimeError("pvesh down")
    assert _run(vm_inventory.vm_pool_map(_Boom())) == {}


def test_first_pool_wins_on_duplicate_vmid():
    agent = _Agent([
        {"poolid": "a", "members": [{"vmid": 100}]},
        {"poolid": "b", "members": [{"vmid": 100}]},
    ])
    assert _run(vm_inventory.vm_pool_map(agent)) == {100: "a"}
