"""Guest-agent watchdog ladder (``guest_watchdog.run_pass``) — port of
``proxmox/check_guest.sh``.

Locks in the bash script's escalation verbatim (10m → reset, 20m → power cycle,
no heartbeat → start) plus the interlocks the bash never needed because it
predated the provisioning loop:

* a VM the loop is cloning/deleting/recloning is SKIPPED, never acted on —
  resetting mid-``qm clone`` corrupts the disk;
* a VM inside its post-clone settle window is skipped (QGA-silent by design);
* the action cooldown stops a reset being escalated to a power cycle on the very
  next sweep while the VM is still booting;
* protected vmids and anything at/below the 90000 floor are never touched.

Fake pve_cmds throughout — no Proxmox, no subprocesses.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

os.environ.setdefault("LM_DEP_GUARD_DISABLE", "1")

SRC = Path(__file__).resolve().parent.parent / "src"

_pkg = types.ModuleType("pxmx_agent_src_gw")
_pkg.__path__ = [str(SRC)]
sys.modules["pxmx_agent_src_gw"] = _pkg


def _load(modname, fname):
    spec = importlib.util.spec_from_file_location(
        f"pxmx_agent_src_gw.{modname}", SRC / fname)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"pxmx_agent_src_gw.{modname}"] = mod
    spec.loader.exec_module(mod)
    return mod


_gw = _load("guest_watchdog", "guest_watchdog.py")

T0 = 2_000_000.0
MIN = 60.0


class _FakePve:
    """Records every action; answers pings from `alive`."""

    def __init__(self, vmids, alive=(), stopped_after_stop=True, ping_raises=()):
        self._vmids = list(vmids)
        self._alive = set(alive)
        self.actions = []
        self._stopped_after_stop = stopped_after_stop
        # VMIDs whose ping should RAISE instead of returning a bool — mirrors
        # what the REAL qm_agent_ping does when the underlying `qm agent
        # ping` subprocess call itself times out (pve_cmds._run raises
        # PveError unconditionally on timeout, regardless of check=False) —
        # the guest's virtio-serial socket is wedged badly enough that even
        # the ping helper hangs, a STRONGER "guest is dead" signal than a
        # clean non-zero exit.
        self._ping_raises = set(ping_raises)

    async def list_qemu_vmids(self):
        return list(self._vmids)

    async def is_template(self, vmid, kind=None):
        return False

    async def qm_agent_ping(self, vmid, protected=None, timeout=None):
        if int(vmid) in self._ping_raises:
            raise TimeoutError(f"timeout ({timeout}s): qm agent {vmid} ping")
        return int(vmid) in self._alive

    async def start_vm(self, vmid, protected=None, **kw):
        self.actions.append(("start", int(vmid)))

    async def stop_vm(self, vmid, protected=None, **kw):
        self.actions.append(("stop", int(vmid)))

    async def vm_status(self, vmid):
        return {"vmid": vmid, "kind": "qemu",
                "running": not self._stopped_after_stop, "raw": ""}

    async def _run(self, argv, check=False, timeout=None):
        self.actions.append((argv[1], int(argv[2])) if len(argv) > 2 else (argv[1], None))
        return 0, b"", b""


class _FakeUsbProv:
    def __init__(self, deleting=(), recloning=(), prov_run=None, clones=None,
                 protected=()):
        self._deleting, self._recloning = list(deleting), list(recloning)
        self._prov_run = prov_run or {"running": False, "items": []}
        self._clones = clones or {}
        self._protected = set(protected)

    def current_deleting_vmids(self):
        return list(self._deleting)

    def current_reclone_vmids(self):
        return list(self._recloning)

    def current_prov_run(self):
        return dict(self._prov_run)

    def load_usb_state(self):
        return {"post_prov_reboot": self._clones}

    def _protected_vmids(self, agent):
        return set(self._protected)


def _wire(monkeypatch, tmp_path, pve, prov):
    """Point the module at tmp state + fakes. run_pass imports pve_cmds and
    usb_provision from the package at call time, so patch sys.modules."""
    lib = tmp_path / "pxmx"
    lib.mkdir(exist_ok=True)
    _gw.PXMLIB = str(lib)
    _gw.GUEST_WATCHDOG_FILE = f"{lib}/guest_watchdog.json"
    sys.modules["pxmx_agent_src_gw.pve_cmds"] = pve
    sys.modules["pxmx_agent_src_gw.usb_provision"] = prov


def _run(pve, prov, tmp_path, monkeypatch, now, seed=None):
    _wire(monkeypatch, tmp_path, pve, prov)
    if seed is not None:
        _gw._save(seed)
    return asyncio.run(_gw.run_pass(object(), now=now))


# ── the ladder ───────────────────────────────────────────────────────────────

def test_responding_vm_refreshes_heartbeat(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001], alive=[90001]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0)
    assert r["responding"] == 1 and pve.actions == []
    assert _gw._load()["90001"]["last_ok"] == T0


def test_silent_under_grace_is_left_alone(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 5 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert pve.actions == [] and r["waiting"] == [90001]


def test_silent_10_to_20_min_resets(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 15 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert ("reset", 90001) in pve.actions
    assert r["reset"] == [90001] and r["power_cycled"] == []


def test_silent_over_20_min_power_cycles_in_order(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 25 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert r["power_cycled"] == [90001]
    names = [a[0] for a in pve.actions]
    assert names.index("unlock") < names.index("stop") < names.index("start")


def test_no_heartbeat_starts_the_vm(tmp_path, monkeypatch):
    """Faithful to the bash: a VM with no heartbeat on record is started."""
    pve, prov = _FakePve([90001]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0)
    assert r["started"] == [90001] and ("start", 90001) in pve.actions


# ── ping-timeout escalation (bug fix) ────────────────────────────────────────
# qm_agent_ping raising (the ping helper itself hangs — pve_cmds._run raises
# unconditionally on timeout, regardless of qm_agent_ping's check=False) used
# to `continue` past the VM entirely: no last_ok bookkeeping, no escalation,
# forever. That treated the WORST case (ping hangs) as invisible while a
# clean non-zero-exit ping escalated normally — backwards. A guest wedged
# badly enough to hang its own QGA ping helper must escalate at LEAST as
# readily as a guest that merely refuses the ping cleanly.

def test_ping_timeout_is_treated_as_silent_not_skipped(tmp_path, monkeypatch):
    pve = _FakePve([90001], ping_raises=[90001])
    prov = _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 25 * MIN,
            seed={"90001": {"last_ok": T0}})
    assert r["power_cycled"] == [90001], (
        "a ping timeout after 25 min of prior silence must escalate to "
        "power-cycle exactly like a clean non-responsive ping would")
    assert r["skipped"] == 0 and 90001 not in r.get("waiting", [])


def test_ping_timeout_is_recorded_on_the_persisted_vm_record(tmp_path, monkeypatch):
    pve = _FakePve([90001], ping_raises=[90001])
    prov = _FakeUsbProv()
    _run(pve, prov, tmp_path, monkeypatch, T0 + 5 * MIN,
        seed={"90001": {"last_ok": T0}})
    rec = _gw._load()["90001"]
    assert rec.get("ping_error_streak") == 1
    assert rec.get("last_ping_error_ts") == T0 + 5 * MIN
    assert "last_ping_error" in rec


def test_ping_error_streak_clears_once_the_guest_responds_again(tmp_path, monkeypatch):
    pve = _FakePve([90001], ping_raises=[90001])
    prov = _FakeUsbProv()
    _run(pve, prov, tmp_path, monkeypatch, T0 + 5 * MIN,
        seed={"90001": {"last_ok": T0}})
    assert _gw._load()["90001"].get("ping_error_streak") == 1
    pve2 = _FakePve([90001], alive=[90001])
    _run(pve2, prov, tmp_path, monkeypatch, T0 + 6 * MIN)
    assert "ping_error_streak" not in _gw._load()["90001"]


# ── interlocks ───────────────────────────────────────────────────────────────

def test_vm_being_deleted_is_skipped(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv(deleting=[90001])
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 25 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert pve.actions == [] and r["skipped"] == 1


def test_vm_being_recloned_is_skipped(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv(recloning=[90001])
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 25 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert pve.actions == [] and r["skipped"] == 1


def test_vm_in_an_active_prov_run_is_skipped(tmp_path, monkeypatch):
    prov = _FakeUsbProv(prov_run={"running": True, "items": [{"vmid": 90001}]})
    pve = _FakePve([90001])
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 25 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert pve.actions == [] and r["skipped"] == 1


def test_recently_cloned_vm_is_skipped(tmp_path, monkeypatch):
    """A fresh clone is QGA-silent by design and reboots itself twice."""
    prov = _FakeUsbProv(clones={"90001": {"cloned_at": T0 + 20 * MIN}})
    pve = _FakePve([90001])
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 25 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert pve.actions == [] and r["skipped"] == 1


def test_clone_grace_expires(tmp_path, monkeypatch):
    prov = _FakeUsbProv(clones={"90001": {"cloned_at": T0}})
    pve = _FakePve([90001])
    r = _run(pve, prov, tmp_path, monkeypatch,
             T0 + _gw.POST_CLONE_GRACE_S + 25 * MIN,
             seed={"90001": {"last_ok": T0}})
    assert r["power_cycled"] == [90001]


def test_cooldown_blocks_immediate_escalation(tmp_path, monkeypatch):
    """A reset must be given time to take effect. Without this the next sweep
    reads the still-booting VM as unresponsive and power-cycles it."""
    pve, prov = _FakePve([90001]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 25 * MIN,
             seed={"90001": {"last_ok": T0, "last_action": "reset",
                             "last_action_ts": T0 + 24 * MIN}})
    assert pve.actions == [] and r["waiting"] == [90001]


def test_cooldown_expires_and_escalation_proceeds(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0 + 60 * MIN,
             seed={"90001": {"last_ok": T0, "last_action": "reset",
                             "last_action_ts": T0 + 15 * MIN}})
    assert r["power_cycled"] == [90001]


# ── guards ───────────────────────────────────────────────────────────────────

def test_below_the_floor_is_never_touched(tmp_path, monkeypatch):
    pve, prov = _FakePve([1001, 100, 90000]), _FakeUsbProv()
    r = _run(pve, prov, tmp_path, monkeypatch, T0)
    assert pve.actions == [] and r["checked"] == 0


def test_protected_vmid_is_never_touched(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv(protected=[90001])
    r = _run(pve, prov, tmp_path, monkeypatch, T0)
    assert pve.actions == [] and r["checked"] == 0


def test_template_is_skipped(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001]), _FakeUsbProv()
    pve.is_template = lambda vmid, kind=None: asyncio.sleep(0, result=True)
    r = _run(pve, prov, tmp_path, monkeypatch, T0)
    assert pve.actions == [] and r["checked"] == 0


def test_destroyed_vmid_is_forgotten(tmp_path, monkeypatch):
    """A recreated vmid must not inherit the dead VM's heartbeat."""
    pve, prov = _FakePve([90001], alive=[90001]), _FakeUsbProv()
    _run(pve, prov, tmp_path, monkeypatch, T0,
         seed={"90001": {"last_ok": T0}, "90999": {"last_ok": T0}})
    assert "90999" not in _gw._load()


