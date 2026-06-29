"""Async qm/pct wrappers for the unified pxmx Client-Simulation agent.

Every *mutating* operation funnels through :func:`cs_guard.assert_sim_vm` so
the agent never touches a non-sim VM or a protected container. Read-only
operations (status / list) are unguarded — they enumerate the sim range and
are harmless.

Mirrors the fast paths of ``cs/proxmox/proxmox-agent.sh`` (the ``case
"$action"`` dispatch at line ~4042), but with the execution-layer guard the
bash agent lacked, and with the batch commands (start_vms/stop_vms/
snapshot_vms) filtered to the sim range instead of the bash agent's
unfiltered ``qm list | awk 'NR>1{print $1}'``.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set

from .cs_guard import GuardError, assert_sim_vm, is_sim_vm

logger = logging.getLogger("PxmxAgent")

# Subprocess timeout for fast VM commands (matches the cs bash `timeout 60`).
FAST_CMD_TIMEOUT = 60


class PveError(Exception):
    """A qm/pct invocation failed."""


async def _run(argv: List[str], *, timeout: int = FAST_CMD_TIMEOUT,
               check: bool = True, env: Optional[Dict[str, str]] = None) -> "tuple[int, bytes, bytes]":
    """Run ``argv`` as a subprocess and return ``(rc, stdout, stderr)``.

    Raises ``PveError`` on timeout (kills the proc) or, when ``check``, on a
    nonzero exit. The single async primitive every ``qm``/``pct`` wrapper builds on.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PveError(f"timeout ({timeout}s): {' '.join(argv)}")
    if check and proc.returncode != 0:
        raise PveError(stderr.decode().strip() or f"{' '.join(argv)} exited {proc.returncode}")
    return proc.returncode, stdout, stderr


async def detect_guest_type(vmid: int) -> str:
    """Return 'lxc' if vmid is a CT, else 'qemu'.

    Mirrors the bash dispatch sniff: ``pct status <vmid>`` succeeds only for
    containers, ``qm status`` only for qemu guests.
    """
    rc, _, _ = await _run(["pct", "status", str(vmid)], check=False, timeout=10)
    return "lxc" if rc == 0 else "qemu"


async def vm_status(vmid: int) -> Dict[str, Any]:
    """Read-only status probe (no guard). Returns {vmid, kind, running, raw}."""
    kind = await detect_guest_type(vmid)
    bin_ = "pct" if kind == "lxc" else "qm"
    rc, out, _ = await _run([bin_, "status", str(vmid)], check=False, timeout=15)
    text = out.decode().strip()
    return {"vmid": vmid, "kind": kind, "running": "running" in text, "raw": text}


# ── Single-VM mutating commands (all guarded) ───────────────────────────────

async def start_vm(vmid: Any, protected: Set[int]) -> Dict[str, Any]:
    """Guarded start of a sim VM (qemu or lxc). Returns ``{vmid, action, kind}``."""
    vid = assert_sim_vm(vmid, protected)
    kind = await detect_guest_type(vid)
    bin_ = "pct" if kind == "lxc" else "qm"
    await _run([bin_, "start", str(vid)])
    return {"vmid": vid, "action": "start", "kind": kind}


async def stop_vm(vmid: Any, protected: Set[int]) -> Dict[str, Any]:
    """Guarded stop of a sim VM (qemu or lxc). Returns ``{vmid, action, kind}``."""
    vid = assert_sim_vm(vmid, protected)
    kind = await detect_guest_type(vid)
    bin_ = "pct" if kind == "lxc" else "qm"
    await _run([bin_, "stop", str(vid)])
    return {"vmid": vid, "action": "stop", "kind": kind}


async def reboot_vm(vmid: Any, protected: Set[int]) -> Dict[str, Any]:
    """Guarded reboot of a sim VM (qemu or lxc). Returns ``{vmid, action, kind}``."""
    vid = assert_sim_vm(vmid, protected)
    kind = await detect_guest_type(vid)
    bin_ = "pct" if kind == "lxc" else "qm"
    await _run([bin_, "reboot", str(vid)])
    return {"vmid": vid, "action": "reboot", "kind": kind}


async def snapshot_vm(vmid: Any, protected: Set[int],
                      name: Optional[str] = None) -> Dict[str, Any]:
    """Guarded snapshot of a sim VM; auto-names ``auto-<YYYYmmddHHMM>`` if no name given."""
    vid = assert_sim_vm(vmid, protected)
    kind = await detect_guest_type(vid)
    bin_ = "pct" if kind == "lxc" else "qm"
    snap = name or f"auto-{time.strftime('%Y%m%d%H%M')}"
    await _run([bin_, "snapshot", str(vid), snap, "--description", "client-sim"])
    return {"vmid": vid, "action": "snapshot", "snapshot": snap, "kind": kind}


