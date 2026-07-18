"""Resource sampling + cache + auto-delete gate for the USB provisioning brain.

Owns the host-resource ring (1h CPU/mem samples), its on-disk cache
(resource_cache.json), the delete-gate cooldown file (delete_gate.json), and the
in-process gate state the telemetry surfaces (provision-halt, 1h averages, and
the delete-gate decision trace). Extracted from ``usb_provision`` so the
provisioning "brain" (run_provision_loop) is separable from the resource-gate
mechanics it drives.

The brain in ``usb_provision`` reads/writes this module's state by qualified
access (``usb_resource_gate._provision_halt = ...`` etc.) and calls the gate/
sampling functions (re-exported by ``usb_provision`` so existing callers are
unchanged). ``_run_delete_gate`` reaches back into the brain for the config/
tier/tracking helpers via a deferred import (house style).
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from . import usb_state_store

logger = logging.getLogger("PxmxAgent")

# Mirrors usb_provision.PXMLIB (this module owns its JSON files).
PXMLIB = "/var/lib/pxmx"

_RESOURCE_SAMPLE_WINDOW = 3600  # 1h rolling window (cs _RESOURCE_SAMPLE_WINDOW)
DELETE_GATE_COOLDOWN_S = 300    # cs line 3615
DELETE_GATE_FILE = f"{PXMLIB}/delete_gate.json"

# Rolling resource samples pruned to the 1h window: [(ts, pct), ...]. Plain
# lists (NOT deque): the prune below is a slice-assignment (`samples[:] = [...]`)
# which deque does not support — as a deque it raised TypeError every call, so
# CPU never pruned and mem was never recorded (mem_avg stuck None). Lists also
# match the cs original (server.py _cpu_samples/_mem_samples are list[tuple]).
_cpu_samples: List[Tuple[float, float]] = []
_mem_samples: List[Tuple[float, float]] = []
_resource_samples_started: float = 0.0   # epoch of first sample (cs _resource_samples_started)

# Persist the 1h samples so an agent restart doesn't reset the rolling window
# (and with it the provision/delete-gate warmup). Faithful port of the cs
# webui-spoke resource_cache (README: "a spoke restart does not reset the warmup
# countdown"). Without this, frequent agent restarts keep cpu_avg cold and the
# 1h-average delete gate can never warm up to its threshold.
_RESOURCE_CACHE_FILE = f"{PXMLIB}/resource_cache.json"
_RESOURCE_CACHE_SAVE_INTERVAL = 60.0     # persist at most once/min (cs _RESOURCE_CACHE_SAVE_INTERVAL)
_resource_cache_last_saved: float = 0.0
_resource_cache_loaded: bool = False

# In-process brain state, reported up via telemetry (rebuilt each pass). Set by
# run_provision_loop (the brain) via qualified access.
_provision_halt: Optional[Dict[str, Any]] = None

# The 1h-average CPU/mem the delete + provision gates actually ACT ON (from the
# persisted resource ring each tick). Surfaced separately from the spoke's own
# display average so the operator can see BOTH: the CPU 1H tile (display) AND the
# exact number the auto-prov gate decides on.
_cpu_1h_avg: Optional[float] = None
_mem_1h_avg: Optional[float] = None

# Delete-gate decision trace — surfaced every tick so the WebUI can show WHY the
# gate did or didn't shed a VM (the operator couldn't tell before: it silently
# held at cpu_avg < delete-threshold, or skipped on cooldown / no eligible T2).
# Shape: {cpu_avg, cpu_threshold, mem_avg, mem_threshold, threshold_exceeded,
#         cooldown_remaining_s, tracked_usb_vms, reason, last_torn_down}.
_delete_gate: Optional[Dict[str, Any]] = None


def _load_resource_cache() -> None:
    """Restore CPU/mem samples from disk so an agent restart doesn't reset the
    1-hour rolling window. Faithful port of cs webui-spoke ``_load_resource_cache``
    — the original persists to resource_cache.json for exactly this reason. Stale
    samples (outside the 1h window) are dropped on load."""
    global _resource_samples_started
    try:
        if not os.path.exists(_RESOURCE_CACHE_FILE):
            return
        with open(_RESOURCE_CACHE_FILE) as f:
            data = json.load(f)
        cutoff = time.time() - _RESOURCE_SAMPLE_WINDOW
        _cpu_samples[:] = [(float(ts), float(v))
                           for ts, v in (data.get("cpu_samples") or []) if float(ts) >= cutoff]
        _mem_samples[:] = [(float(ts), float(v))
                           for ts, v in (data.get("mem_samples") or []) if float(ts) >= cutoff]
        started = float(data.get("started") or 0)
        _resource_samples_started = started if started > 0 else 0.0
        logger.info("Loaded resource cache: %d CPU / %d mem samples (started %.0fs ago)",
                    len(_cpu_samples), len(_mem_samples),
                    time.time() - _resource_samples_started if _resource_samples_started else 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.debug("Could not load resource cache from %s", _RESOURCE_CACHE_FILE, exc_info=True)


def _save_resource_cache(force: bool = False) -> None:
    """Persist CPU/mem samples so the 1-hour window survives restarts. Throttled
    to once per minute (cs _RESOURCE_CACHE_SAVE_INTERVAL); atomic replace so a
    crash mid-write can't leave a truncated cache."""
    global _resource_cache_last_saved
    now = time.time()
    if not force and (now - _resource_cache_last_saved) < _RESOURCE_CACHE_SAVE_INTERVAL:
        return
    _resource_cache_last_saved = now
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        tmp = _RESOURCE_CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"cpu_samples": _cpu_samples,
                       "mem_samples": _mem_samples,
                       "started": _resource_samples_started}, f)
        os.replace(tmp, _RESOURCE_CACHE_FILE)
    except OSError:
        logger.debug("Could not save resource cache to %s", _RESOURCE_CACHE_FILE, exc_info=True)