def test_ping_failure_does_not_abort_the_sweep(tmp_path, monkeypatch):
    pve, prov = _FakePve([90001, 90002], alive=[90002]), _FakeUsbProv()

    async def _boom(vmid, protected=None, timeout=None):
        if int(vmid) == 90001:
            raise RuntimeError("qm wedged")
        return True
    pve.qm_agent_ping = _boom
    r = _run(pve, prov, tmp_path, monkeypatch, T0)
    assert r["responding"] == 1 and any("90001" in e for e in r["errors"])


# ── the slow jobs must NOT run on the telemetry loop ─────────────────────────
# systemd feeds WatchdogSec only while a TELEMETRY TICK COMPLETES
# (_sd_watchdog_loop tracks _last_tick_done_ts). Running the guest watchdog
# inline in that loop — bounded at 180s, and legitimately slow when it
# stops/starts a hung VM — held the tick open past WatchdogSec=60, so systemd
# killed the agent as hung and restarted it every ~70s, forever:
#     lm-pxmx-agent.service: Watchdog timeout (limit 1min)!
# That took the whole fleet down. This guards the decoupling.

def _agent_src():
    return (Path(__file__).resolve().parent.parent / "src" / "agent.py").read_text()


def _slice(src, start_marker, end_marker):
    i = src.index(start_marker)
    return src[i:src.index(end_marker, i + 1)]