# ── Batch commands (guarded filter, never unfiltered qm list) ───────────────

async def list_qemu_vmids() -> List[int]:
    """All qemu VMIDs on this host. Read-only, unguarded — the caller filters
    to the sim range via :func:`cs_guard.is_sim_vm`."""
    rc, out, _ = await _run(["qm", "list"], check=False, timeout=20)
    if rc != 0:
        return []
    ids: List[int] = []
    for line in out.decode().splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            ids.append(int(parts[0]))
    return ids


async def _batch(action: str, protected: Set[int]) -> Dict[str, Any]:
    """Run a single-VM action (start/stop/snapshot) over every sim VM in ``list_qemu_vmids``.

    Non-sim VMIDs are skipped; per-VM failures are logged and don't abort the batch.
    Returns ``{action, done, skipped}``.
    """
    done: List[int] = []
    skipped: List[int] = []
    fn = {"start_vms": start_vm, "stop_vms": stop_vm,
          "snapshot_vms": snapshot_vm}[action]
    for vid in await list_qemu_vmids():
        if not is_sim_vm(vid, protected):
            skipped.append(vid)
            continue
        try:
            await fn(vid, protected)
            done.append(vid)
        except (PveError, GuardError) as e:
            logger.warning(f"{action}: vmid {vid} failed: {e}")
    return {"action": action, "done": done, "skipped": skipped}


async def start_vms(protected: Set[int]) -> Dict[str, Any]:
    """Start every sim VM on the host (batch). Returns ``{action, started, skipped}``."""
    r = await _batch("start_vms", protected)
    return {**r, "started": r.pop("done")}


async def stop_vms(protected: Set[int]) -> Dict[str, Any]:
    """Stop every sim VM on the host (batch). Returns ``{action, stopped, skipped}``."""
    r = await _batch("stop_vms", protected)
    return {**r, "stopped": r.pop("done")}


async def snapshot_vms(protected: Set[int]) -> Dict[str, Any]:
    """Snapshot every sim VM on the host (batch). Returns ``{action, snapshotted, skipped}``."""
    r = await _batch("snapshot_vms", protected)
    return {**r, "snapshotted": r.pop("done"),
            "snapshot": f"auto-{time.strftime('%Y%m%d%H%M')}"}


# ── Host-level recovery commands (no vmid guard — non-destructive) ──────────

async def unlock_template(template_ids: List[int]) -> Dict[str, Any]:
    """``qm unlock`` the given template VMIDs. Templates are the *source* images
    (typically < 90000), so the sim guard does NOT apply. Best-effort per ID."""
    unlocked: List[int] = []
    failed: List[int] = []
    for tid in template_ids:
        try:
            await _run(["qm", "unlock", str(int(tid))], check=False)
            unlocked.append(int(tid))
        except (PveError, ValueError) as e:
            logger.warning(f"unlock_template: qm unlock {tid} failed: {e}")
            failed.append(int(tid) if str(tid).lstrip('-').isdigit() else tid)
    return {"action": "unlock_template", "unlocked": unlocked, "failed": failed}