async def sample_resources(agent) -> None:
    """Append a CPU + memory sample to the rolling 1h lists, sourced from the
    SAME Proxmox node figures the CS telemetry displays (``get_node_stats`` →
    /cluster/resources: ``cpu_usage`` + ``mem_pct``). Called once per
    ``_usb_provision_loop`` tick. Best-effort — a failure leaves the sample
    lists untouched (averages degrade to None/cold-start, which the gate treats
    as "no data yet → don't block", matching the card's "applies only after a
    full hour" help text). Samples are persisted (resource_cache.json) so a
    restart doesn't reset the 1h window — the cache is lazy-loaded on first call.

    Why Proxmox not psutil: the gate used to read ``psutil.virtual_memory()`` /
    ``cpu_percent`` (the agent OS view), but the user-visible CPU/Mem 1h tiles
    read Proxmox's own node stats. On a Proxmox host the two diverge — esp.
    memory, where psutil counts VM RAM + page cache as "used" and routinely
    reads 80%+ while Proxmox's ``mem_used`` reads far lower — so the gate fired
    "resource gate" while the card showed low load. Sourcing the gate from the
    same Proxmox figures makes "below threshold" mean below threshold. Uses
    ``nodes[0]`` to mirror ``_cs_telemetry_body`` so the gate sees exactly what
    the card renders."""
    global _resource_samples_started, _resource_cache_loaded
    if not _resource_cache_loaded:
        _resource_cache_loaded = True
        _load_resource_cache()
    try:
        stats = await agent.get_node_stats()
        nodes = (stats or {}).get("nodes", []) or []
        if not nodes:
            return
        n = nodes[0]
        cpu_pct = float(n.get("cpu_usage", 0) or 0)
        mem_pct = float(n.get("mem_pct", 0) or 0)
        now = time.time()
        cutoff = now - _RESOURCE_SAMPLE_WINDOW
        if not _resource_samples_started:
            _resource_samples_started = now
        _cpu_samples.append((now, cpu_pct))
        _cpu_samples[:] = [(ts, v) for ts, v in _cpu_samples if ts >= cutoff]
        _mem_samples.append((now, mem_pct))
        _mem_samples[:] = [(ts, v) for ts, v in _mem_samples if ts >= cutoff]
        _save_resource_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("sample_resources failed: %s", exc)