def test_telemetry_loop_does_not_run_the_slow_jobs():
    body = _slice(_agent_src(), "async def _telemetry_loop", "def _cs_telemetry_body")
    assert "guest_watchdog.run_pass" not in body, \
        "guest watchdog is back on the telemetry loop — it will starve WatchdogSec"
    assert "usb_diagnostics.collect" not in body, \
        "usb diagnostics is back on the telemetry loop — it will starve WatchdogSec"


def test_slow_jobs_loop_owns_them_and_is_spawned():
    src = _agent_src()
    loop = _slice(src, "async def _slow_jobs_loop", "async def _sd_watchdog_loop")
    assert "guest_watchdog.run_pass" in loop and "usb_diagnostics.collect" in loop
    assert "if self.cs_enabled:" in loop
    assert "asyncio.create_task(self._slow_jobs_loop())" in src, \
        "_slow_jobs_loop is defined but never started"


def test_both_slow_jobs_stay_bounded():
    """Unbounded, they would wedge their own loop instead — still no good."""
    loop = _slice(_agent_src(), "async def _slow_jobs_loop", "async def _sd_watchdog_loop")
    assert loop.count("asyncio.wait_for") >= 2


def test_telemetry_body_only_reads_cached_results():
    """Decoupling must not change frame CONTENT: the body reads caches, never
    kicks the work itself."""
    body = _slice(_agent_src(), "def _cs_telemetry_body", "async def _set_cs_enabled")
    assert "_last_usb_diag" in body and "current_guest_watchdog()" in body
    assert "await" not in body, "the telemetry body must stay synchronous"


def test_run_command_uses_package_relative_runner_import():
    src = _agent_src()
    assert "from .command_runner import run_local_command" in src
    assert "from command_runner import run_local_command" not in src