async def clear_provision_lock() -> Dict[str, Any]:
    """Kill hung ``qm clone|list`` processes and unlock stuck VMs — the cs bash
    ``clear_provision_lock`` recovery path. Non-destructive; no vmid guard
    (unlock is safe). The cs flock/cooldown state files are owned by the USB
    provision loop (Phase E); they're removed best-effort if present."""
    killed = 0
    # SIGTERM any stuck qm clone/list processes, then SIGKILL survivors.
    rc, out, _ = await _run(["pgrep", "-f", r"^qm (clone|list)"], check=False, timeout=10)
    pids = [int(p) for p in out.decode().split() if p.isdigit()]
    for pid in pids:
        try:
            await _run(["kill", "-TERM", str(pid)], check=False, timeout=5)
            killed += 1
        except PveError:
            pass
    if pids:
        await asyncio.sleep(3)
        await _run(["pkill", "-KILL", "-f", r"^qm (clone|list)"], check=False, timeout=5)

    # Unlock VMs flagged 'locked' in qm list (admin recovery — unlock is safe).
    unlocked: List[int] = []
    rc, out, _ = await _run(["qm", "list"], check=False, timeout=20)
    if rc == 0:
        for line in out.decode().splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[0].isdigit() and parts[2] == "locked":
                vid = int(parts[0])
                try:
                    await _run(["qm", "unlock", str(vid)], check=False)
                    unlocked.append(vid)
                except PveError:
                    pass

    # Best-effort: clear cs provision-halt/cooldown markers if present (Phase E
    # owns these; harmless to touch if absent).
    import os
    for p in ("/var/lib/pxmx/provision.lock",
              "/var/lib/pxmx/provision_halt.cache",
              "/var/lib/pxmx/provision_cooldown_reset"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    return {"action": "clear_provision_lock", "killed_qm_pids": killed,
            "unlocked_vmids": unlocked}


async def clear_usb_quarantine(bus_path: Optional[str] = None) -> Dict[str, Any]:
    """Clear USB dongle-quarantine state for a bus (or all). The quarantine
    store is populated by the USB provision loop (Phase E); until then this
    is a safe no-op that ensures the store is empty/cleared on demand."""
    import json as _json
    import os
    path = "/var/lib/pxmx/usb_quarantine.json"
    try:
        os.makedirs("/var/lib/pxmx", exist_ok=True)
        if bus_path:
            # Clear one bus entry, preserve the rest.
            data: Dict[str, Any] = {}
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path) as f:
                    try:
                        data = _json.load(f) or {}
                    except _json.JSONDecodeError:
                        data = {}
            data.pop(bus_path, None)
            with open(path, "w") as f:
                _json.dump(data, f)
            return {"action": "clear_usb_quarantine", "bus": bus_path, "cleared": True}
        # Clear all.
        with open(path, "w") as f:
            _json.dump({}, f)
        return {"action": "clear_usb_quarantine", "bus": None, "cleared": True}
    except OSError as e:
        logger.warning(f"clear_usb_quarantine: could not write {path}: {e}")
        return {"action": "clear_usb_quarantine", "cleared": False, "error": str(e)}


# ── Long-op primitives (Phase E: delete / reclone / clone_lxc / backup / reseed)
#
# These wrap the qm/pct/vzdump/qmrestore invocations the long ops drive. The
# mutating VM-targeted ones funnel through assert_sim_vm exactly like the fast
# commands; the template/source VMIDs (below the 90000 floor) are NOT guarded
# because they are the clone sources, not sim guests.


def _parse_kv(text: str) -> Dict[str, str]:
    """Parse ``qm config``/``pct config`` ``key: value`` output into a dict."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


async def qm_config(vmid: int) -> Dict[str, str]:
    """Read-only ``qm config <vmid>`` → dict. No guard (read-only)."""
    rc, out, _ = await _run(["qm", "config", str(vmid)], check=False, timeout=20)
    if rc != 0:
        return {}
    return _parse_kv(out.decode())


async def pct_config(vmid: int) -> Dict[str, str]:
    """Read-only ``pct config <vmid>`` → dict. No guard (read-only)."""
    rc, out, _ = await _run(["pct", "config", str(vmid)], check=False, timeout=20)
    if rc != 0:
        return {}
    return _parse_kv(out.decode())


async def qm_set(vmid: int, *args: str, protected: Optional[Set[int]] = None) -> None:
    """``qm set <vmid> <args...>`` — best-effort config mutation (guarded)."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["qm", "set", str(vid), *args], check=False, timeout=30)


async def pct_set(vmid: int, *args: str, protected: Optional[Set[int]] = None) -> None:
    """``pct set <vmid> <args...>`` — best-effort config mutation (guarded)."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["pct", "set", str(vid), *args], check=False, timeout=30)


async def qm_clone(template: Any, vmid: Any, name: str, *,
                   protected: Optional[Set[int]] = None, timeout: int = 600) -> None:
    """``qm clone <template> <vmid> --name <name>``. Template is the source
    (below the sim floor, unguarded); the target vmid is guarded."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["qm", "clone", str(int(template)), str(vid), "--name", name],
               timeout=timeout)


