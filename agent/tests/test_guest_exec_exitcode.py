"""qm_guest_exec_shell must report the GUEST's exit code, not qm's.

Root cause of "mis-stamped clones never self-heal": the helper returned
``rc == 0`` — the exit status of the ``qm`` PROCESS. ``qm guest exec`` exits 0
whenever the exec MECHANISM worked and reports the guest's real status inside
its JSON envelope. So every predicate built on it was permanently True:

  * the hostname audit's `[ "$(hostname)" = "<expected>" ]` always "matched",
    so it never re-stamped a single misnamed clone (field evidence: VM 90083
    named `wmiller` with in-guest hostname `sim-rpi-0000` and an EMPTY
    hostname_fix record — audited, mismatched, no strike ever taken);
  * the lsusb dongle-presence probe always said "present";
  * every guest-health check always said "healthy";
  * and a hostname STAMP that failed inside the guest was recorded as success.
"""
import asyncio
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC.parent) not in sys.path:
    sys.path.insert(0, str(SRC.parent))

from src import pve_cmds  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _stub(monkeypatch, rc, payload):
    async def _fake(argv, **kw):
        return rc, (payload.encode() if isinstance(payload, str) else payload), b""
    monkeypatch.setattr(pve_cmds, "_run", _fake)
    monkeypatch.setattr(pve_cmds, "assert_sim_vm", lambda v, p, **k: int(v))


def test_guest_failure_is_false_even_though_qm_succeeded(monkeypatch):
    # THE bug: qm exits 0, guest script exits 1. Must be False, not True.
    _stub(monkeypatch, 0, '{"exitcode":1,"exited":1,"out-data":""}')
    assert _run(pve_cmds.qm_guest_exec_shell(90083, '[ "$(hostname)" = "wmiller" ]')) is False


def test_guest_success_is_true(monkeypatch):
    _stub(monkeypatch, 0, '{"exitcode":0,"exited":1,"out-data":"ok\\n"}')
    assert _run(pve_cmds.qm_guest_exec_shell(90083, "true")) is True


@pytest.mark.parametrize("rc,payload", [
    (1, ""),                                  # qm itself failed
    (0, ""),                                  # no output
    (0, "not json at all"),                   # unparseable envelope
    (0, '{"exited":1,"out-data":"x"}'),       # envelope without exitcode
    (0, '{"exitcode":"nope","exited":1}'),    # non-numeric exitcode
])
def test_unknown_results_are_none_never_true(monkeypatch, rc, payload):
    # Unknown must NOT be reported as success — callers treat True as proof.
    _stub(monkeypatch, rc, payload)
    assert _run(pve_cmds.qm_guest_exec_shell(90083, "hostname")) is None


def test_a_failed_stamp_is_not_reported_as_success(monkeypatch):
    # The action path: a stamp script that fails inside the guest must be
    # falsy so the caller retries instead of recording a bogus success.
    _stub(monkeypatch, 0, '{"exitcode":2,"exited":1,"out-data":""}')
    assert not _run(pve_cmds.qm_guest_exec_shell(90083, "write-the-hostname"))