async def _current_cpu_pct(agent) -> Optional[float]:
    """Fresh CPU% from the same Proxmox node-stats source as sample_resources
    (not /proc/stat, not psutil — see sample_resources' docstring for why).
    Used only by the reclone-concurrency pacing gate to recheck load between
    staggered clone starts within a single provisioning batch."""
    try:
        stats = await agent.get_node_stats()
        nodes = (stats or {}).get("nodes", []) or []
        return float(nodes[0].get("cpu_usage", 0) or 0) if nodes else None
    except Exception:
        return None


def _resource_1h_average(samples: List[Tuple[float, float]]) -> Optional[float]:
    if not samples:
        return None
    cutoff = time.time() - _RESOURCE_SAMPLE_WINDOW
    recent = [v for ts, v in samples if ts >= cutoff]
    return (sum(recent) / len(recent)) if recent else None


def cpu_samples() -> List[Tuple[float, float]]:
    """The live CPU sample ring (the brain reads it to compute its own averages)."""
    return _cpu_samples


def mem_samples() -> List[Tuple[float, float]]:
    """The live mem sample ring (the brain reads it to compute its own averages)."""
    return _mem_samples


def set_1h_averages(cpu_avg: Optional[float], mem_avg: Optional[float]) -> None:
    """Publish the exact 1h averages the gates decide on (rounded like the delete
    gate does), so the WebUI shows what auto-prov acts on. Called by the brain
    each tick and by ``_run_delete_gate``."""
    global _cpu_1h_avg, _mem_1h_avg
    _cpu_1h_avg = round(cpu_avg, 1) if cpu_avg is not None else None
    _mem_1h_avg = round(mem_avg, 1) if mem_avg is not None else None


def current_provision_halt() -> Optional[Dict[str, Any]]:
    """Agent-computed resource halt (``{halted, reason}`` or ``None``) for the
    telemetry body. Set by ``run_provision_loop`` when cpu/mem cross the
    provision threshold (cs ``proxmox-agent.sh`` 2648-2666 writes the cache)."""
    return _provision_halt


def current_delete_gate() -> Optional[Dict[str, Any]]:
    """The delete-gate decision trace (cpu_avg vs delete threshold, cooldown,
    eligible-candidate count, and the human reason it did/didn't shed a VM) so
    the WebUI can show what auto-prov is deciding on. None until the first pass."""
    return _delete_gate


def current_gate_averages() -> Dict[str, Any]:
    """The 1h averages the gates ACT on (distinct from the spoke's display avg)
    so the UI can show what the auto-prov decision uses."""
    return {"cpu_1h_avg": _cpu_1h_avg, "mem_1h_avg": _mem_1h_avg}