async def pct_clone(source: Any, vmid: Any, hostname: str, *,
                    protected: Optional[Set[int]] = None, timeout: int = 600) -> None:
    """``pct clone <source> <vmid> --hostname <hostname>``. Source unguarded."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["pct", "clone", str(int(source)), str(vid), "--hostname", hostname],
               timeout=timeout)


async def qm_start(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Guarded ``qm start <vmid>`` (qemu). Used by the provision flow to boot a freshly cloned VM."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["qm", "start", str(vid)], timeout=60)


async def pct_start(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Guarded ``pct start <vmid>`` (lxc). Used by the provision flow to boot a freshly cloned CT."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["pct", "start", str(vid)], timeout=60)


async def _qemu_pid(vmid: int) -> Optional[int]:
    """Best-effort QEMU pid lookup (bash _destroy_guest_only 1768-1777)."""
    for p in (f"/run/qemu-server/{vmid}.pid", f"/var/run/qemu-server/{vmid}.pid"):
        try:
            with open(p) as f:
                pid = int(f.read().strip())
            if pid > 0:
                return pid
        except (OSError, ValueError):
            continue
    # Fall back to pgrep. Prefer the space-bounded, precise pattern first (bash
    # 1776-1777) so vmid 100 doesn't match qemu carrying vmid 1001; only then
    # fall back to the broader `qemu.*{vmid}` match.
    for pat in (rf"[[:space:]]{vmid}[[:space:]]", f"qemu.*{vmid}"):
        rc, out, _ = await _run(["pgrep", "-f", pat], check=False, timeout=10)
        for line in out.decode().split():
            if line.isdigit():
                return int(line)
    return None


async def qm_stop_force(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Tiered force-stop mirroring bash _destroy_guest_only (1763-1785): timeout 5
    → forceStop 1 → timeout 1, then a QEMU pid kill + systemd kill if still
    running. Guarded target."""
    vid = assert_sim_vm(vmid, protected or set())
    for flag in (["--timeout", "5"], ["--forceStop", "1"], ["--timeout", "1"]):
        await _run(["qm", "stop", str(vid), "--skiplock", *flag], check=False, timeout=30)
    if not await wait_stopped(vid, "qemu", 15):
        pid = await _qemu_pid(vid)
        if pid:
            await _run(["kill", "-9", str(pid)], check=False, timeout=5)
        else:
            await _run(["systemctl", "kill", "--signal=SIGKILL",
                        f"qemu-server@{vid}.service"], check=False, timeout=10)
        await asyncio.sleep(3)
    await wait_stopped(vid, "qemu", 20)


async def qm_destroy(vmid: Any, protected: Optional[Set[int]] = None,
                     timeout: int = 300) -> bool:
    """``qm destroy --skiplock --purge --destroy-unreferenced-disks``. On failure,
    emergency-kill QEMU and retry once (bash 1796-1807). Returns True on success."""
    vid = assert_sim_vm(vmid, protected or set())
    rc, _, _ = await _run(
        ["qm", "destroy", str(vid), "--skiplock", "--purge",
         "--destroy-unreferenced-disks"], check=False, timeout=timeout)
    if rc == 0:
        # Post-destroy gate (bash _wait_guest_gone 1811-1814): don't claim
        # success until the config/disk is actually gone, so a reclone at the
        # same VMID can't race a still-purging disk.
        return await wait_guest_gone(vid, "qemu", 360)
    pid = await _qemu_pid(vid)
    if pid:
        await _run(["kill", "-9", str(pid)], check=False, timeout=5)
    await _run(["systemctl", "kill", "--signal=SIGKILL",
                f"qemu-server@{vid}.service"], check=False, timeout=10)
    await asyncio.sleep(5)
    rc, _, _ = await _run(
        ["qm", "destroy", str(vid), "--skiplock", "--purge",
         "--destroy-unreferenced-disks"], check=False, timeout=timeout)
    if rc != 0:
        return False
    return await wait_guest_gone(vid, "qemu", 360)


