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

# Standard library: async subprocesses, JSON, logging, regex and timing.
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Guard helpers that keep mutations limited to sim VMs.
from .cs_guard import GuardError, assert_sim_vm, is_sim_vm

# Shared agent logger.
logger = logging.getLogger("PxmxAgent")

# Subprocess timeout for fast VM commands (matches the cs bash `timeout 60`).
FAST_CMD_TIMEOUT = 60


class PveError(Exception):
    """A qm/pct invocation failed."""


async def _kill_and_reap(proc: "asyncio.subprocess.Process", *,
                         reap_timeout: float = 5.0) -> None:
    """SIGKILL ``proc`` and reap it, bounding the wait so a D-state child can't
    wedge the calling coroutine forever. SIGKILL is not delivered to a process
    in uninterruptible sleep (stuck on storage I/O / a wedged USB passthrough
    device — the failure surface of a dongle-provisioning host running ``qm
    clone`` / ``qm set -usb0``), so a bare ``await proc.wait()`` may block
    indefinitely waiting for a reap that never comes. If the reap doesn't
    settle within ``reap_timeout``, abandon the child to init — the kernel
    reaps it when it eventually leaves D-state — and let the caller raise.

    The single cleanup primitive used by every subprocess kill path (_run,
    agent._pvesh, agent._pvesh_action) so a hung clone/qm/pvesh never
    permanently stalls the provision or telemetry loop. The
    pvesh-subprocess-leak invariant: kill the child on timeout AND cancel."""
    # Kill the process first.
    proc.kill()
    # Then wait (bounded) for it to be reaped.
    try:
        await asyncio.wait_for(proc.wait(), timeout=reap_timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        # D-state child (reap timeout) or a re-entrant cancel during cleanup —
        # either way don't block cleanup; the child is abandoned to init.
        pass


async def _run(argv: List[str], *, timeout: int = FAST_CMD_TIMEOUT,
               check: bool = True, env: Optional[Dict[str, str]] = None) -> "tuple[int, bytes, bytes]":
    """Run ``argv`` as a subprocess and return ``(rc, stdout, stderr)``.

    Raises ``PveError`` on timeout (kills the proc) or, when ``check``, on a
    nonzero exit. The single async primitive every ``qm``/``pct`` wrapper builds on.

    On cancellation the child is killed+reaped before CancelledError re-raises
    (a bare wait_for orphans the qm/pct child — the leak behind fleet-wide
    pvesh slowdowns). Mirrors agent._pvesh."""
    # Start the command with stdout and stderr captured.
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    # Wait for it to finish within the timeout.
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    # On timeout: kill the child and report the failure.
    except asyncio.TimeoutError:
        await _kill_and_reap(proc)
        raise PveError(f"timeout ({timeout}s): {' '.join(argv)}")
    except asyncio.CancelledError:
        # A cancel (e.g. _stop_cs_tasks on a CS-disable/config flip, or the
        # provision gather's PROV_RUN_STUCK_S deadline) must not orphan the
        # child — kill+reap before re-raising.
        await _kill_and_reap(proc)
        raise
    # With check, a non-zero exit is an error (stderr gives the reason).
    if check and proc.returncode != 0:
        raise PveError(stderr.decode().strip() or f"{' '.join(argv)} exited {proc.returncode}")
    # Return exit code and output.
    return proc.returncode, stdout, stderr


async def detect_guest_type(vmid: int) -> str:
    """Return 'lxc' if vmid is a CT, else 'qemu'.

    Mirrors the bash dispatch sniff: ``pct status <vmid>`` succeeds only for
    containers, ``qm status`` only for qemu guests.
    """
    # `pct status` succeeds only for containers.
    rc, _, _ = await _run(["pct", "status", str(vmid)], check=False, timeout=10)
    return "lxc" if rc == 0 else "qemu"


async def vm_status(vmid: int) -> Dict[str, Any]:
    """Read-only status probe (no guard). Returns {vmid, kind, running, raw}."""
    # Pick the right tool for the guest type.
    kind = await detect_guest_type(vmid)
    bin_ = "pct" if kind == "lxc" else "qm"
    # Ask for the status; no error raised if it fails.
    rc, out, _ = await _run([bin_, "status", str(vmid)], check=False, timeout=15)
    # Report whether the output says "running".
    text = out.decode().strip()
    return {"vmid": vmid, "kind": kind, "running": "running" in text, "raw": text}


async def is_template(vmid: Any, kind: Optional[str] = None) -> bool:
    """True if ``vmid`` is a Proxmox TEMPLATE (a clone source), not a runnable
    guest. Templates carry ``template: 1`` in ``qm config`` / ``pct config``.
    Read-only and unguarded (a template's VMID may sit below the sim floor).
    Mirrors the bash agent's ``guest_is_template`` — the single detector every
    delete path uses so a source image can never be destroyed. Best-effort: on
    any probe failure returns ``False`` (unknown → treat as a normal guest; the
    90000-floor + protected-set guard still applies)."""
    # A non-numeric VMID can't be a template.
    try:
        vid = int(vmid)
    except (TypeError, ValueError):
        return False
    # Detect the guest type if not given.
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await detect_guest_type(vid)
    # Read the config with the matching tool.
    bin_ = "pct" if k == "lxc" else "qm"
    rc, out, _ = await _run([bin_, "config", str(vid)], check=False, timeout=15)
    # Unreadable config: treat as a normal guest.
    if rc != 0:
        return False
    # Look for a "template:" line set to a true value.
    for line in out.decode(errors="replace").splitlines():
        s = line.strip().lower()
        if s.startswith("template:"):
            return s.split(":", 1)[1].strip() in ("1", "true", "yes")
    # No template line: a normal guest.
    return False


# Host PCI address → "vvvv:pppp" resolved via `lspci -n`. PCI topology is
# static while the agent runs, so entries never expire — the cache lives for
# the process (an agent restart naturally rebuilds it). Only SUCCESSFUL
# resolutions are cached: a None (miss / transient lspci failure) is NOT stored,
# so it self-heals on the next call instead of permanently mis-tiering the VM.
async def list_host_pci_by_vidpid(vidpids) -> Dict[str, str]:
    """Enumerate PRESENT host PCI functions whose ``vvvv:pppp`` is in ``vidpids``.

    Returns ``{pci_addr: vidpid}`` with FULL-domain addresses (e.g.
    ``0000:01:00.0``) — the form ``qm set -hostpciN`` expects. This is the inverse
    of ``pci_passthrough_vidpids`` (which reads a VM's attached devices): here we
    scan the HOST so the provisioner can find T1/T3 controllers to pass through.
    Empty on any failure (best-effort, never raises)."""
    # The wanted VID:PIDs, normalised; nothing wanted means nothing to find.
    want = {str(v).strip().lower() for v in (vidpids or ()) if v}
    out: Dict[str, str] = {}
    if not want:
        return out
    # -D: full-domain addresses; -n: numeric vendor:device (no name lookup).
    rc, o, _ = await _run(["lspci", "-Dn"], check=False, timeout=15)
    if rc != 0:
        return out
    # Check each lspci line.
    for line in o.decode(errors="replace").lower().splitlines():
        parts = line.split()
        if not parts:
            continue
        # The first field is the PCI address.
        addr = parts[0]  # e.g. "0000:01:00.0"
        # The class code ("0280:") is 4hex+colon+SPACE; the vendor:device is the
        # contiguous 4hex:4hex token after it — same shape _lspci_vidpid matches.
        m = re.search(r"\b([0-9a-f]{4}:[0-9a-f]{4})\b", line[len(addr):])
        # Keep the addresses whose device ID is wanted.
        if m and m.group(1) in want:
            out[addr] = m.group(1)
    # Return the matches.
    return out


# Domain-stripped PCI address -> "vvvv:pppp", filled by _build_lspci_map.
_lspci_vidpid_cache: Dict[str, str] = {}


def _addr_key(addr: str) -> str:
    """Normalise a PCI address for map lookups: lowercased, WITHOUT the domain
    prefix (``0000:03:00.0`` → ``03:00.0``). ``lspci -Dn`` prints full-domain
    addresses while a ``hostpciN`` line or an older ``lspci -s`` may omit the
    domain, so both sides key on the domain-stripped form."""
    # Normalise the address text.
    a = str(addr or "").strip().lower()
    # Split into parts; three parts means the domain is present.
    parts = a.split(":")
    if len(parts) == 3:  # domain:bus:slot.func → drop the domain
        a = ":".join(parts[1:])
    # Return the normalised address.
    return a


async def _build_lspci_map() -> None:
    """Populate ``_lspci_vidpid_cache`` from a SINGLE ``lspci -Dn`` scan, keyed by
    the domain-stripped address. One scan for the whole host is both cheaper than
    a ``lspci -s`` per address AND avoids the ``-s`` selector mismatch (some lspci
    builds don't match a full-domain address given to ``-s``, returning nothing —
    which silently left every PCI VM unresolved, i.e. unclassified). Best-effort."""
    # Scan all PCI devices once.
    rc, o, _ = await _run(["lspci", "-Dn"], check=False, timeout=15)
    if rc != 0:
        return
    # Parse each line into address and vendor:device.
    for line in o.decode(errors="replace").lower().splitlines():
        parts = line.split()
        if not parts:
            continue
        # The first field is the address; find the vendor:device after it.
        addr = parts[0]
        m = re.search(r"\b([0-9a-f]{4}:[0-9a-f]{4})\b", line[len(addr):])
        # Cache the result under the normalised address.
        if m:
            _lspci_vidpid_cache[_addr_key(addr)] = m.group(1)


async def _lspci_vidpid(addr: str) -> Optional[str]:
    """Resolve a PCI address to its lowercased ``vvvv:pppp``, memoized module-level
    (see ``_lspci_vidpid_cache``). Resolves from a one-shot ``lspci -Dn`` map so a
    full-domain ``hostpciN`` address (``0000:03:00.0``) resolves regardless of the
    local ``lspci -s`` selector behaviour. ``None`` if the address isn't present."""
    # Use the cache when we already know the address.
    key = _addr_key(addr)
    if key in _lspci_vidpid_cache:
        return _lspci_vidpid_cache[key]
    # Otherwise rescan lspci and look again.
    await _build_lspci_map()
    return _lspci_vidpid_cache.get(key)


def _live_config_text(text: str) -> str:
    """The LIVE section of a Proxmox VM/CT config — everything before the first
    ``[snapshot]`` / ``[pending]`` section header. The pmxcfs conf file appends
    snapshot configs (each ``[name]``) after the current config; a ``hostpciN``
    inside a snapshot is NOT attached to the running VM, so tier classification
    must ignore it (``qm config`` already returns only the live section, but the
    direct file read below sees the whole file)."""
    # Collect lines until the first section header.
    out_lines = []
    for line in text.splitlines():
        if re.match(r"^\[[^\]]+\]\s*$", line):  # first snapshot/pending header
            break
        out_lines.append(line)
    # Join the live lines back into text.
    return "\n".join(out_lines)


async def _vm_config_text(vid: int, kind: str) -> Optional[str]:
    """Raw LIVE VM config text for ``vid``.

    Reads the pmxcfs config file DIRECTLY
    (``/etc/pve/qemu-server/<vid>.conf`` | ``/etc/pve/lxc/<vid>.conf``) rather than
    shelling ``qm config``. ``qm config`` spawns perl (~0.5-2s each); probed over
    EVERY VM in a tier sweep it blew the 8s tiers deadline on hosts with ~20+ VMs,
    so the sweep perpetually fell back to USB-only and T1/T3 VMs never classified
    (Class column read e.g. ``0 T1`` despite passed-through radios). A file read is
    ~instant. Falls back to ``qm``/``pct config`` if the file is unreadable. Returns
    only the live section (snapshots stripped). None on total failure."""
    # Config file path for the guest type.
    path = ("/etc/pve/lxc/%d.conf" % vid) if kind == "lxc" \
        else ("/etc/pve/qemu-server/%d.conf" % vid)
    # Read the file directly (fast) and keep only the live section.
    try:
        with open(path, "r") as fh:
            return _live_config_text(fh.read())
    # If that fails, use the qm/pct config command instead.
    except Exception:  # noqa: BLE001 — fall back to the (slower) CLI
        rc, out, _ = await _run(["pct" if kind == "lxc" else "qm", "config", str(vid)],
                                check=False, timeout=15)
        return out.decode(errors="replace") if rc == 0 else None


async def pci_passthrough_vidpids(vmid: Any, kind: Optional[str] = None) -> Set[str]:
    """Set of PCI-passthrough device VID:PIDs (lowercased ``vvvv:pppp``) attached
    to ``vmid`` via ``hostpciN:`` lines in its live config, each resolved with
    ``lspci -n``. This is the T1/T3 tier signal (T1 and T3 are PCI passthrough;
    T2 is USB). Read-only, via a direct pmxcfs file read (see ``_vm_config_text``)
    so a full-fleet sweep stays inside the tiers deadline. Best-effort — returns
    whatever resolves, empty on any failure; LXC (no PCI passthrough) short-circuits
    to empty. Resource-mapping passthroughs (``hostpci0: mapping=...``) are skipped
    (no raw address to resolve)."""
    # A non-numeric VMID has no devices.
    try:
        vid = int(vmid)
    except (TypeError, ValueError):
        return set()
    # Detect the guest type if needed; containers have no PCI passthrough.
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await detect_guest_type(vid)
    if k == "lxc":
        return set()
    # Read the live config; nothing to scan if it's unavailable.
    text = await _vm_config_text(vid, k)
    if not text:
        return set()
    # hostpciN: <addr>[,opt=val,...] — capture the first token (the PCI address).
    addrs: Set[str] = set()
    # Collect each hostpci line's address.
    for m in re.finditer(r"(?mi)^hostpci\d+:\s*([^\s,]+)", text):
        tok = m.group(1).strip()
        if not tok or "=" in tok:  # skip resource-mapping form (mapping=...)
            continue
        addrs.add(tok)
    # Resolve each address to its vendor:device ID.
    vidpids: Set[str] = set()
    for addr in addrs:
        vidpid = await _lspci_vidpid(addr)  # memoized — PCI topology is static
        if vidpid:
            vidpids.add(vidpid)
    # Return the IDs we resolved.
    return vidpids


# ── Single-VM mutating commands (all guarded) ───────────────────────────────

async def start_vm(vmid: Any, protected: Set[int],
                   kind: Optional[str] = None) -> Dict[str, Any]:
    """Guarded start of a sim VM (qemu or lxc). Returns ``{vmid, action, kind}``.
    ``kind`` ("qemu"/"lxc") skips the detect_guest_type probe when the caller
    already knows it (e.g. the qemu-only batch path)."""
    # Refuse anything that isn't a sim VM.
    vid = assert_sim_vm(vmid, protected)
    # Detect the guest type if not given.
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await detect_guest_type(vid)
    # Start it with the matching tool.
    bin_ = "pct" if k == "lxc" else "qm"
    await _run([bin_, "start", str(vid)])
    return {"vmid": vid, "action": "start", "kind": k}


async def stop_vm(vmid: Any, protected: Set[int],
                  kind: Optional[str] = None) -> Dict[str, Any]:
    """Guarded stop of a sim VM (qemu or lxc). Returns ``{vmid, action, kind}``.
    ``kind`` skips the detect_guest_type probe when known (see start_vm)."""
    # Refuse anything that isn't a sim VM.
    vid = assert_sim_vm(vmid, protected)
    # Detect the guest type if not given.
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await detect_guest_type(vid)
    # Stop it with the matching tool.
    bin_ = "pct" if k == "lxc" else "qm"
    await _run([bin_, "stop", str(vid)])
    return {"vmid": vid, "action": "stop", "kind": k}


async def reboot_vm(vmid: Any, protected: Set[int]) -> Dict[str, Any]:
    """Guarded reboot of a sim VM (qemu or lxc). Returns ``{vmid, action, kind}``."""
    # Refuse anything that isn't a sim VM, then detect its type.
    vid = assert_sim_vm(vmid, protected)
    kind = await detect_guest_type(vid)
    # Reboot it with the matching tool.
    bin_ = "pct" if kind == "lxc" else "qm"
    await _run([bin_, "reboot", str(vid)])
    return {"vmid": vid, "action": "reboot", "kind": kind}


async def snapshot_vm(vmid: Any, protected: Set[int],
                      name: Optional[str] = None,
                      kind: Optional[str] = None) -> Dict[str, Any]:
    """Guarded snapshot of a sim VM; auto-names ``auto-<YYYYmmddHHMM>`` if no name given.
    ``kind`` skips the detect_guest_type probe when known (see start_vm)."""
    # Refuse anything that isn't a sim VM.
    vid = assert_sim_vm(vmid, protected)
    # Detect the guest type if not given.
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await detect_guest_type(vid)
    # Take the snapshot, naming it by time if no name was given.
    bin_ = "pct" if k == "lxc" else "qm"
    snap = name or f"auto-{time.strftime('%Y%m%d%H%M')}"
    await _run([bin_, "snapshot", str(vid), snap, "--description", "client-sim"])
    return {"vmid": vid, "action": "snapshot", "snapshot": snap, "kind": k}


# ── Unguarded single-VM actions (Hypervisors view — ANY vmid) ────────────────
#
# The guarded start_vm/stop_vm/reboot_vm/snapshot_vm above funnel through
# cs_guard.assert_sim_vm, which refuses any vmid outside the client-sim 90000
# floor — so they can't be reused for the Hypervisors view, which manages real
# tenant VMs at arbitrary vmids. These *_any variants skip the guard and operate
# on any vmid the hub requests. `kind` ("qemu"/"lxc") is taken from the caller
# when known (the hub has the VM's type) to avoid a detect_guest_type round-trip.

async def vm_action_any(vmid: Any, action: str, kind: Optional[str] = None,
                        snapshot_name: Optional[str] = None,
                        backup_opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Unguarded start/stop/reboot/snapshot/backup of ANY vmid (qemu or lxc).

    Returns ``{vmid, action, kind[, snapshot]}``. Raises ``PveError`` on failure.
    ``backup_opts`` (storage/mode/keep) is required for the ``backup`` action.
    """
    # VMID and guest type (detected if not given).
    vid = int(vmid)
    k = (kind or "").lower()
    if k not in ("qemu", "lxc"):
        k = await detect_guest_type(vid)
    # Tool for the guest type and the normalised action.
    bin_ = "pct" if k == "lxc" else "qm"
    act = (action or "").lower()
    # Start.
    if act == "start":
        await _run([bin_, "start", str(vid)])
    # Stop.
    elif act == "stop":
        await _run([bin_, "stop", str(vid)])
    elif act in ("reboot", "restart"):
        # RELIABLE reboot. `qm/pct reboot` is a GRACEFUL ACPI shutdown+start: it
        # needs the guest to honor ACPI, silently no-ops on a guest that doesn't
        # (no qemu-guest-agent / ACPI daemon), and BLOCKS long enough to be killed
        # mid-flight by the command timeout — which is why "reboot delivered but
        # nothing happened". Instead:
        #   * QEMU → `qm reset` — an immediate HARDWARE reset (the reset button):
        #     always reboots a RUNNING VM, no guest cooperation. Not a clean
        #     shutdown, but it actually reboots.
        #   * LXC  → `pct reboot` — containers reboot cleanly + fast.
        # Foreground + rc check (qm reset is fast) so a failed reset (e.g. VM
        # not running) raises PveError instead of a silent false-success — the
        # old detached create_subprocess_exec returned "started" without ever
        # checking the reset actually ran.
        reboot_argv = [bin_, "reset", str(vid)] if k == "qemu" else [bin_, "reboot", str(vid)]
        await _run(reboot_argv)
        return {"vmid": vid, "action": "reboot", "kind": k,
                "method": "reset" if k == "qemu" else "reboot", "started": True}
    # Snapshot, named by time if no name was given.
    elif act == "snapshot":
        snap = snapshot_name or f"auto-{time.strftime('%Y%m%d%H%M')}"
        await _run([bin_, "snapshot", str(vid), snap, "--description", "lm-hub"])
        return {"vmid": vid, "action": "snapshot", "snapshot": snap, "kind": k}
    elif act == "backup":
        # Node-level vzdump (not qm/pct). vzdump can run for minutes, so this is
        # FIRE-AND-FORGET: spawn it detached and return {started:true} promptly so
        # the command RPC doesn't time out — completion/failure surfaces in the
        # Proxmox node task log. The storage MUST be configured (Setup →
        # Hypervisors); mode defaults to snapshot (no downtime). keep>0 prunes to
        # keep-last=N. Validate the storage exists first so an obvious misconfig
        # fails fast with a clear message instead of a silent task-log error.
        # Storage must be set.
        opts = backup_opts or {}
        storage = str(opts.get("storage") or "").strip()
        if not storage:
            raise PveError("backup: no storage configured — set one in Setup → Hypervisors")
        # Check the storage exists on this host.
        rc, out, _ = await _run(["pvesm", "status", "--storage", storage],
                                check=False, timeout=15)
        if rc != 0 or storage not in out.decode(errors="replace"):
            raise PveError(f"backup: storage '{storage}' not found on this host")
        # Backup mode, defaulting to snapshot if not recognised.
        mode = str(opts.get("mode") or "snapshot").lower()
        if mode not in ("snapshot", "suspend", "stop"):
            mode = "snapshot"
        # vzdump command line.
        argv = ["vzdump", str(vid), "--mode", mode, "--storage", storage, "--compress", "zstd"]
        # Optional retention: keep only the last N backups.
        try:
            keep = int(opts.get("keep") or 0)
        except (TypeError, ValueError):
            keep = 0
        if keep > 0:
            argv += ["--prune-backups", f"keep-last={keep}"]
        # Launch vzdump in the background without waiting.
        await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return {"vmid": vid, "action": "backup", "storage": storage,
                "mode": mode, "keep": keep, "kind": k, "started": True}
    elif act in ("destroy", "delete"):
        # Irreversible delete. `qm/pct destroy` FAILS on a running guest, so stop
        # first (best-effort — `check=False` so the "not running" error on an
        # already-stopped guest is ignored), then destroy + --purge so the VM,
        # its disks, AND its backup-job membership are all removed (not just the
        # config). Authorized + delete-protection-checked at the hub; the agent
        # runs it unguarded like the other actions.
        await _run([bin_, "stop", str(vid)], check=False)
        await _run([bin_, "destroy", str(vid), "--purge"])
        return {"vmid": vid, "action": "destroy", "kind": k, "purged": True}
    # Anything else is an unknown action.
    else:
        raise PveError(f"unknown vm action: {action}")
    # Report the action done.
    return {"vmid": vid, "action": act, "kind": k}


async def next_free_vmid() -> int:
    """Next free VMID from Proxmox (``/cluster/nextid`` via pvesh).

    Falls back to max(qm list ∪ pct list)+1 when pvesh is unavailable. Used by
    the clone-from-template flow to auto-assign the new VM's id.
    """
    # Ask Proxmox for the next free ID.
    rc, out, _ = await _run(["pvesh", "get", "/cluster/nextid"], check=False, timeout=15)
    if rc == 0:
        # Parse the reply (JSON object, quoted string, or plain number).
        try:
            data = json.loads(out.decode())
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("data", data)
        if isinstance(data, str):
            data = data.strip().strip('"')
        # Use it if it's a number.
        try:
            return int(data)
        except (TypeError, ValueError):
            pass
    # Fallback: max existing vmid + 1.
    used: List[int] = []
    # Collect every VMID listed by qm and pct.
    for bin_ in ("qm", "pct"):
        rc, out, _ = await _run([bin_, "list"], check=False, timeout=20)
        if rc != 0:
            continue
        for line in out.decode().splitlines()[1:]:  # skip header
            parts = line.split()
            if parts and parts[0].isdigit():
                used.append(int(parts[0]))
    # Next number after the highest (100 if none).
    return (max(used) + 1) if used else 100


async def clone_vm_any(template_vmid: Any, new_vmid: Any, name: str, kind: str,
                       *, full: bool = True, pool: Optional[str] = None,
                       timeout: int = 600) -> None:
    """Unguarded clone of a template VM/CT to a new VMID (clone-from-template).

    ``kind`` is ``"qemu"`` or ``"lxc"`` (caller passes it so we skip a
    detect_guest_type round-trip). qemu uses ``qm clone`` (full clone by default
    so the new VM has its own disk); lxc uses ``pct clone`` with ``--hostname``.
    ``pool`` optionally places the new VM in a Proxmox resource pool (both
    ``qm clone`` and ``pct clone`` accept ``--pool``). Raises ``PveError`` on
    failure.
    """
    # Source and target VMIDs.
    tvid = int(template_vmid)
    nvid = int(new_vmid)
    # Containers: pct clone with a hostname (and optional pool).
    if kind == "lxc":
        flags = ["pct", "clone", str(tvid), str(nvid), "--hostname", name]
        if pool:
            flags += ["--pool", str(pool)]
        await _run(flags, timeout=timeout)
    # VMs: qm clone, full by default (and optional pool).
    else:
        flags = ["qm", "clone", str(tvid), str(nvid), "--name", name]
        if full:
            flags.append("--full")
        if pool:
            flags += ["--pool", str(pool)]
        await _run(flags, timeout=timeout)


async def set_tags_any(vmid: Any, kind: str, tags: List[str]) -> None:
    """Set the ``tags`` list on a VM/CT (qemu or lxc). ``tags`` is a list of
    strings; Proxmox stores them semicolon-separated. Overwrites the current
    tags. Used by clone-from-template to tag the new VM for the cloning tenant.
    """
    # Join the non-empty tags with ";" and write them.
    vid = int(vmid)
    joined = ";".join(str(t).strip() for t in tags if str(t).strip())
    bin_ = "pct" if kind == "lxc" else "qm"
    await _run([bin_, "set", str(vid), "--tags", joined], timeout=30)


async def retag_tenant(old_tag: str, new_tag: str) -> Dict[str, Any]:
    """Re-tag every VM/CT on this host that carries ``old_tag`` so it carries
    ``new_tag`` instead — used by cross-tenant migration when a tenant's
    ``proxmox_tag`` changes. Reads ``/cluster/resources`` for each VM's current
    tags, swaps the tag (dedup, preserving the VM's other tags), and writes it
    back via :func:`set_tags_any`. VMs without ``old_tag`` are skipped; a no-op
    or empty tag pair is a no-op. Returns the changed VMs + any per-VM errors."""
    # Clean the tag names; nothing to do if either is missing or they're the same.
    old_tag = (old_tag or "").strip()
    new_tag = (new_tag or "").strip()
    if not old_tag or not new_tag or old_tag == new_tag:
        return {"status": "SUCCESS", "retagged": [], "count": 0,
                "message": "no tag change requested"}
    # Fetch every VM and its tags from the cluster.
    rc, out, err = await _run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        check=False, timeout=30)
    if rc != 0:
        return {"status": "ERROR",
                "message": f"cluster/resources failed: {err.decode('utf-8', 'replace')[:200]}"}
    # Parse the JSON list.
    try:
        resources = json.loads(out.decode("utf-8", "replace") or "[]")
    except Exception as e:  # noqa: BLE001
        return {"status": "ERROR", "message": f"parse failed: {e}"}
    # Collected results and per-VM errors.
    changed: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    # Check each VM's tags.
    for r in resources:
        tags = [t for t in str(r.get("tags", "")).split(";") if t.strip()]
        if old_tag not in tags:
            continue
        # VMID and guest type.
        vmid = r.get("vmid")
        kind = "lxc" if r.get("type") == "lxc" else "qemu"
        # Swap the old tag for the new one (no duplicate).
        new_tags = [t for t in tags if t != old_tag]
        if new_tag not in new_tags:
            new_tags.append(new_tag)
        # Write the new tags; record failures without stopping.
        try:
            await set_tags_any(vmid, kind, new_tags)
            changed.append({"vmid": vmid, "kind": kind, "tags": new_tags})
        except Exception as e:  # noqa: BLE001 - one VM must not abort the sweep
            errors[str(vmid)] = str(e)
    # Report the outcome (PARTIAL if any VM failed).
    status = "SUCCESS" if not errors else "PARTIAL"
    return {"status": status, "retagged": changed, "count": len(changed), "errors": errors,
            "message": (f"re-tagged {len(changed)} VM(s) {old_tag!r}→{new_tag!r}"
                        + (f", {len(errors)} error(s)" if errors else ""))}


# Prefix used for client-simulation tags.
_SIM_TAG_PREFIX = "sim-"


async def apply_sim_tags(tag_map: Dict[Any, List[str]]) -> Dict[str, Any]:
    """Apply ``sim-`` tags to VMs LOCALLY via ``qm``/``pct`` — no Proxmox API.

    ``tag_map`` is ``{vmid: [desired sim- tags]}`` computed by the cs spoke from
    the client registry (client hostname → active_simulations). The cs spoke is
    OFF the Proxmox host and cannot run ``qm``; it used to PUT ``tags=`` to the
    API per VM, which storms the telemetry hot path (CS_INGEST_TELEMETRY Request
    Timeouts → stale VM Server / quota engine fleet-wide). This runs on the host.

    For each VM: read its current tags from ``/cluster/resources`` (one read for
    the whole sweep), preserve every non-``sim-`` tag, replace the ``sim-`` tags
    with the desired set, and write back via :func:`set_tags_any`. Idempotent
    (unchanged VMs are skipped). One VM's failure never aborts the sweep. VMs not
    present on this host are silently skipped. Best-effort — returns a summary."""
    # Nothing to apply.
    if not tag_map:
        return {"status": "SUCCESS", "updated": [], "count": 0,
                "message": "no tags to apply"}
    # Read every VM and its tags from the cluster in one call.
    rc, out, err = await _run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        check=False, timeout=30)
    if rc != 0:
        return {"status": "ERROR",
                "message": f"cluster/resources failed: {err.decode('utf-8', 'replace')[:200]}"}
    # Parse the JSON list.
    try:
        resources = json.loads(out.decode("utf-8", "replace") or "[]")
    except Exception as e:  # noqa: BLE001
        return {"status": "ERROR", "message": f"parse failed: {e}"}
    # Index VMs by VMID; collect results and per-VM errors.
    by_vmid = {str(r.get("vmid")): r for r in resources if r.get("vmid") is not None}
    updated: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}
    # Work out each VM's new tag list.
    for vmid, desired in tag_map.items():
        r = by_vmid.get(str(vmid))
        if r is None:
            continue  # VM not on this host (another node owns it) — skip
        # Guest type and current tags.
        kind = "lxc" if r.get("type") == "lxc" else "qemu"
        current = [t.strip() for t in str(r.get("tags", "")).split(";") if t.strip()]
        # Keep the non-sim tags and replace the sim tags with the desired set.
        non_sim = [t for t in current if not t.lower().startswith(_SIM_TAG_PREFIX)]
        desired_clean = sorted({
            str(t).strip() for t in (desired or [])
            if str(t).strip() and str(t).strip().lower().startswith(_SIM_TAG_PREFIX)})
        new_tags = non_sim + desired_clean
        # Skip VMs whose tags are already right.
        if sorted(current) == sorted(new_tags):
            continue  # idempotent — no change for this VM
        # Write the new tags; record failures without stopping.
        try:
            await set_tags_any(vmid, kind, new_tags)
            updated.append({"vmid": vmid, "tags": new_tags})
        except Exception as e:  # noqa: BLE001 - one VM must not abort the sweep
            errors[str(vmid)] = str(e)
    # Report the outcome (PARTIAL if any VM failed).
    status = "SUCCESS" if not errors else "PARTIAL"
    return {"status": status, "updated": updated, "count": len(updated), "errors": errors,
            "message": (f"tagged {len(updated)} VM(s)"
                        + (f", {len(errors)} error(s)" if errors else ""))}


# ── Batch commands (guarded filter, never unfiltered qm list) ───────────────

async def list_qemu_vmids() -> List[int]:
    """All qemu VMIDs on this host. Read-only, unguarded — the caller filters
    to the sim range via :func:`cs_guard.is_sim_vm`."""
    # List VMs; empty on failure.
    rc, out, _ = await _run(["qm", "list"], check=False, timeout=20)
    if rc != 0:
        return []
    # Take the numeric VMID from each row after the header.
    ids: List[int] = []
    for line in out.decode().splitlines()[1:]:
        parts = line.split()
        if parts and parts[0].isdigit():
            ids.append(int(parts[0]))
    return ids


async def list_qemu_vms() -> List[Tuple[int, str]]:
    """All qemu VMs on this host as ``(vmid, name)`` from ``qm list``.

    Used to resolve a configured clone-source template NAME to its vmid
    (auto-provisioning accepts either a vmid or a name in the template field;
    see ``usb_provision._resolve_template_vmid``). Read-only, unguarded.

    ``qm list`` columns are FIXED-WIDTH (``VMID %10s``, ``NAME %20s``,
    ``STATUS %10s``, …) so NAME is SLICED off the column, not whitespace-split
    — a name containing spaces survives. A name longer than 20 chars overflows
    the NAME column and would be truncated by the slice (realistic template
    names are short; flagged in the field's WebUI help). Returns ``[]`` on any
    failure (non-zero ``qm list`` or empty output)."""
    # List VMs; empty on failure.
    rc, out, _ = await _run(["qm", "list"], check=False, timeout=20)
    if rc != 0:
        return []
    # Parse each row after the header.
    vms: List[Tuple[int, str]] = []
    for line in out.decode().splitlines()[1:]:  # skip the header row
        if len(line) < 10:
            continue
        # The VMID column is the first 10 characters.
        vid_s = line[:10].strip()
        if not vid_s.isdigit():
            continue
        # NAME column is [10:30]; fall back to the remainder if the line is
        # shorter than the full column set (a short name + trailing columns).
        name = line[10:30].strip() if len(line) >= 30 else line[10:].strip()
        # Record the VM.
        vms.append((int(vid_s), name))
    return vms


async def local_node_name() -> str:
    """This host's Proxmox node name — the ``/cluster/status`` entry flagged
    ``local: 1``. Falls back to the short hostname (Proxmox requires the node
    name to equal the hostname), so this also works on a standalone (non-
    clustered) host. Used to scope clone-source template resolution to THIS
    node — on a cluster every node carries its own same-named template at a
    different vmid, and a cluster-wide match would wrongly look ambiguous."""
    # Ask the cluster which node is local; any failure falls back to the hostname.
    try:
        rc, out, _ = await _run(
            ["pvesh", "get", "/cluster/status", "--output-format", "json"],
            check=False, timeout=10)
        if rc == 0:
            for e in json.loads(out.decode("utf-8", "replace") or "[]"):
                if e.get("type") == "node" and e.get("local") in (1, True):
                    name = e.get("name")
                    if name:
                        return str(name)
    except Exception:  # noqa: BLE001 — fall back to the hostname
        pass
    # Fallback: short hostname.
    import socket
    return socket.gethostname().split(".")[0]


async def _pvesh_json(path: str, timeout: int = 15) -> Any:
    """``pvesh get <path>`` as JSON; None on any failure."""
    # Run pvesh and parse its JSON; None on failure.
    try:
        rc, out, _ = await _run(["pvesh", "get", path, "--output-format", "json"],
                                check=False, timeout=timeout)
        return json.loads(out.decode("utf-8", "replace") or "null") if rc == 0 else None
    except Exception:  # noqa: BLE001
        return None


async def cluster_info() -> Dict[str, Any]:
    """``{name, node, nodes}`` when this host is a member of a multi-node
    Proxmox cluster, else ``{}`` (standalone host or pvesh unavailable). Lets
    the hub's OS-updates module put a clustered node into HA maintenance
    before an auto-reboot."""
    # Read the cluster status rows.
    rows = await _pvesh_json("/cluster/status")
    if not isinstance(rows, list):
        return {}
    # Find the cluster entry, all nodes, and the local node.
    cluster = next((r for r in rows if r.get("type") == "cluster"), None)
    nodes = [r for r in rows if r.get("type") == "node"]
    local = next((r for r in nodes if r.get("local") in (1, True)), None)
    # Only report a real multi-node cluster.
    if not cluster or not local or len(nodes) < 2:
        return {}
    return {"name": str(cluster.get("name") or ""), "node": str(local.get("name") or ""),
            "nodes": len(nodes)}


# Guests this host shut down for maintenance (they could not be live-migrated);
# started again when maintenance ends, even across the reboot.
_MAINT_STATE = Path("/var/lib/lm/pve_maintenance.json")
_SHUTDOWN_TIMEOUT_S = 120
_drain_task: Optional["asyncio.Task"] = None


def _load_shutdown() -> List[Dict[str, Any]]:
    try:
        return list(json.loads(_MAINT_STATE.read_text()).get("shutdown") or [])
    except Exception:  # noqa: BLE001
        return []


def _save_shutdown(items: List[Dict[str, Any]]) -> None:
    try:
        if items:
            _MAINT_STATE.parent.mkdir(parents=True, exist_ok=True)
            _MAINT_STATE.write_text(json.dumps({"shutdown": items}))
        else:
            _MAINT_STATE.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("maintenance state not saved: %s", e)


async def _ha_state(node: str) -> Tuple[Dict[str, Any], List[str], Set[int]]:
    """``(manager_status, HA guests still active here, vmids of ALL HA-managed guests)``."""
    status = await _pvesh_json("/cluster/ha/status/manager_status")
    ms = (status.get("manager_status") or {}) if isinstance(status, dict) else {}
    services = ms.get("service_status") or {}
    here = sorted(sid for sid, sv in services.items()
                  if (sv or {}).get("node") == node
                  and (sv or {}).get("state") not in ("stopped", "disabled", "ignored", "error"))
    managed: Set[int] = set()
    for sid in services:
        tail = str(sid).split(":", 1)[-1]
        if tail.isdigit():
            managed.add(int(tail))
    return ms, here, managed


async def _running_guests(node: str, skip: Set[int]) -> List[Tuple[int, str]]:
    """Running, non-template qemu VMs and LXC containers on ``node`` that HA does not manage."""
    out: List[Tuple[int, str]] = []
    for kind in ("qemu", "lxc"):
        rows = await _pvesh_json(f"/nodes/{node}/{kind}")
        for r in (rows if isinstance(rows, list) else []):
            try:
                vmid = int(r.get("vmid"))
            except (TypeError, ValueError):
                continue
            if r.get("status") == "running" and not r.get("template") and vmid not in skip:
                out.append((vmid, kind))
    return sorted(out)


async def _drain_node(node: str, clustered: bool, skip: Set[int]) -> None:
    """Move every running non-HA guest off this host: live-migrate to another
    cluster node when possible, otherwise shut it down gracefully (forced only
    if that times out) and remember it so maintenance-off starts it again."""
    peers: List[str] = []
    if clustered:
        rows = await _pvesh_json("/cluster/status") or []
        peers = [r["name"] for r in rows if r.get("type") == "node"
                 and r.get("name") != node and r.get("online") in (1, True)]
    shut = _load_shutdown()
    for i, (vmid, kind) in enumerate(await _running_guests(node, skip)):
        tool = "qm" if kind == "qemu" else "pct"
        if peers:
            rc, _, _ = await _run(
                [tool, "migrate", str(vmid), peers[i % len(peers)],
                 "--online" if kind == "qemu" else "--restart"],
                check=False, timeout=1800)
            if rc == 0:
                logger.warning("maintenance: %s %s migrated away", kind, vmid)
                continue
        rc, _, _ = await _run([tool, "shutdown", str(vmid), "--timeout", str(_SHUTDOWN_TIMEOUT_S)],
                              check=False, timeout=_SHUTDOWN_TIMEOUT_S + 60)
        if rc != 0:
            await _run([tool, "stop", str(vmid)], check=False, timeout=120)
        logger.warning("maintenance: %s %s could not move — shut down", kind, vmid)
        shut.append({"vmid": vmid, "kind": kind})
        _save_shutdown(shut)


async def _restart_shutdown_guests() -> List[int]:
    """Start the guests maintenance shut down; any that fail to start stay recorded."""
    left: List[Dict[str, Any]] = []
    started: List[int] = []
    for g in _load_shutdown():
        tool = "qm" if g.get("kind") == "qemu" else "pct"
        rc, _, _ = await _run([tool, "start", str(g["vmid"])], check=False, timeout=120)
        if rc == 0:
            started.append(g["vmid"])
        else:
            left.append(g)
    _save_shutdown(left)
    return started


async def node_maintenance(action: str) -> Dict[str, Any]:
    """Maintenance mode for THIS host: ``action`` is enable, disable or status.

    Enable: on a cluster the HA stack migrates the HA guests away
    (``ha-manager node-maintenance``); a background drain then live-migrates every
    other running guest, and shuts down gracefully the ones that cannot move
    (a standalone host shuts everything down). Disable clears HA maintenance and
    starts the guests shut down for it. Always answers with the current state:
    ``{status, node, maintenance, ha_guests, local_guests, draining}`` — empty
    ``ha_guests`` and ``local_guests`` means safe to reboot."""
    global _drain_task
    # Reject unknown actions.
    if action not in ("enable", "disable", "status"):
        return {"status": "ERROR", "message": f"unknown maintenance action {action!r}"}
    node = await local_node_name()
    clustered = bool(await cluster_info())
    if action != "status" and clustered:
        rc, _, err = await _run(["ha-manager", "crm-command", "node-maintenance", action, node],
                                check=False, timeout=30)
        if rc != 0:
            return {"status": "ERROR", "node": node,
                    "message": f"ha-manager node-maintenance {action} failed: "
                               f"{err.decode('utf-8', 'replace').strip()[:300]}"}
    ms, ha_here, managed = await _ha_state(node) if clustered else ({}, [], set())
    if action == "enable" and not (_drain_task and not _drain_task.done()):
        _drain_task = asyncio.ensure_future(_drain_node(node, clustered, managed))
    restarted: List[int] = []
    if action == "disable":
        if _drain_task and not _drain_task.done():
            _drain_task.cancel()
        _drain_task = None
        restarted = await _restart_shutdown_guests()
    draining = bool(_drain_task and not _drain_task.done())
    local = [] if action == "disable" else [f"{k}:{v}" for v, k in await _running_guests(node, managed)]
    maint = ((ms.get("node_status") or {}).get(node) == "maintenance") if clustered else (
        action == "enable" or bool(_load_shutdown()))
    return {"status": "SUCCESS", "node": node, "maintenance": maint, "ha_guests": ha_here,
            "local_guests": local, "draining": draining, "restarted": restarted}


# A Proxmox node name as the hub may send it (a hostname label, no shell metacharacters).
_NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")


def valid_node_name(node: Any) -> bool:
    """True when ``node`` is a string that looks like a Proxmox node name."""
    return isinstance(node, str) and bool(_NODE_NAME_RE.fullmatch(node))


def ha_maintenance_argv(node: str, enable: bool) -> List[str]:
    """The ``ha-manager`` argv that puts ``node`` into (or takes it out of) HA
    maintenance mode. Raises ValueError for a name that fails the node regex."""
    if not valid_node_name(node):
        raise ValueError(f"invalid node name {node!r}")
    return ["ha-manager", "crm-command", "node-maintenance",
            "enable" if enable else "disable", node]


# HA manager status older than this means no CRM master is acting on it (HA
# not configured, or a file left behind after HA resources were removed).
# Proxmox itself calls a status older than 30 s "dead?".
_HA_STATUS_MAX_AGE_S = 120


def maintenance_nodes(status: Any, now: Optional[float] = None) -> Optional[Set[str]]:
    """Node names in HA maintenance mode, from the JSON of
    ``pvesh get /cluster/ha/status/manager_status`` (the API form of
    ``ha-manager status``). A node counts when the CRM marks it
    ``node_status: maintenance`` or its LRM runs in ``mode: maintenance``.
    None (state unknown) unless the status shows an active CRM with HA
    resources: a ``master_node``, a ``node_status`` map, a non-empty
    ``service_status`` and a recent ``timestamp``."""
    # Only an active CRM's status means anything.
    if not isinstance(status, dict):
        return None
    ms = status.get("manager_status")
    if not isinstance(ms, dict) or not ms.get("master_node") \
            or not isinstance(ms.get("node_status"), dict) \
            or not isinstance(ms.get("service_status"), dict) or not ms["service_status"]:
        return None
    try:
        age = (time.time() if now is None else now) - float(ms.get("timestamp"))
    except (TypeError, ValueError):
        return None
    if age > _HA_STATUS_MAX_AGE_S:
        return None
    # Nodes the CRM or their own LRM report in maintenance.
    found = {str(n) for n, st in ms["node_status"].items() if st == "maintenance"}
    lrm_status = status.get("lrm_status")
    if isinstance(lrm_status, dict):
        found |= {str(n) for n, lrm in lrm_status.items()
                  if isinstance(lrm, dict) and lrm.get("mode") == "maintenance"}
    return found


async def ha_maintenance_nodes() -> Optional[Set[str]]:
    """Node names currently in HA maintenance mode; None when that is unknown
    (pvesh failed, or HA is not active on this cluster — see
    :func:`maintenance_nodes`), so callers leave the flag unset."""
    return maintenance_nodes(await _pvesh_json("/cluster/ha/status/manager_status", timeout=8))


async def cluster_node_names() -> Optional[List[str]]:
    """Names of every node in this host's Proxmox cluster (``/cluster/status``),
    or ``/nodes`` on a host where that read fails. None when both reads fail,
    so "membership unreadable" is not reported as "not a member"."""
    rows = await _pvesh_json("/cluster/status")
    names = [str(r.get("name")) for r in (rows if isinstance(rows, list) else [])
             if isinstance(r, dict) and r.get("type") == "node" and r.get("name")]
    if names:
        return names
    rows = await _pvesh_json("/nodes")
    if not isinstance(rows, list):
        return None
    return [str(r.get("node")) for r in rows if isinstance(r, dict) and r.get("node")]


async def _os_update_drain_pending() -> bool:
    """True while this host is in an OS-update maintenance cycle started by
    :func:`node_maintenance`: a guest drain is running, or the shutdown
    record exists (guests waiting to be started again; also when that file
    is unreadable, so the full disable still gets the chance to start them)."""
    if _drain_task and not _drain_task.done():
        return True
    return await asyncio.to_thread(_MAINT_STATE.exists)


async def set_ha_node_maintenance(node: Any, enable: Any) -> Dict[str, Any]:
    """Run ``ha-manager crm-command node-maintenance enable|disable <node>`` for
    any node of this host's cluster (the hub's Hypervisors → Nodes
    "Maintenance" button). Unlike :func:`node_maintenance` this does not drain
    non-HA guests: it only flips the HA flag, which the CRM acts on. The node
    must pass the name regex AND be a member of this cluster; ``enable`` must
    be a real bool.

    Exiting maintenance on THIS host while an OS-update drain is pending goes
    through :func:`node_maintenance` instead, so the drain is cancelled and the
    guests it shut down are started again (a remote node's drain belongs to
    that node's own agent; the OS-update flow always turns it off itself)."""
    # Validate the request before anything goes near a process.
    if not isinstance(enable, bool):
        return {"status": "ERROR", "message": "enable must be true or false"}
    if not valid_node_name(node):
        return {"status": "ERROR", "message": "invalid node name"}
    members = await cluster_node_names()
    if members is None:
        return {"status": "ERROR", "node": node,
                "message": "could not read the Proxmox cluster member list (pvesh failed)"}
    if node not in members:
        return {"status": "ERROR", "node": node,
                "message": f"node {node} is not a member of this Proxmox cluster"}
    # This host leaving an OS-update cycle: full disable (cancel drain, restart
    # guests). Checked before the HA test so guests restart even if HA is down.
    if not enable and await _os_update_drain_pending() and node == await local_node_name():
        res = await node_maintenance("disable")
        if res.get("status") == "SUCCESS":
            res["message"] = (f"{node}: maintenance mode disable requested; OS-update drain "
                              f"cancelled, {len(res.get('restarted') or [])} guest(s) restarted")
        return res
    # HA must be active, or ha-manager would queue a command nobody acts on (until
    # someone adds an HA resource, much later).
    if await ha_maintenance_nodes() is None:
        return {"status": "ERROR", "node": node,
                "message": "HA is not active on this cluster (no HA resources configured, "
                           "or the HA manager status can't be read)"}
    # argv list, no shell: the node name is passed as one literal argument.
    try:
        rc, out, err = await _run(ha_maintenance_argv(node, enable), check=False, timeout=30)
    except (PveError, OSError) as e:
        return {"status": "ERROR", "node": node, "message": f"ha-manager failed: {e}"}
    if rc != 0:
        detail = (err or out).decode("utf-8", "replace").strip()[:300]
        return {"status": "ERROR", "node": node,
                "message": f"ha-manager node-maintenance {'enable' if enable else 'disable'} "
                           f"failed: {detail or f'exit {rc}'}"}
    logger.warning("HA maintenance %s requested for node %s",
                   "enable" if enable else "disable", node)
    return {"status": "SUCCESS", "node": node, "maintenance": enable,
            "message": f"{node}: maintenance mode {'enable' if enable else 'disable'} requested"}


def node_reboot_argv(node: str) -> List[str]:
    """``pvesh create /nodes/<node>/status --command reboot`` as an argv list
    (the Proxmox API reboot, which pvesh forwards to that node). Raises
    ValueError for a bad node name, so it can never reach a process."""
    if not valid_node_name(node):
        raise ValueError(f"invalid node name {node!r}")
    return ["pvesh", "create", f"/nodes/{node}/status", "--command", "reboot"]


async def running_guests_strict(node: str) -> Optional[List[str]]:
    """Running, non-template guests on ``node`` as ``["qemu:101", ...]``; None
    when either list can't be read, so a failed read never looks like "no
    guests" to the reboot check."""
    found: List[str] = []
    for kind in ("qemu", "lxc"):
        rows = await _pvesh_json(f"/nodes/{node}/{kind}")
        if not isinstance(rows, list):
            return None
        found += [f"{kind}:{r.get('vmid')}" for r in rows
                  if isinstance(r, dict) and r.get("status") == "running" and not r.get("template")]
    return sorted(found)


async def reboot_ha_node(node: Any, reboot_local) -> Dict[str, Any]:
    """Reboot a cluster node that is in HA maintenance mode (the hub's
    Hypervisors → Nodes "Reboot" button). Re-checks everything live, right
    before rebooting, and refuses unless: the name is valid and a cluster
    member, the HA status says the node is in maintenance, and no guest is
    still running on it (the HA migration or a manual drain isn't finished).

    This host is rebooted with ``reboot_local()`` (the agent's privileged
    ``shutdown -r +1``, so the reply gets out first); another node through
    the Proxmox API, ``pvesh create /nodes/<node>/status --command reboot``."""
    # Validate the node.
    if not valid_node_name(node):
        return {"status": "ERROR", "message": "invalid node name"}
    members = await cluster_node_names()
    if members is None:
        return {"status": "ERROR", "node": node,
                "message": "could not read the Proxmox cluster member list (pvesh failed)"}
    if node not in members:
        return {"status": "ERROR", "node": node,
                "message": f"node {node} is not a member of this Proxmox cluster"}
    # It must be in HA maintenance mode right now.
    in_maint = await ha_maintenance_nodes()
    if in_maint is None:
        return {"status": "ERROR", "node": node,
                "message": "refusing to reboot: the HA status can't be read"}
    if node not in in_maint:
        return {"status": "ERROR", "node": node,
                "message": f"refusing to reboot: {node} is not in HA maintenance mode"}
    # No guest may still be running on it.
    guests = await running_guests_strict(node)
    if guests is None:
        return {"status": "ERROR", "node": node,
                "message": f"refusing to reboot: could not list the guests on {node}"}
    if guests:
        shown = ", ".join(guests[:10]) + (" …" if len(guests) > 10 else "")
        return {"status": "ERROR", "node": node, "running_guests": guests,
                "message": f"refusing to reboot: {len(guests)} guest(s) still running on "
                           f"{node} ({shown}); wait for the migration to finish or shut them down"}
    # Reboot: this host via the privileged local path, any other node via the API.
    if node == await local_node_name():
        res = await reboot_local()
        res = res if isinstance(res, dict) else {"status": "ERROR", "message": "no result"}
        res["node"] = node
        return res
    try:
        rc, out, err = await _run(node_reboot_argv(node), check=False, timeout=60)
    except (PveError, OSError) as e:
        return {"status": "ERROR", "node": node, "message": f"reboot failed: {e}"}
    if rc != 0:
        detail = (err or out).decode("utf-8", "replace").strip()[:300]
        return {"status": "ERROR", "node": node,
                "message": f"reboot of {node} failed: {detail or f'exit {rc}'}"}
    logger.warning("node %s reboot requested (in HA maintenance, no guests running)", node)
    return {"status": "SUCCESS", "node": node, "message": f"{node}: reboot requested"}


async def list_qemu_vms_cluster() -> List[Tuple[int, str, str]]:
    """Every qemu VM across the cluster as ``(vmid, name, node)`` from
    ``/cluster/resources`` (each row carries the owning ``node``). Unlike
    ``list_qemu_vms`` (``qm list``, local node only) this sees peer nodes, so a
    caller can resolve a template NAME to the copy on a SPECIFIC node. Read-only.
    Returns ``[]`` on any failure."""
    # List every VM in the cluster.
    rc, out, _ = await _run(
        ["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"],
        check=False, timeout=20)
    if rc != 0:
        return []
    # Parse the JSON list.
    try:
        rows = json.loads(out.decode("utf-8", "replace") or "[]")
    except Exception:  # noqa: BLE001
        return []
    # Keep the qemu VMs as (vmid, name, node).
    out_vms: List[Tuple[int, str, str]] = []
    for r in (rows if isinstance(rows, list) else []):
        if r.get("type") != "qemu" or r.get("vmid") is None:
            continue
        out_vms.append((int(r["vmid"]), str(r.get("name") or ""), str(r.get("node") or "")))
    return out_vms


async def _batch(action: str, protected: Set[int]) -> Dict[str, Any]:
    """Run a single-VM action (start/stop/snapshot) over every sim VM in ``list_qemu_vmids``.

    Non-sim VMIDs are skipped; per-VM failures are logged and don't abort the
    batch. VMs run in PARALLEL under a Semaphore(4) (was serial — a fleet of
    slow ops stacked linearly), and ``kind="qemu"`` is passed explicitly since
    ``list_qemu_vmids`` only returns qemu guests — skipping the wasted
    pct-status detect probe per VM. Returns ``{action, done, skipped}``.
    """
    # Results, and the single-VM function for this action.
    done: List[int] = []
    skipped: List[int] = []
    fn = {"start_vms": start_vm, "stop_vms": stop_vm,
          "snapshot_vms": snapshot_vm}[action]
    # Split the VMs into sim targets and skipped ones.
    targets: List[int] = []
    for vid in await list_qemu_vmids():
        if not is_sim_vm(vid, protected):
            skipped.append(vid)
        else:
            targets.append(vid)

    # Run at most 4 VMs at a time.
    sem = asyncio.Semaphore(4)

    async def _one(vid: int) -> None:
        """Run the action on one VM, recording success or logging the failure."""
        # Wait for a free slot.
        async with sem:
            try:
                await fn(vid, protected, kind="qemu")
                done.append(vid)
            except (PveError, GuardError) as e:
                logger.warning(f"{action}: vmid {vid} failed: {e}")

    # Run them all, then report sorted results.
    await asyncio.gather(*[_one(vid) for vid in targets])
    done.sort()
    return {"action": action, "done": done, "skipped": skipped}


async def start_vms(protected: Set[int]) -> Dict[str, Any]:
    """Start every sim VM on the host (batch). Returns ``{action, started, skipped}``."""
    # Run the batch and rename "done" to "started".
    r = await _batch("start_vms", protected)
    return {**r, "started": r.pop("done")}


async def stop_vms(protected: Set[int]) -> Dict[str, Any]:
    """Stop every sim VM on the host (batch). Returns ``{action, stopped, skipped}``."""
    # Run the batch and rename "done" to "stopped".
    r = await _batch("stop_vms", protected)
    return {**r, "stopped": r.pop("done")}


async def snapshot_vms(protected: Set[int]) -> Dict[str, Any]:
    """Snapshot every sim VM on the host (batch). Returns ``{action, snapshotted, skipped}``."""
    # Run the batch and rename "done" to "snapshotted".
    r = await _batch("snapshot_vms", protected)
    return {**r, "snapshotted": r.pop("done"),
            "snapshot": f"auto-{time.strftime('%Y%m%d%H%M')}"}


# ── Host-level recovery commands (no vmid guard — non-destructive) ──────────

async def unlock_template(template_ids: List[int]) -> Dict[str, Any]:
    """``qm unlock`` the given template VMIDs. Templates are the *source* images
    (typically < 90000), so the sim guard does NOT apply. Best-effort per ID."""
    # Track which IDs were unlocked or failed.
    unlocked: List[int] = []
    failed: List[int] = []
    # Unlock each template, logging failures.
    for tid in template_ids:
        try:
            await _run(["qm", "unlock", str(int(tid))], check=False)
            unlocked.append(int(tid))
        except (PveError, ValueError) as e:
            logger.warning(f"unlock_template: qm unlock {tid} failed: {e}")
            failed.append(int(tid) if str(tid).lstrip('-').isdigit() else tid)
    # Report the result.
    return {"action": "unlock_template", "unlocked": unlocked, "failed": failed}


async def clear_provision_lock() -> Dict[str, Any]:
    """Kill hung ``qm clone|list`` processes and unlock stuck VMs — the cs bash
    ``clear_provision_lock`` recovery path. Non-destructive; no vmid guard
    (unlock is safe). The cs flock/cooldown state files are owned by the USB
    provision loop (Phase E); they're removed best-effort if present."""
    # Count of processes signalled.
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
    # IDs we unlocked.
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
    # Only needed here.
    import os
    for p in ("/var/lib/pxmx/provision.lock",
              "/var/lib/pxmx/provision_halt.cache",
              "/var/lib/pxmx/provision_cooldown_reset"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    # Report what was killed and unlocked.
    return {"action": "clear_provision_lock", "killed_qm_pids": killed,
            "unlocked_vmids": unlocked}


async def clear_usb_quarantine(bus_path: Optional[str] = None) -> Dict[str, Any]:
    """Clear USB dongle-quarantine state for a bus (or all). The quarantine
    store is populated by the USB provision loop (Phase E); until then this
    is a safe no-op that ensures the store is empty/cleared on demand."""
    # Only needed here.
    import json as _json
    import os
    # Quarantine store path.
    path = "/var/lib/pxmx/usb_quarantine.json"
    # Any write failure is logged and reported.
    try:
        # Make sure the directory exists.
        os.makedirs("/var/lib/pxmx", exist_ok=True)
        if bus_path:
            # Clear one bus entry, preserve the rest.
            # Load the current store, treating a bad file as empty.
            data: Dict[str, Any] = {}
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path) as f:
                    try:
                        data = _json.load(f) or {}
                    except _json.JSONDecodeError:
                        data = {}
            # Drop this bus and save the rest.
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
    # Split each "key: value" line on the first colon.
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    # Return the parsed config.
    return out


async def qm_config(vmid: int) -> Dict[str, str]:
    """Read-only ``qm config <vmid>`` → dict. No guard (read-only)."""
    # Read the config; empty dict on failure.
    rc, out, _ = await _run(["qm", "config", str(vmid)], check=False, timeout=20)
    if rc != 0:
        return {}
    # Parse the key/value lines.
    return _parse_kv(out.decode())


async def pct_config(vmid: int) -> Dict[str, str]:
    """Read-only ``pct config <vmid>`` → dict. No guard (read-only)."""
    # Read the config; empty dict on failure.
    rc, out, _ = await _run(["pct", "config", str(vmid)], check=False, timeout=20)
    if rc != 0:
        return {}
    # Parse the key/value lines.
    return _parse_kv(out.decode())


async def qm_set(vmid: int, *args: str, protected: Optional[Set[int]] = None) -> None:
    """``qm set <vmid> <args...>`` — best-effort config mutation (guarded)."""
    # Refuse non-sim VMs, then apply the settings.
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["qm", "set", str(vid), *args], check=False, timeout=30)


async def pct_set(vmid: int, *args: str, protected: Optional[Set[int]] = None) -> None:
    """``pct set <vmid> <args...>`` — best-effort config mutation (guarded)."""
    # Refuse non-sim VMs, then apply the settings.
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["pct", "set", str(vid), *args], check=False, timeout=30)


async def qm_clone(template: Any, vmid: Any, name: str, *,
                   protected: Optional[Set[int]] = None, pool: Optional[str] = None,
                   timeout: int = 600) -> None:
    """``qm clone <template> <vmid> --name <name> [--pool <pool>]``. Template is
    the source (below the sim floor, unguarded); the target vmid is guarded.

    ``pool`` places the new VM in a Proxmox resource pool at CLONE time — the
    only moment it can be set in one shot (``qm set`` has no --pool; retrofitting
    an existing VM needs ``pvesh set /pools/<id>``, see :func:`pool_add_vms`)."""
    # Refuse a non-sim target VMID.
    vid = assert_sim_vm(vmid, protected or set())
    # Clone command, with the pool if given.
    argv = ["qm", "clone", str(int(template)), str(vid), "--name", name]
    if pool:
        argv += ["--pool", str(pool)]
    # Run the clone.
    await _run(argv, timeout=timeout)


# NOTE: pool LISTING lives in vm_inventory.list_pools (via the Proxmox API,
# already strict about requiring a dict with a poolid). A second shell-based
# implementation here shadowed the existing PXMX_LIST_POOLS handler and parsed a
# pvesh schema payload into fake pools named "command"/"poolid". Do not re-add
# one — reuse vm_inventory.list_pools.


async def pool_add_vms(pool: str, vmids: List[int]) -> Dict[str, Any]:
    """Add existing VMs to a resource pool (``pvesh set /pools/<id> --vms a,b``).

    Used to retrofit VMs that were cloned before a pool was configured. Proxmox
    errors if a VM already belongs to ANOTHER pool, so those are reported rather
    than silently skipped — moving a VM between pools is an operator decision."""
    # Clean inputs; nothing to do without a pool and VMs.
    pool = str(pool or "").strip()
    vms = [int(v) for v in (vmids or [])]
    if not pool or not vms:
        return {"status": "SUCCESS", "added": 0, "message": "nothing to do"}
    # Add the VMs to the pool.
    rc, out, err = await _run(
        ["pvesh", "set", f"/pools/{pool}", "--vms", ",".join(str(v) for v in vms)],
        check=False, timeout=60)
    # Report errors with the command's output.
    if rc != 0:
        return {"status": "ERROR", "added": 0,
                "message": (err or out or b"").decode("utf-8", "replace")[:300]}
    # Success: report how many were added.
    return {"status": "SUCCESS", "added": len(vms)}


async def pct_clone(source: Any, vmid: Any, hostname: str, *,
                    protected: Optional[Set[int]] = None, timeout: int = 600) -> None:
    """``pct clone <source> <vmid> --hostname <hostname>``. Source unguarded."""
    # Refuse a non-sim target, then clone.
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["pct", "clone", str(int(source)), str(vid), "--hostname", hostname],
               timeout=timeout)


async def qm_start(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Guarded ``qm start <vmid>`` (qemu). Used by the provision flow to boot a freshly cloned VM."""
    # Refuse non-sim VMs, then start it.
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["qm", "start", str(vid)], timeout=60)


async def pct_start(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Guarded ``pct start <vmid>`` (lxc). Used by the provision flow to boot a freshly cloned CT."""
    # Refuse non-sim VMs, then start it.
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["pct", "start", str(vid)], timeout=60)


async def _qemu_pid(vmid: int) -> Optional[int]:
    """Best-effort QEMU pid lookup (bash _destroy_guest_only 1768-1777)."""
    # Try the pid files first.
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
    # Then try pgrep patterns.
    for pat in (rf"[[:space:]]{vmid}[[:space:]]", f"qemu.*{vmid}"):
        rc, out, _ = await _run(["pgrep", "-f", pat], check=False, timeout=10)
        for line in out.decode().split():
            if line.isdigit():
                return int(line)
    # Not found.
    return None


async def qm_stop_force(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Tiered force-stop mirroring bash _destroy_guest_only (1763-1785): timeout 5
    → forceStop 1 → timeout 1, then a QEMU pid kill + systemd kill if still
    running. Guarded target."""
    # Refuse non-sim VMs.
    vid = assert_sim_vm(vmid, protected or set())
    # Try progressively harder qm stop options.
    for flag in (["--timeout", "5"], ["--forceStop", "1"], ["--timeout", "1"]):
        await _run(["qm", "stop", str(vid), "--skiplock", *flag], check=False, timeout=30)
    # Still running: kill the QEMU process (or its systemd unit) directly.
    if not await wait_stopped(vid, "qemu", 15):
        pid = await _qemu_pid(vid)
        if pid:
            await _run(["kill", "-9", str(pid)], check=False, timeout=5)
        else:
            await _run(["systemctl", "kill", "--signal=SIGKILL",
                        f"qemu-server@{vid}.service"], check=False, timeout=10)
        await asyncio.sleep(3)
    # Wait for it to stop.
    await wait_stopped(vid, "qemu", 20)


async def qm_destroy(vmid: Any, protected: Optional[Set[int]] = None,
                     timeout: int = 300) -> "tuple[bool, str]":
    """``qm destroy --skiplock --purge --destroy-unreferenced-disks``. On failure,
    emergency-kill QEMU and retry once (bash 1796-1807).

    Returns ``(ok, reason)`` — ``reason`` is ``""`` on success, else the actual
    ``qm`` stderr (or a "config still present after purge" note). Previously this
    dropped stderr (``rc, _, _``), so a failed shed could only be reported as a
    generic "locked/busy or purge timeout" guess; now the real cause is logged +
    surfaced to the delete-gate trace on the auto-prov card."""
    # Refuse non-sim VMs.
    vid = assert_sim_vm(vmid, protected or set())
    # Destroy the VM, its disks and its backup-job entries.
    argv = ["qm", "destroy", str(vid), "--skiplock", "--purge",
            "--destroy-unreferenced-disks"]
    # First attempt; keep the error text.
    rc, _, err = await _run(argv, check=False, timeout=timeout)
    reason = err.decode(errors="replace").strip()
    if rc == 0:
        # Post-destroy gate (bash _wait_guest_gone 1811-1814): don't claim
        # success until the config/disk is actually gone, so a reclone at the
        # same VMID can't race a still-purging disk.
        if await wait_guest_gone(vid, "qemu", 360):
            return True, ""
    elif not await guest_exists(vid, "qemu"):
        return True, ""          # rc!=0 because the VM is ALREADY gone → success
    else:
        logger.warning("destroy VM %s: first `qm destroy` rc=%s: %s",
                       vid, rc, reason or "(no stderr)")
    # Still present → emergency-kill QEMU + retry once (bash 1796-1807).
    pid = await _qemu_pid(vid)
    if pid:
        await _run(["kill", "-9", str(pid)], check=False, timeout=5)
    await _run(["systemctl", "kill", "--signal=SIGKILL",
                f"qemu-server@{vid}.service"], check=False, timeout=10)
    await asyncio.sleep(5)
    rc, _, err = await _run(argv, check=False, timeout=timeout)
    reason = err.decode(errors="replace").strip() or reason
    if rc == 0 and await wait_guest_gone(vid, "qemu", 360):
        return True, ""
    if not await guest_exists(vid, "qemu"):
        return True, ""          # gone after the kill/retry → success
    # Still present: log and report the error.
    msg = reason or f"`qm destroy` exited {rc}; VM {vid} config still present"
    logger.error("destroy VM %s failed: %s", vid, msg)
    return False, msg


async def pct_stop(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """Guarded ``pct stop`` for an lxc sim VM, escalating through ``--force`` → ``--skiplock`` → plain until stopped."""
    # Refuse non-sim VMs.
    vid = assert_sim_vm(vmid, protected or set())
    # Try each stop option until it stops.
    for flag in (["--force"], ["--skiplock"], []):
        await _run(["pct", "stop", str(vid), *flag], check=False, timeout=120)
        if await wait_stopped(vid, "lxc", 10):
            return


async def pct_destroy(vmid: Any, protected: Optional[Set[int]] = None,
                      timeout: int = 300) -> "tuple[bool, str]":
    """``pct destroy`` with the bash fallback ladder (1755-1761).

    Returns ``(ok, reason)`` — the last ``pct`` stderr across the flag ladder on
    failure (previously dropped), so a failed shed reports the real cause."""
    # Refuse non-sim VMs; keep the last error text.
    vid = assert_sim_vm(vmid, protected or set())
    reason = ""
    # Try each destroy option set in turn.
    for flag in (["--skiplock", "--purge", "--force"], ["--purge", "--force"],
                 ["--skiplock", "--purge"], ["--skiplock"], []):
        rc, _, err = await _run(["pct", "destroy", str(vid), *flag],
                                check=False, timeout=timeout)
        if rc == 0:
            # Post-destroy gate (bash _wait_guest_gone 1811-1814).
            if await wait_guest_gone(vid, "lxc", 360):
                return True, ""
            msg = f"`pct destroy` returned 0 but CT {vid} config still present after 360s"
            logger.error("destroy CT %s: %s", vid, msg)
            return False, msg
        # Keep the latest error text.
        reason = err.decode(errors="replace").strip() or reason
        if not await guest_exists(vid, "lxc"):
            return True, ""          # already gone → idempotent success
    # Every option failed.
    logger.error("destroy CT %s: all `pct destroy` variants failed: %s",
                 vid, reason or "(no stderr)")
    return False, reason or "pct destroy failed (all variants)"


async def wait_stopped(vmid: int, kind: str, timeout: int) -> bool:
    """Poll ``qm/pct status`` until the guest is stopped or timeout elapses."""
    # Tool for the guest type and the deadline.
    bin_ = "pct" if kind == "lxc" else "qm"
    deadline = time.time() + timeout
    # Check every 2 seconds until stopped.
    while time.time() < deadline:
        rc, out, _ = await _run([bin_, "status", str(vmid)], check=False, timeout=15)
        if rc == 0 and "running" not in out.decode().lower():
            return True
        await asyncio.sleep(2)
    # Timed out.
    return False


async def qm_guest_exec(vmid: Any, *cmd: str,
                        protected: Optional[Set[int]] = None) -> bool:
    """``qm guest exec <vmid> ...`` — best-effort (guest agent may be absent)."""
    # Refuse non-sim VMs, then run the command in the guest.
    vid = assert_sim_vm(vmid, protected or set())
    rc, _, _ = await _run(["qm", "guest", "exec", str(vid), *cmd],
                          check=False, timeout=30)
    # Success means the qm command worked.
    return rc == 0


async def qm_agent_ping(vmid: Any, protected: Optional[Set[int]] = None,
                        timeout: int = 10) -> bool:
    """Return True if the qemu guest-agent on ``vmid`` responds to ``qm agent ping`` (guest is up + agent live)."""
    # Refuse non-sim VMs, then ping the guest agent.
    vid = assert_sim_vm(vmid, protected or set())
    rc, _, _ = await _run(["qm", "agent", str(vid), "ping"], check=False, timeout=timeout)
    # Responding means up.
    return rc == 0


async def guest_exists(vmid: int, kind: str) -> bool:
    """One-shot: True if the guest's config still exists (``qm/pct config`` rc 0).

    A destroy that fails because the guest is ALREADY GONE (a concurrent shed, a
    prior tick, an admin delete, or a slow purge that finished after the
    wait_guest_gone window) is an IDEMPOTENT SUCCESS — "gone" is the goal state.
    Treating it as a failure was the "did NOT complete but the VM is actually
    deleted" bug: the delete gate then skipped clear_assignment and retried a
    ghost VMID every tick, racking up destroy-fails toward a bogus orphan."""
    # Ask for the config; success means it still exists.
    bin_ = "pct" if kind == "lxc" else "qm"
    rc, _, _ = await _run([bin_, "config", str(vmid)], check=False, timeout=15)
    return rc == 0


async def wait_guest_gone(vmid: int, kind: str, timeout: int = 360) -> bool:
    """Poll until the guest's config/disk is fully gone after a destroy (bash
    ``_wait_guest_gone`` 1811-1814 — 360s default). ``qm/pct config`` returns
    non-zero once the VMID no longer exists, so the caller can't race a
    reclone/clone into a still-purging VMID. Read-only (no guard)."""
    # Tool for the guest type and the deadline.
    bin_ = "pct" if kind == "lxc" else "qm"
    deadline = time.time() + timeout
    # Check every 5 seconds until the config is gone.
    while time.time() < deadline:
        rc, _, _ = await _run([bin_, "config", str(vmid)], check=False, timeout=15)
        if rc != 0:
            return True  # config gone → disk cleanup done
        await asyncio.sleep(5)
    # Timed out.
    return False


async def qm_guest_exec_shell_out(vmid: Any, script: str, *,
                                  exec_timeout: int = 20,
                                  outer_timeout: int = 30,
                                  protected: Optional[Set[int]] = None) -> Optional[str]:
    """Like ``qm_guest_exec_shell`` but returns the guest's STDOUT, not just rc.

    ``qm guest exec`` prints a JSON envelope -- {"exitcode":0,"out-data":"..."} --
    so the guest's actual output has to be pulled out of ``out-data``. Returns
    None when the exec fails or the envelope cannot be parsed; callers treat that
    as "unknown", never as "empty".
    """
    # Refuse non-sim VMs.
    vid = assert_sim_vm(vmid, protected or set())
    # Run the script in the guest and wait for it.
    rc, out, _ = await _run(["qm", "guest", "exec", str(vid),
                             "--timeout", str(exec_timeout), "--",
                             "bash", "-c", script],
                            check=False, timeout=outer_timeout)
    # The exec failed: unknown.
    if rc != 0 or not out:
        return None
    # Pull the output out of the JSON envelope.
    try:
        import json as _json
        return str((_json.loads(out) or {}).get("out-data") or "")
    except Exception:  # noqa: BLE001 — non-JSON envelope: hand back the raw text
        return out


async def qm_guest_exec_shell(vmid: Any, script: str, *,
                              exec_timeout: int = 60,
                              outer_timeout: int = 90,
                              protected: Optional[Set[int]] = None) -> Optional[bool]:
    """Run a ``bash -c <script>`` inside the guest with an explicit PVE
    ``--timeout`` (synchronous — PVE waits for completion before returning).

    Returns True/False for the GUEST script's exit status, or None when the
    result is UNKNOWN (qm itself failed, or the envelope had no exitcode).

    CRITICAL: this used to ``return rc == 0`` — the exit code of the ``qm``
    PROCESS, not of the script inside the guest. ``qm guest exec`` returns 0
    whenever the exec MECHANISM worked and reports the guest's real status in
    its JSON envelope ({"exitcode":N,"exited":1,"out-data":...}). So every
    predicate built on this was permanently True: the hostname audit's
    ``[ "$(hostname)" = "<expected>" ]`` compare always said "matches" and never
    re-stamped a single misnamed clone; the lsusb dongle-presence probe always
    said "present"; every guest-health check always said "healthy". Actions
    fared no better — a hostname stamp that failed INSIDE the guest was recorded
    as successful, which is one way a clone ends up misnamed in the first place.

    Mirrors the bash hostname/override writes (proxmox-agent.sh 2025-2046):
    ``qm guest exec <vmid> --timeout <t> -- bash -c '<script>'``. The explicit
    ``--timeout`` is mandatory — without it PVE defaults to async
    fire-and-forget and the caller races the next step. ``hostnamectl`` is
    deliberately avoided here (D-Bus may be unready post-boot and can hang)."""
    # Refuse non-sim VMs.
    vid = assert_sim_vm(vmid, protected or set())
    # Run the script in the guest and wait for it.
    rc, out, _ = await _run(["qm", "guest", "exec", str(vid),
                             "--timeout", str(exec_timeout), "--",
                             "bash", "-c", script],
                            check=False, timeout=outer_timeout)
    if rc != 0 or not out:
        return None                      # the exec itself failed → UNKNOWN
    # Parse the JSON envelope.
    try:
        import json as _json
        env = _json.loads(out)
    except Exception:  # noqa: BLE001 — non-JSON envelope → unknown, never "ok"
        return None
    # No exit code in the envelope: unknown.
    if not isinstance(env, dict) or env.get("exitcode") is None:
        return None
    # True if the guest script exited 0.
    try:
        return int(env["exitcode"]) == 0
    except (TypeError, ValueError):
        return None


async def vzdump(vmid: Any, dumpdir: str, *, compress: str = "zstd",
                 mode: str = "snapshot", protected: Optional[Set[int]] = None,
                 timeout: int = 1800) -> None:
    """``vzdump <vmid> --compress zstd --mode snapshot --dumpdir <dir>``."""
    # Refuse non-sim VMs, then back it up.
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["vzdump", str(vid), "--compress", compress, "--mode", mode,
                "--dumpdir", dumpdir], timeout=timeout)


async def qmrestore(path: str, vmid: Any, *, force: bool = True,
                    protected: Optional[Set[int]] = None,
                    timeout: int = 600) -> None:
    """``qmrestore <file> <vmid> [--force]``. Target vmid guarded."""
    # Refuse non-sim VMs.
    vid = assert_sim_vm(vmid, protected or set())
    # Restore command, overwriting if force is set.
    args = ["qmrestore", path, str(vid)]
    if force:
        args.append("--force")
    # Run the restore.
    await _run(args, timeout=timeout)


async def qm_template(vmid: Any, protected: Optional[Set[int]] = None) -> None:
    """``qm template <vmid>`` (reseed templating step)."""
    # Refuse non-sim VMs, then convert it to a template.
    vid = assert_sim_vm(vmid, protected or set())
    await _run(["qm", "template", str(vid)], timeout=120)


async def list_backup_storages() -> Dict[str, Any]:
    """This host's Proxmox storages that can hold backups (content=backup) plus
    the node hostname — feeds the Setup → Hypervisors backup-storage dropdown.
    Read-only; best-effort (empty list if pvesm is unavailable)."""
    # This host's name and the storage list.
    import socket
    host = socket.gethostname()
    storages: List[str] = []
    # Parallel name→type map (pvesm status cols: name type status total used
    # avail). Lets callers filter non-file storages (e.g. ``pbs`` — vzdump-to-PBS
    # pushes dedup chunks, not a single streamable .vma.zst). Existing consumers
    # that only read ``storages`` (names) are unaffected.
    storage_types: Dict[str, str] = {}
    # Ask for storages that can hold backups.
    rc, out, _ = await _run(["pvesm", "status", "--content", "backup"],
                            check=False, timeout=15)
    if rc == 0:
        # Record each storage's name and type, once each.
        for line in out.decode(errors="replace").splitlines()[1:]:
            parts = line.split()
            if parts:
                name = parts[0]
                stype = parts[1] if len(parts) > 1 else ""
                if name not in storages:
                    storages.append(name)
                    storage_types[name] = stype
    # Return them for this host.
    return {"hosts": [{"hostname": host, "storages": storages,
                       "storage_types": storage_types}]}


async def list_all_vmids() -> List[int]:
    """All qemu + lxc VMIDs present on this host (read-only). Used by the USB
    provision loop to find free slots and reconcile stale bus state."""
    # Start with the qemu VMIDs.
    ids: List[int] = list(await list_qemu_vmids())
    # Add the container VMIDs.
    rc, out, _ = await _run(["pct", "list"], check=False, timeout=20)
    if rc == 0:
        for line in out.decode().splitlines()[1:]:
            parts = line.split()
            if parts and parts[0].isdigit():
                ids.append(int(parts[0]))
    # Return them all.
    return ids


# ── VNC console (agent-terminates-WSS) ──────────────────────────────────────
#
# The agent runs ON the Proxmox host, so localhost:8006 is the local PVE API
# proxy (cluster-wide). We create a VNC ticket via REST using a Proxmox API
# token, then open the vncwebsocket to localhost:8006 with the SAME
# PVEAPIToken header that created the ticket — Proxmox's /vncwebsocket
# requires the creating token's identity (ticket+port alone are rejected; see
# the upstream reference .scratch-shpe/client-sim/webui-spoke/server.py:6242-
# 6287). The returned px_ws is owned by the caller, which relays frames over
# the existing agent↔spoke↔hub WS legs (multiplexed VNC_* JSON messages).

async def open_vnc_ws(vmid: Any, node: str, kind: str, api_token: str):
    """POST the vncproxy and open the vncwebsocket to localhost:8006.

    Returns ``(px_ws, ticket, port)``. ``px_ws`` is an open websockets
    connection the caller MUST close on teardown. Raises ``PveError`` on any
    failure (the caller emits VNC_ERROR up)."""
    # Imported here so they're only needed when a console is opened.
    import urllib.parse
    import ssl
    import websockets
    import httpx

    # Guest type (default qemu), VMID, node, and the API token header.
    k = (kind or "qemu").lower()
    if k not in ("qemu", "lxc"):
        k = "qemu"
    vid = int(vmid)
    pve_node = (node or "localhost").strip() or "localhost"
    headers = {"Authorization": f"PVEAPIToken={api_token}"}

    # Ask the local API for a VNC proxy ticket.
    vncproxy_url = (
        f"https://localhost:8006/api2/json/nodes/"
        f"{urllib.parse.quote(pve_node, safe='')}/{k}/{vid}/vncproxy"
    )
    async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
        resp = await client.post(vncproxy_url, headers=headers, data={"websocket": 1})
    # A non-200 reply is an error.
    if resp.status_code != 200:
        raise PveError(f"vncproxy returned {resp.status_code}: {resp.text[:200]}")
    # Parse the JSON body.
    try:
        body = resp.json() or {}
    except Exception as exc:
        raise PveError(f"vncproxy bad json: {exc}")
    # It must contain a data object with a ticket and port.
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise PveError("vncproxy returned no data object")
    ticket = str(data.get("ticket") or "").strip()
    port = data.get("port")
    if not ticket or port is None:
        raise PveError("vncproxy did not return ticket+port")

    # TLS context that accepts the host's self-signed certificate.
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    # WebSocket URL with the port and ticket.
    params = urllib.parse.urlencode({"port": port, "vncticket": ticket})
    ws_url = (
        f"wss://localhost:8006/api2/json/nodes/"
        f"{urllib.parse.quote(pve_node, safe='')}/{k}/{vid}/vncwebsocket?{params}"
    )

    # websockets renamed extra_headers → additional_headers (v13+). Probe the
    # signature so this works on either the agent's pinned or a newer lib.
    # Only needed here.
    import inspect
    # Connection options.
    connect_kwargs: Dict[str, Any] = {
        "ssl": ssl_ctx,
        "open_timeout": 20,
        "max_size": None,
        # Disable the websockets library's keepalive PING. Proxmox's
        # /vncwebsocket is a raw binary bridge to QEMU's VNC socket and does
        # NOT reply to WS PING control frames, so the library default
        # ping_interval=20 / ping_timeout=20 sends a PING once the VNC goes
        # idle (~20s after the RFB handshake), Proxmox never PONGs, and the
        # library raises ConnectionClosed — tearing down the console at
        # ~15-20s. RFB has its own liveness via the framebuffer, so no WS
        # keepalive is needed here.
        "ping_interval": None,
    }
    # Pass the auth header under the name this websockets version uses.
    hdr_key = "additional_headers" if "additional_headers" in inspect.signature(
        websockets.connect).parameters else "extra_headers"
    connect_kwargs[hdr_key] = headers

    # Open the WebSocket and return it with the ticket and port.
    px_ws = await websockets.connect(ws_url, **connect_kwargs)
    return px_ws, ticket, int(port)