def _load_delete_gate_cooldown() -> float:
    try:
        if os.path.exists(DELETE_GATE_FILE) and os.path.getsize(DELETE_GATE_FILE) > 0:
            with open(DELETE_GATE_FILE) as f:
                return float(json.load(f).get("until") or 0.0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return 0.0


def _save_delete_gate_cooldown(until: float) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(DELETE_GATE_FILE, "w") as f:
            json.dump({"until": float(until)}, f)
    except OSError:
        pass


async def _run_delete_gate(agent, usb_cfg: Dict[str, Any]) -> Optional[int]:
    """Resource delete gate — shed the newest T2 (USB) sim VM when the 1h-avg
    CPU or mem is over the delete threshold and not in the post-delete cooldown.

    Runs on EVERY tick whenever auto-provision is ON, and is called BEFORE the
    provisioning preconditions (dongle_vidpids / templates) so a provisioning
    config gap can never disable the safety shed. Self-contained: loads + saves
    its own usb state, and publishes the ``_delete_gate`` decision trace + the
    gate's 1h averages so the WebUI can show what it decided on and WHY. Returns
    the torn-down VMID (so the caller can count it) or None.

    ONLY the resource gate lives here — the missing-dongle teardown stays in the
    main loop (it needs the live dongle scan, which needs dongle_vidpids)."""
    global _delete_gate
    from . import cs_sim, pve_cmds, usb_provision  # deferred — cs_sim/usb_provision cycle
    now = time.time()
    cpu_avg = _resource_1h_average(_cpu_samples)
    mem_avg = _resource_1h_average(_mem_samples)
    set_1h_averages(cpu_avg, mem_avg)
    cpu_del_thr = usb_provision._pct_setting(usb_cfg, "cpu_delete_threshold", 90)
    mem_del_thr = usb_provision._pct_setting(usb_cfg, "mem_delete_threshold", 90)
    state = usb_state_store.load_usb_state()
    cooldown_until = _load_delete_gate_cooldown()
    threshold_exceeded = (
        (cpu_avg is not None and cpu_avg >= cpu_del_thr)
        or (mem_avg is not None and mem_avg >= mem_del_thr)
    )
    target_vmid = None      # the VM actually shed (None if none / destroy failed)
    destroy_failed = None    # a VMID we tried to shed but destroy_vm returned not-ok
    destroy_fail_reason = ""  # the actual qm/pct stderr for that failure (card trace)
    ghost_cleaned = None     # a VMID that was already gone when we went to shed it
    candidate_count = None  # eligible T2 count (only computed when the gate runs)
    if threshold_exceeded and now >= cooldown_until:
        provisioning = usb_provision._provisioning_vmids()
        # Refresh the tracked list against the LIVE host VM list BEFORE selecting:
        # a VM deleted by ANY path (a prior shed, an admin/manual delete, a crash)
        # is dropped from bus_to_vmid here, so the gate can't fixate on a ghost
        # VMID and stall — the user-reported "stuck on a VMID shed hours ago,
        # never advances to the next". Prune is by-value so a bus_to_vmid entry
        # stranded after a partial clear is caught (the old vmid_to_bus-only
        # reconcile missed it). Reload state after a prune.
        _ghosts = usb_state_store.prune_ghost_vms(set(await pve_cmds.list_all_vmids()))
        if _ghosts:
            logger.info("delete gate: pruned ghost VMID(s) no longer on host "
                        "(shed/deleted earlier): %s", _ghosts)
            state = usb_state_store.load_usb_state()
        # Candidates = dongle-backed VMs (bus_to_vmid), newest first. Shed ONLY a
        # pure T2: skip templates and any VM with a protecting T1/T3 PCI
        # passthrough (destroy_vm also refuses templates at the choke point).
        candidates = sorted({int(v) for v in state["bus_to_vmid"].values()
                             if str(v).lstrip("-").isdigit() and int(v) not in provisioning})
        candidate_count = len(candidates)
        protected_pci = usb_provision._t1_pci_vidpids(agent) | usb_provision._t3_pci_vidpids(agent)
        bus = None
        for _cand in sorted(candidates, reverse=True):
            if await pve_cmds.is_template(_cand):
                logger.info("delete gate: skipping template VMID %s (never torn down)", _cand)
                continue
            if protected_pci:
                _pci = await pve_cmds.pci_passthrough_vidpids(_cand)
                if _pci & protected_pci:
                    logger.info("delete gate: skipping VMID %s — protected PCI passthrough %s "
                                "(T1/T3, never torn down)", _cand, sorted(_pci & protected_pci))
                    continue
            target_vmid = _cand
            bus = state["bus_to_vmid"].get(str(_cand))
            break
        if target_vmid is not None:
            _cand_vmid = target_vmid
            # Stamp the transient "deleting" state BEFORE the destroy so the next
            # telemetry frame surfaces it; dropped if the destroy doesn't succeed.
            usb_provision._deleting[int(_cand_vmid)] = now
            try:
                result = await cs_sim.destroy_vm(agent, _cand_vmid, bus=bus)
            except Exception as e:  # noqa: BLE001 — GuardError (template/protected) etc.
                result = {"ok": False, "error": str(e)}
                logger.warning("auto-provision delete gate: destroy of %s raised: %s",
                               _cand_vmid, e)
            # CRITICAL: destroy_vm returns {"ok": bool} and only RAISES on a guard
            # (template/protected) — a plain destroy failure (VM locked/busy,
            # wait_guest_gone timeout, disk still purging) returns ok=False WITHOUT
            # raising. Arming the cooldown on ok=False was the "went into cooldown
            # but the VM is still there" bug: it masked the failure as a success
            # for 300s. Only arm the cooldown + count the shed when ok is True.
            # destroy_vm.clear_assignment already dropped the bus assignment from
            # persisted state on success (and on orphan) — the gate no longer
            # double-manages state.
            if result.get("ok") and result.get("already_gone"):
                # The VM was already gone (deleted earlier / a race after the
                # prune). destroy_vm cleared its state. Do NOT arm the cooldown —
                # nothing was actually shed, so the real over-threshold shed
                # should proceed next tick instead of idling 300s on a no-op.
                usb_provision._deleting.pop(int(_cand_vmid), None)
                target_vmid = None
                ghost_cleaned = _cand_vmid
                logger.info(
                    "auto-provision delete gate: VMID %s already gone — cleared "
                    "stale assignment, no cooldown (real shed proceeds)", _cand_vmid)
            elif result.get("ok"):
                cooldown_until = now + DELETE_GATE_COOLDOWN_S
                _save_delete_gate_cooldown(cooldown_until)
                logger.warning(
                    "auto-provision delete gate: destroyed newest USB VM %s "
                    "(cpu_avg=%.1f mem_avg=%.1f) — 300s cooldown",
                    _cand_vmid, cpu_avg or 0.0, mem_avg or 0.0)
            else:
                # NOT destroyed — do NOT arm the cooldown (retry next tick).
                # destroy_vm tracks its own fail count → orphan after
                # DESTROY_MAX_FAILS, which releases the bus.
                usb_provision._deleting.pop(int(_cand_vmid), None)
                target_vmid = None
                destroy_failed = _cand_vmid
                destroy_fail_reason = result.get("reason") or ""
                logger.warning(
                    "auto-provision delete gate: VMID %s did NOT destroy "
                    "(fails=%s reason=%s) — no cooldown, retrying next tick",
                    _cand_vmid, result.get("fails"), destroy_fail_reason or "(none)")

    # Decision trace → telemetry → WebUI (what it decided on + WHY it did/didn't
    # shed — previously invisible; it just silently held under threshold). Reload
    # state so tracked reflects destroy_vm's own clear_assignment.
    state = usb_state_store.load_usb_state()
    tracked = len(state.get("bus_to_vmid") or {})
    _cpu_s = f"{round(cpu_avg, 1)}" if cpu_avg is not None else "—"
    _mem_s = f"{round(mem_avg, 1)}" if mem_avg is not None else "—"
    if target_vmid is not None:
        reason = f"shed VMID {target_vmid} (CPU {_cpu_s}% ≥ {cpu_del_thr}%)"
    elif ghost_cleaned is not None:
        reason = (f"cleared stale VMID {ghost_cleaned} (already deleted) — "
                  f"real shed proceeds next tick, no cooldown")
    elif destroy_failed is not None:
        _why = f": {destroy_fail_reason}" if destroy_fail_reason else \
               " (VM locked/busy or purge timeout; see agent log)"
        reason = (f"over threshold — tried to shed VMID {destroy_failed} but the "
                  f"destroy did NOT complete{_why} — retrying, no cooldown armed")
    elif not threshold_exceeded:
        reason = (f"holding — CPU {_cpu_s}%/{cpu_del_thr}% · Mem {_mem_s}%/"
                  f"{mem_del_thr}% (1h avg under delete threshold)")
    elif now < cooldown_until:
        reason = f"over threshold but in cooldown ({int(cooldown_until - now)}s left)"
    elif (candidate_count or 0) == 0:
        reason = (f"over threshold but NO eligible T2 VMs to shed "
                  f"({tracked} USB VM(s) tracked in bus_to_vmid)")
    else:
        reason = ("over threshold but all candidates are templates or "
                  "protected T1/T3 (never torn down)")
    _delete_gate = {
        "cpu_avg": round(cpu_avg, 1) if cpu_avg is not None else None,
        "cpu_threshold": cpu_del_thr,
        "mem_avg": round(mem_avg, 1) if mem_avg is not None else None,
        "mem_threshold": mem_del_thr,
        "threshold_exceeded": threshold_exceeded,
        "cooldown_remaining_s": max(0, int(cooldown_until - now)),
        "tracked_usb_vms": tracked,
        "eligible_candidates": candidate_count,
        "reason": reason,
        "last_torn_down": [target_vmid] if target_vmid is not None else [],
    }
    return target_vmid