async def pct_stop(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Guarded ``pct stop`` for an lxc sim VM, escalating through ``--force`` → ``--skiplock`` → plain until stopped."""
    vid = assert_sim_vm(vmid, protected or set())
    for flag in (["--force"], ["--skiplock"], []):
        await _run(["pct", "stop", str(vid), *flag], check=False, timeout=120)
        if await wait_stopped(vid, "lxc", 10):
            return


async def pct_destroy(vmid: Any, protected: Optional[Set[int]] = None,
                      timeout: int = 300) -> bool:
    """``pct destroy`` with the bash fallback ladder (1755-1761)."""
    vid = assert_sim_vm(vmid, protected or set())
    for flag in (["--skiplock", "--purge", "--force"], ["--purge", "--force"],
                 ["--skiplock", "--purge"], ["--skiplock"], []):
        rc, _, _ = await _run(["pct", "destroy", str(vid), *flag],
                              check=False, timeout=timeout)
        if rc == 0:
            # Post-destroy gate (bash _wait_guest_gone 1811-1814).
            return await wait_guest_gone(vid, "lxc", 360)
    return False


async def wait_stopped(vmid: int, kind: str, timeout: int) -> bool:
    """Poll ``qm/pct status`` until the guest is stopped or timeout elapses."""
    bin_ = "pct" if kind == "lxc" else "qm"
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, out, _ = await _run([bin_, "status", str(vmid)], check=False, timeout=15)
        if rc == 0 and "running" not in out.decode().lower():
            return True
        await asyncio.sleep(2)
    return False


async def qm_guest_exec(vmid: Any, *cmd: str,
                        protected: Optional[Set[int]] = None) -> bool:
    """``qm guest exec <vmid> ...`` — best-effort (guest agent may be absent)."""
    vid = assert_sim_vm(vmid, protected or set())
    rc, _, _ = await _run(["qm", "guest", "exec", str(vid), *cmd],
                          check=False, timeout=30)
    return rc == 0


async def qm_agent_ping(vmid: Any, protected: Optional[Set[int]] = None,
                        timeout: int = 10) -> bool:
    """Return True if the qemu guest-agent on ``vmid`` responds to ``qm agent ping`` (guest is up + agent live)."""
    vid = assert_sim_vm(vmid, protected or set())
    rc, _, _ = await _run(["qm", "agent", str(vid), "ping"], check=False, timeout=timeout)
    return rc == 0


async def wait_guest_gone(vmid: int, kind: str, timeout: int = 360) -> bool:
    """Poll until the guest's config/disk is fully gone after a destroy (bash
    ``_wait_guest_gone`` 1811-1814 — 360s default). ``qm/pct config`` returns
    non-zero once the VMID no longer exists, so the caller can't race a
    reclone/clone into a still-purging VMID. Read-only (no guard)."""
    bin_ = "pct" if kind == "lxc" else "qm"
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, _, _ = await _run([bin_, "config", str(vmid)], check=False, timeout=15)
        if rc != 0:
            return True  # config gone → disk cleanup done
        await asyncio.sleep(5)
    return False


async def qm_guest_exec_shell(vmid: Any, script: str, *,
                              exec_timeout: int = 60,
                              outer_timeout: int = 90,
                              protected: Optional[Set[int]] = None) -> bool:
    """Run a ``bash -c <script>`` inside the guest with an explicit PVE
    ``--timeout`` (synchronous — PVE waits for completion before returning).

    Mirrors the bash hostname/override writes (proxmox-agent.sh 2025-2046):
    ``qm guest exec <vmid> --timeout <t> -- bash -c '<script>'``. The explicit
    ``--timeout`` is mandatory — without it PVE defaults to async
    fire-and-forget and the caller races the next step. ``hostnamectl`` is
    deliberately avoided here (D-Bus may be unready post-boot and can hang).
    Best-effort: returns the exec rc == 0."""
    vid = assert_sim_vm(vmid, protected or set())
    rc, _, _ = await _run(["qm", "guest", "exec", str(vid),
                            "--timeout", str(exec_timeout), "--",
                            "bash", "-c", script],
                           check=False, timeout=outer_timeout)
    return rc == 0


async def vzdump(vmid: Any, dumpdir: str, *, compress: str = "zstd",
                 mode: str = "snapshot", protected: Optional[Set[int]] = None,
                 timeout: int = 1800) -> None:
    """``vzdump <vmid> --compress zstd --mode snapshot --dumpdir <dir>``."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["vzdump", str(vid), "--compress", compress, "--mode", mode,
                "--dumpdir", dumpdir], timeout=timeout)


async def qmrestore(path: str, vmid: Any, *, force: bool = True,
                    protected: Optional[Set[int]] = None,
                    timeout: int = 600) -> None:
    """``qmrestore <file> <vmid> [--force]``. Target vmid guarded."""
    vid = assert_sim_vm(vmid, protected or set())
    args = ["qmrestore", path, str(vid)]
    if force:
        args.append("--force")
    await _run(args, timeout=timeout)


async def qm_template(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """``qm template <vmid>`` (reseed templating step)."""
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["qm", "template", str(vid)], timeout=120)


async def list_all_vmids() -> List[int]:
    """All qemu + lxc VMIDs present on this host (read-only). Used by the USB
    provision loop to find free slots and reconcile stale bus state."""
    ids: List[int] = list(await list_qemu_vmids())
    rc, out, _ = await _run(["pct", "list"], check=False, timeout=20)
    if rc == 0:
        for line in out.decode().splitlines()[1:]:
            parts = line.split()
            if parts and parts[0].isdigit():
                ids.append(int(parts[0]))
    return ids