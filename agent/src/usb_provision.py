"""Client-Simulation USB dongle blacklist + orphan-VM tracking for the unified
pxmx agent.

Ports two host-side safety pieces from ``cs/proxmox/proxmox-agent.sh`` (the
full USB provisioning loop arrives in Phase E):

1. **Dongle-driver blacklist** (bash ``blacklist_dongle_drivers``, lines
   1415-1484) — scans ``/sys/bus/usb/devices`` for USB devices whose ``vid:pid``
   is in the configured dongle set, finds the kernel driver bound to each
   interface child, writes a stable ``/etc/modprobe.d/cs-dongle-blacklist.conf``,
   runs ``depmod -a``, and ``rmmod``s the currently-loaded drivers. This keeps
   the host kernel from grabbing dongles (WiFi adapters etc.) that must be
   passed through to sim VMs. The driver set is *discovered*, not hardcoded.

2. **Orphan-VM registry** (bash ``increment_destroy_fail_count`` etc., lines
   1319-1371) — when a VM destroy fails ``DESTROY_MAX_FAILS`` (3) times, the VM
   is declared an orphan: the bus is force-released for re-provisioning and the
   VMID is appended to ``/var/lib/pxmx/orphan_vms.json``. The call sites
   (destroy success/failure) arrive with Phase E's destroy path; this module
   provides the registry helpers now so Phase E just calls them.
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("PxmxAgent")

PXMLIB = "/var/lib/pxmx"
DONGLE_BLACKLIST_CONF = "/etc/modprobe.d/cs-dongle-blacklist.conf"

# Bus-map + orphan-registry persistence lives in usb_state_store; the dongle
# quarantine + destroy-fail + bus-exclusion state lives in usb_quarantine. Both
# are re-exported here so existing usb_provision.X callers (and this module's own
# unqualified calls in the provisioning brain) are unchanged. usb_state_store
# owns USB_STATE_FILE / ORPHAN_VMS_FILE; usb_quarantine owns USB_QUARANTINE_FILE
# / DESTROY_FAILS_FILE.
from .usb_state_store import (  # noqa: E402
    ORPHAN_VMS_FILE, USB_STATE_FILE,
    _read_orphans, _write_orphans, add_orphan_vm, remove_orphan_vm,
    get_orphan_vms, _new_usb_state, load_usb_state, save_usb_state,
    clear_assignment, prune_ghost_vms, set_assignment, reconcile_bus_map,
    bus_for_vmid,
)
from .usb_quarantine import (  # noqa: E402
    USB_QUARANTINE_FILE, DESTROY_FAILS_FILE, QUARANTINE_MAX_FAILS,
    QUARANTINE_RECOVERY_S, DESTROY_MAX_FAILS, QUARANTINE_PERMANENT_STRIKES,
    _USB_DMESG_ERROR_RE, _DMESG_USB_WINDOW_S, _DMESG_USB_QUARANTINE_MIN,
    exclude_bus, clear_excluded_buses, clear_quarantine,
    _read_quarantine, _save_quarantine, scan_dmesg_usb_errors, quarantine_bus,
    _read_destroy_fails, _save_destroy_fails, record_destroy_fail,
    clear_destroy_fails,
)
# Resource sampling ring + cache + auto-delete gate live in usb_resource_gate.
# The brain (run_provision_loop) reads/writes its provision-halt + 1h-average
# state by qualified access (usb_resource_gate._provision_halt = ... etc.); the
# gate/sampling functions are re-exported so existing callers are unchanged.
from . import usb_resource_gate  # noqa: E402
from .usb_resource_gate import (  # noqa: E402
    sample_resources, _current_cpu_pct, _resource_1h_average,
    current_provision_halt, current_delete_gate, current_gate_averages,
    _run_delete_gate, _load_delete_gate_cooldown, _save_delete_gate_cooldown,
    _load_resource_cache, _save_resource_cache,
)

_VIDPID_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")


# ── usb_config readers (cs-spoke schema) ───────────────────────────────────
# The cs speak (relayed to this agent verbatim by the LM hub's cs_bridge) publishes
# ``client_simulation.usb_config`` with ``usb_vidpids`` = a JSON array of
# ``{vidpid, type, label}`` dicts and ``usb_ignored_vidpids`` = a JSON array of
# bare lowercased vidpid strings. These helpers read that schema (with legacy
# ``dongle_vidpids`` / ``certified_types`` fallbacks for non-LM-managed agents).

def _parse_vidpid_items(raw: Any) -> List[Any]:
    """Coerce a usb_vidpids/usb_ignored_vidpids value (JSON string, list, or
    legacy comma string) into a list of items (dicts or bare strings)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    s = str(raw).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return [p.strip() for p in s.split(",") if p.strip()]


def _usb_cfg(agent) -> Dict[str, Any]:
    return (agent.config.get("client_simulation") or {}).get("usb_config") or {}


def _dongle_vidpids(agent) -> Set[str]:
    """The certified dongle VID:PID set (lowercased ``vid:pid``). Reads the
    cs-spoke ``usb_config.vidpids`` array of ``{vidpid,...}`` dicts (the key the
    cs spoke's ``usb_config_payload`` emits), with legacy
    ``usb_vidpids``/``dongle_vidpids``/``certified_types`` fallbacks for older
    spoke builds. Empty until the hub delivers usb_config — the blacklist +
    telemetry classify as no-op/unknown."""
    cfg = _usb_cfg(agent)
    items = _parse_vidpid_items(cfg.get("vidpids"))
    if not items:  # legacy fallbacks (older cs spoke builds)
        items = (_parse_vidpid_items(cfg.get("usb_vidpids"))
                 or _parse_vidpid_items(cfg.get("dongle_vidpids"))
                 or list(cfg.get("certified_types") or []))
    out: Set[str] = set()
    for v in items:
        vp = (v.get("vidpid") if isinstance(v, dict) else v)
        s = str(vp or "").strip().lower()
        if _VIDPID_RE.match(s):
            out.add(s)
    return out


def _certified_types(agent) -> Dict[str, str]:
    """``{vidpid: type}`` from the certified list (default ``wireless``). Reads
    the cs-spoke ``usb_config.vidpids`` array (legacy ``usb_vidpids`` fallback)."""
    cfg = _usb_cfg(agent)
    items = _parse_vidpid_items(cfg.get("vidpids"))
    if not items:  # legacy fallback (older cs spoke builds)
        items = _parse_vidpid_items(cfg.get("usb_vidpids"))
    out: Dict[str, str] = {}
    for v in items:
        if not isinstance(v, dict):
            continue
        vp = str(v.get("vidpid") or "").strip().lower()
        if _VIDPID_RE.match(vp):
            out[vp] = str(v.get("type") or "wireless")
    return out


def _ignored_vidpids(agent) -> Set[str]:
    """The ignored dongle VID:PID set (lowercased) from the cs-spoke
    ``usb_config.ignored_vidpids`` array (legacy ``usb_ignored_vidpids`` fallback)."""
    cfg = _usb_cfg(agent)
    out: Set[str] = set()
    items = _parse_vidpid_items(cfg.get("ignored_vidpids"))
    if not items:  # legacy fallback (older cs spoke builds)
        items = _parse_vidpid_items(cfg.get("usb_ignored_vidpids"))
    for v in items:
        vp = (v.get("vidpid") if isinstance(v, dict) else v)
        s = str(vp or "").strip().lower()
        if _VIDPID_RE.match(s):
            out.add(s)
    return out


def _pci_vidpid_set(raw: Any) -> Set[str]:
    """Coerce a t1/t3_pci_vidpids config value (JSON array of bare ``vid:pid``
    strings, list, or comma-string) into a lowercased, regex-validated set."""
    out: Set[str] = set()
    for v in _parse_vidpid_items(raw):
        vp = (v.get("vidpid") if isinstance(v, dict) else v)
        s = str(vp or "").strip().lower()
        if _VIDPID_RE.match(s):
            out.add(s)
    return out


def _t1_pci_vidpids(agent) -> Set[str]:
    """T1 PCI-passthrough VID:PID set (configurable, Setup → Proxmox). A VM whose
    hostpci device matches one of these is T1 — never torn down. Empty until the
    hub delivers usb_config."""
    return _pci_vidpid_set(_usb_cfg(agent).get("t1_pci_vidpids"))


def _t3_pci_vidpids(agent) -> Set[str]:
    """T3 PCI-passthrough VID:PID set (configurable, Setup → Proxmox). A VM whose
    hostpci device matches one of these is T3 — never torn down."""
    return _pci_vidpid_set(_usb_cfg(agent).get("t3_pci_vidpids"))


# Cache the per-VM tier map — passthrough rarely changes, and resolving PCI
# (qm config + lspci) for every VM on each ~60s telemetry tick is wasteful.
_vm_tier_cache: Dict[str, Any] = {"ts": 0.0, "tiers": {}}
_VM_TIER_TTL = 60.0


async def compute_vm_tiers(agent, vms) -> Dict[str, str]:
    """Authoritative per-VM tier map ``{str(vmid): 't1'|'t2'|'t3'}``, classified by
    PASSTHROUGH — the reliable signal, independent of any guest self-report:
      * PCI passthrough matching t3_pci_vidpids → ``t3`` (protected)
      * PCI passthrough matching t1_pci_vidpids → ``t1`` (protected)
      * a USB dongle (vmid in usb_state) and no protecting PCI device → ``t2``
    PCI wins over USB so a T1/T3 device is never mislabeled T2. VMs with no
    determinable tier are omitted (the UI keeps its default). Cached ``_VM_TIER_TTL``
    seconds; templates are skipped. Consumed by ``_cs_telemetry_body`` (stamped
    per VM → cs spoke → Clients tab badge)."""
    now = time.time()
    if _vm_tier_cache["tiers"] and (now - _vm_tier_cache["ts"]) < _VM_TIER_TTL:
        return dict(_vm_tier_cache["tiers"])
    from . import pve_cmds
    t1_set = _t1_pci_vidpids(agent)
    t3_set = _t3_pci_vidpids(agent)
    st = load_usb_state()
    usb_vmids = {str(v) for v in (st.get("bus_to_vmid") or {}).values()
                 if str(v).lstrip("-").isdigit()}
    tiers: Dict[str, str] = {}
    # Per-VM PCI resolution runs in PARALLEL under a Semaphore(4) (was serial —
    # qm config per VM stacked linearly each cache refresh). kind comes from
    # v["type"] (already known from the VM list) so pci_passthrough_vidpids
    # skips its detect_guest_type probe, and the addr→vidpid lspci lookups are
    # memoized module-level in pve_cmds (PCI topology is static while running).
    sem = asyncio.Semaphore(4)

    async def _classify(v) -> None:
        vid = v.get("vmid") if isinstance(v, dict) else v
        if vid in (None, ""):
            return
        svid = str(vid)
        tier = None
        if (t1_set or t3_set) and not (isinstance(v, dict) and v.get("is_template")):
            async with sem:
                try:
                    pci = await pve_cmds.pci_passthrough_vidpids(
                        vid, v.get("type") if isinstance(v, dict) else None)
                except Exception:  # noqa: BLE001
                    pci = set()
            if pci & t3_set:
                tier = "t3"
            elif pci & t1_set:
                tier = "t1"
        if tier is None and svid in usb_vmids:
            tier = "t2"
        if tier:
            tiers[svid] = tier

    await asyncio.gather(*[_classify(v) for v in (vms or [])])
    _vm_tier_cache.update({"ts": now, "tiers": tiers})
    return dict(tiers)


# ── auto-provisioning brain (cs webui-spoke/server.py brain-loop port) ────
# The cs spoke's brain gates cloning on the ``usb_auto_provision`` toggle and
# host resource thresholds, and can auto-delete the newest sim VM under load
# (cs ``server.py`` 10020-10294 + ``proxmox-agent.sh`` 2648-2666/5005-5060). In
# the LM topology the cs spoke is only a relay, so the brain runs here, inside
# the pxmx agent's ``run_provision_loop`` (called every ~60s by
# ``_usb_provision_loop``). The hub side (toggle/store/push/status) is already
# complete; this is the missing consumer.

VMID_AUDIT_INTERVAL_S = 300    # cs line 3619
# A dongle bus-excluded on admin VM delete (anti-churn) auto-returns to service
# after this cooldown, so an exclusion is never permanent. Configurable per
# tenant via usb_config usb_exclude_cooldown / exclude_cooldown.
EXCLUDE_COOLDOWN_S = 900

VMID_AUDIT_FILE = f"{PXMLIB}/vmid_audit.json"

# Resource sampling ring + cache + delete-gate cooldown live in
# usb_resource_gate; the brain reads/writes its 1h-average + provision-halt
# state there (see the re-export block above).

# In-process brain state, reported up via telemetry (rebuilt each pass).
_prov_run: Dict[str, Any] = {"running": False, "items": []}

# Stuck-run watchdog: if a provision pass's asyncio.gather never returns (a
# clone/reclaim hung — e.g. destroy_vm blocked on a VM that won't stop), the
# _provision_loop body never advances past `await run_provision_loop`, so
# _provision_loop_last_run goes stale AND _prov_run.running stays True → the
# "prov_run active" gate short-circuits every subsequent tick → permanent
# wedge ("under threshold but not deploying"). When a run is older than this,
# the next pass force-clears it and proceeds. The orphaned gather (still
# pending) writes to its OWN captured run dict (see this_run in
# run_provision_loop), not the global, so it can't clobber the fresh run.
PROV_RUN_STUCK_S = 600.0

# VMIDs currently being torn down by the delete gate, mapped to the epoch the
# destroy was issued. A destroy completes fast and the VM then vanishes from the
# `vms` list, so without this the "deleting" transition is invisible between two
# ~10s telemetry ticks. We stamp the vmid just before destroy and keep it for a
# short TTL (current_deleting_vmids) so at least one telemetry frame surfaces the
# 🔴 deleting state to the WebUI VM list. Mirrors the original's
# usb_state[].prov_status == "tearing_down".
_deleting: Dict[int, float] = {}
_DELETING_TTL_S = 30.0

# Same idea for reclones — VMIDs currently being recloned (destroy + clone +
# boot + guest-agent wait), surfaced to the WebUI VM list as a "Recloning" badge.
# Longer TTL than deleting because a reclone runs for minutes (the guest-agent
# wait alone is up to ~10m); the mark is REFRESHED as the reclone progresses
# (mark_recloning is called at each phase) and cleared on completion, so the TTL
# is only a backstop if the agent dies mid-reclone.
_recloning: Dict[int, float] = {}
_RECLONING_TTL_S = 180.0

# Fleet "Reclone All" batch state — the progress bar on the hub's Fleet Reclone
# tile. One batch at a time (a second Reclone All while one is running is
# rejected by the handler). Shape mirrors the first-version webui-spoke
# ``reclone_state`` (renderRecloneStatus): {status, current_vm, phase, total,
# completed, failed, started_at, type, log:[{vmid,name,status,timestamp}],
# last_run}. ``status`` ∈ running|completed|failed|idle. Empty dict = idle.
# Published to telemetry by ``current_reclone_state`` (mirrors
# ``current_prov_run`` / ``current_reclone_vmids``).
_reclone_state: Dict[str, Any] = {}
# Set by request_reclone_stop(); the fleet-reclone batch loop checks it before
# starting each remaining VM and aborts the rest. Cleared on start_reclone_batch.
_reclone_stop: bool = False

# Auto-provisioning pause during a destructive template refresh
# (agent REFRESH_TEMPLATE): the agent wipes the host's sim VMs + template and
# restores a backup, so the provision loop must NOT clone/teardown/shed while
# that runs. Set by the refresh worker around the sequence.
_refresh_paused = False


def set_refresh_paused(value: bool) -> None:
    global _refresh_paused
    _refresh_paused = bool(value)


def refresh_paused() -> bool:
    return _refresh_paused

# The gate 1h-averages (_cpu_1h_avg/_mem_1h_avg) and the delete-gate decision
# trace (_delete_gate) live in usb_resource_gate (surfaced via its current_*
# accessors, re-exported above).

# Auto-provision diagnostic state — WHY the last pass provisioned nothing (or did).
# Surfaced in CS_TELEMETRY → WebUI Auto-Provisioning card so a silent gate (no
# dongle_vidpids / no template ids / no eligible dongles) is visible in the UI
# without grepping the agent log. ``run_provision_loop`` sets these every tick;
# ``current_*`` accessors feed the telemetry body (mirror _provision_halt/_prov_run).
_provision_reason: Optional[str] = None
_provision_cfg_snapshot: Dict[str, Any] = {}
_provision_loop_last_run: float = 0.0
_auto_provision_on: bool = False


def _normalize_toggle(v: Any) -> str:
    s = str(v or "").strip().lower()
    return "on" if s in ("on", "1", "true", "yes", "enabled") else "off"


def _toggle_on(usb_cfg: Dict[str, Any]) -> bool:
    """The toggle arrives under two key names depending on the cs spoke:
    ``usb_auto_provision`` (webui-spoke 6-key blob) or ``auto_provision``
    (lm-spoke full 27-key payload). Accept either."""
    return (_normalize_toggle(usb_cfg.get("usb_auto_provision")) == "on"
            or _normalize_toggle(usb_cfg.get("auto_provision")) == "on")


def _cfg_first(usb_cfg: Dict[str, Any], keys: tuple, default: Any = None) -> Any:
    """First non-empty value among ``keys`` (union of the two relay schemas)."""
    for k in keys:
        v = usb_cfg.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return default


def _pct_setting(usb_cfg: Dict[str, Any], key: str, default: int) -> int:
    v = usb_cfg.get(key)
    try:
        if v is None or str(v).strip() == "":
            return default
        return max(0, min(100, int(str(v).strip())))
    except (TypeError, ValueError):
        return default


def current_prov_run() -> Dict[str, Any]:
    """Live provision-run state (``{running, items:[{vmid,vidpid,status}]}``)
    for the telemetry body (cs ``_default_provision_run_state`` 3576-3586)."""
    return dict(_prov_run)


def current_deleting_vmids() -> List[int]:
    """VMIDs the delete gate is currently tearing down (TTL-pruned).

    Stamped just before ``destroy_vm`` and kept for ``_DELETING_TTL_S`` so the
    brief deleting window survives at least one telemetry tick; the WebUI VM
    list renders these as 🔴 deleting. Prunes expired entries as a side effect."""
    now = time.time()
    for vmid in [v for v, ts in _deleting.items() if now - ts > _DELETING_TTL_S]:
        _deleting.pop(vmid, None)
    return sorted(_deleting.keys())


def mark_deleting(vmid: int) -> None:
    """Mark/refresh a VMID as currently being torn down. Called from
    ``cs_sim.destroy_vm`` (the shared choke point for the manual ``delete_vm``
    long-op, the reclone flow, and the missing-dongle shed gate) so EVERY
    destroy surfaces a 🔴 deleting telemetry frame — without this an operator's
    mass delete showed the VMs as "running" until qm actually destroyed them
    and the next ~15s telemetry tick dropped them from the list. The shed gate
    also stamps directly, but that's harmless (same vmid, refreshes the ts)."""
    try:
        _deleting[int(vmid)] = time.time()
    except (TypeError, ValueError):
        pass


def mark_recloning(vmid: int) -> None:
    """Mark/refresh a VMID as currently recloning (called at each reclone phase
    so the "Recloning" badge stays live for the minutes a reclone takes)."""
    try:
        _recloning[int(vmid)] = time.time()
    except (TypeError, ValueError):
        pass


def clear_recloning(vmid: int) -> None:
    """Drop a VMID's recloning mark (reclone finished or failed)."""
    try:
        _recloning.pop(int(vmid), None)
    except (TypeError, ValueError):
        pass


def current_reclone_vmids() -> List[int]:
    """VMIDs currently being recloned (TTL-pruned). The WebUI VM list renders
    these as a "Recloning" badge. Prunes expired entries as a side effect."""
    now = time.time()
    for vmid in [v for v, ts in _recloning.items() if now - ts > _RECLONING_TTL_S]:
        _recloning.pop(vmid, None)
    return sorted(_recloning.keys())


# ── Fleet "Reclone All" batch tracker ────────────────────────────────────────
# Mutators are called by cs_sim._reclone_all around each per-VM reclone. The
# accessors feed the telemetry body (current_reclone_state) so the hub's Fleet
# Reclone progress bar advances live. One batch at a time.
def current_reclone_state() -> Dict[str, Any]:
    """Live fleet-reclone batch state for the telemetry body → hub Fleet Reclone
    progress bar. Empty dict when idle (no batch running/just-finished)."""
    return dict(_reclone_state)


def start_reclone_batch(total: int, rtype: str = "manual") -> bool:
    """Begin a fleet-reclone batch. Returns False (refuses) if one is already
    running — the handler treats that as a 409/conflict so a second click while
    a batch is active is rejected, not interleaved."""
    global _reclone_state, _reclone_stop
    if _reclone_state.get("status") == "running":
        return False
    _reclone_stop = False
    _reclone_state = {
        "status": "running",
        "current_vm": None,
        "phase": "",
        "total": int(total or 0),
        "completed": 0,
        "failed": 0,
        "started_at": time.time(),
        "type": rtype or "manual",
        "log": [],
    }
    return True


def request_reclone_stop() -> bool:
    """Signal a running fleet-reclone batch to stop after its in-flight VMs
    finish. Returns False (no-op) when no batch is running."""
    global _reclone_stop
    if _reclone_state.get("status") != "running":
        return False
    _reclone_stop = True
    return True


def reclone_stop_requested() -> bool:
    """True once request_reclone_stop() fired for the current batch."""
    return _reclone_stop


def mark_reclone_progress(vmid: int, phase: str, name: str = "") -> None:
    """Set the current VM + phase (destroying/cloning/starting/...) for the
    live progress bar. Refreshes the per-VM "Recloning" badge too."""
    if _reclone_state.get("status") != "running":
        return
    _reclone_state["current_vm"] = int(vmid) if vmid is not None else None
    _reclone_state["phase"] = str(phase or "")
    if name:
        _reclone_state.setdefault("_names", {})[int(vmid)] = str(name)


def mark_reclone_vm_done(vmid: int, ok: bool, name: str = "") -> None:
    """Record one VM's terminal outcome + bump completed/failed. ``ok`` True →
    completed, False → failed. Called after each per-VM reclone resolves."""
    if _reclone_state.get("status") != "running":
        return
    now = time.time()
    _reclone_state["log"].append({
        "vmid": int(vmid) if vmid is not None else None,
        "name": str(name) if name else _reclone_state.get("_names", {}).get(int(vmid), ""),
        "status": "completed" if ok else "failed",
        "timestamp": now,
    })
    if ok:
        _reclone_state["completed"] = int(_reclone_state.get("completed", 0)) + 1
    else:
        _reclone_state["failed"] = int(_reclone_state.get("failed", 0)) + 1
    if _reclone_state.get("current_vm") == (int(vmid) if vmid is not None else None):
        _reclone_state["current_vm"] = None
        _reclone_state["phase"] = ""


def end_reclone_batch(status: str = "completed") -> None:
    """Finalize a batch: set terminal status + archive it to ``last_run`` so the
    card can show "Last run: … · N completed · N failed". The state stays
    populated (terminal) so the final frame surfaces; the next batch
    start_reclone_batch overwrites it. ``status`` ∈ completed|failed|interrupted."""
    global _reclone_state
    if not _reclone_state:
        return
    st = dict(_reclone_state)
    st["status"] = status or "completed"
    st["ended_at"] = time.time()
    st["last_run"] = {
        "timestamp": st["ended_at"],
        "completed": int(st.get("completed", 0)),
        "failed": int(st.get("failed", 0)),
        "type": st.get("type", ""),
    }
    # Drop the transient _names map before publishing.
    st.pop("_names", None)
    _reclone_state = st


def current_provision_reason() -> Optional[str]:
    """The last pass's outcome / gate reason (``"no dongle_vidpids configured"``,
    ``"auto-provision disabled"``, ``"no template ids configured"``,
    ``"resource gate"``, ``"prov_run active"``, ``"slot cap reached"``,
    ``"no eligible dongles"``, or ``"provisioning: attempted N, provisioned M"``)
    for the telemetry body + WebUI card. None until the first pass runs."""
    return _provision_reason


def current_provision_cfg_snapshot() -> Dict[str, Any]:
    """The provision config as the loop saw it last tick (``dongle_vidpids``
    count, ``image1_template_id``/``image2_template_id`` bools, ``max_slots``,
    ``vmid_range``, ``active_usb_vms``) so the UI can show WHICH precondition is
    missing. Empty until the first pass runs."""
    return dict(_provision_cfg_snapshot)


# ── Per-host VMID batch derivation ──────────────────────────────────────────
# Port of proxmox-agent.sh:122-172: each proxmox host runs its own batch of sim
# VMs, the VMID block derived from the host's trailing numeric suffix so ranges
# don't collide across hosts (svr-01→90001-90024, svr-02→90025-90048,
# svr-003→90049-90072). An explicit non-default vmid_start/vmid_end from the cs
# spoke (per-host override / manual range for >25 slots) wins over the derived
# block; vm_set_override (1-99, legacy VM_SET_OVERRIDE) replaces the batch id.

_VMID_BLOCK_STRIDE = 24
_VMID_DEFAULT_START = 90000
_VMID_DEFAULT_END = 99999


def _host_suffix_id(hostname: str, vm_set_override: Any = 0) -> int:
    """Trailing numeric suffix of a proxmox hostname → 1-based batch id
    (svr-02→2, svr-003→3, no-suffix→1). ``vm_set_override`` (1-99) replaces the
    derived id, mirroring the legacy ``VM_SET_OVERRIDE``."""
    if vm_set_override:
        try:
            o = int(vm_set_override)
            if 1 <= o <= 99:
                return o
        except (TypeError, ValueError):
            pass
    m = re.search(r'(\d+)$', (hostname or '').strip())
    n = int(m.group(1)) if m else 1
    return max(1, n)


def _host_vmid_range(hostname: str, max_slots: int,
                     cfg_start: Any, cfg_end: Any,
                     vm_set_override: Any = 0) -> Tuple[int, int, int, bool]:
    """Resolve this host's sim-VMID range.

    Returns ``(start, end, batch_id, derived)``. When the cs spoke sent an
    explicit non-default ``vmid_start``/``vmid_end`` (per-host override or a
    manual range for >25 slots), that range wins (``derived=False``).
    Otherwise the block is derived from the hostname suffix (``derived=True``):
    ``start = 90000 + (batch_id-1)*24 + 1``, ``end = start + max_slots - 1``.
    """
    start_default = (cfg_start in (None, "") or int(cfg_start) == _VMID_DEFAULT_START)
    end_default = (cfg_end in (None, "") or int(cfg_end) == _VMID_DEFAULT_END)
    if not (start_default and end_default):
        s = int(cfg_start) if cfg_start not in (None, "") else _VMID_DEFAULT_START
        e = int(cfg_end) if cfg_end not in (None, "") else _VMID_DEFAULT_END
        return s, e, _host_suffix_id(hostname, vm_set_override), False
    bid = _host_suffix_id(hostname, vm_set_override)
    s = _VMID_DEFAULT_START + (bid - 1) * _VMID_BLOCK_STRIDE + 1
    e = s + max(1, max_slots) - 1
    return s, e, bid, True


# ── Sim-VM hostnames ─────────────────────────────────────────────────────────
# The legacy cs/proxmox/client-setup.conf mapped every sim VMID to a realistic
# random client hostname (c90025→kbell, c90026→ibennett, …) — 10000 entries,
# VMID 90001-100000. That identity did not carry over to the unified agent
# (which named VMs sim-{vmid}-{type}). Ship the same map (vm_names.json, next
# to this module) and look it up at clone time so a VMID always gets the same
# deterministic human name across re-clones; fall back to sim-{vmid}-{type} when
# the VMID is outside the mapped range.

_VM_NAMES: Optional[Dict[str, str]] = None


def _vm_name(vmid: int) -> Optional[str]:
    """Realistic hostname for a sim VMID from the legacy client-setup.conf map,
    or None if ``vmid`` is outside the 90001-100000 mapped range."""
    global _VM_NAMES
    if _VM_NAMES is None:
        try:
            with open(os.path.join(os.path.dirname(__file__), "vm_names.json")) as f:
                _VM_NAMES = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            _VM_NAMES = {}
    return _VM_NAMES.get(str(vmid))


def current_provision_loop_running() -> bool:
    """True if the provision loop task has ticked recently (heartbeat < 180s,
    i.e. 3× the 60s cadence — mirrors cs ``STALE_SECS=180``). False before the
    first tick or after the task has died/stalled, so the UI can flag a crashed
    loop separately from a gated-but-running one."""
    return (time.time() - _provision_loop_last_run) < 180.0


def current_auto_provision_on() -> bool:
    """The last toggle reading (``usb_auto_provision``/``auto_provision``). For
    the UI to confirm the tenant toggle actually reached this host."""
    return _auto_provision_on


def _load_vmid_gap_last_run() -> float:
    try:
        if os.path.exists(VMID_AUDIT_FILE) and os.path.getsize(VMID_AUDIT_FILE) > 0:
            with open(VMID_AUDIT_FILE) as f:
                return float(json.load(f).get("last_run") or 0.0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return 0.0


def _save_vmid_gap_last_run(ts: float) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(VMID_AUDIT_FILE, "w") as f:
            json.dump({"last_run": float(ts)}, f)
    except OSError:
        pass


def _provisioning_vmids() -> Set[int]:
    """VMIDs currently mid-clone (prov_run items with status 'provisioning')."""
    out: Set[int] = set()
    for it in _prov_run.get("items") or []:
        if str(it.get("status") or "") == "provisioning":
            try:
                out.add(int(it.get("vmid") or 0))
            except (TypeError, ValueError):
                pass
    return out


def _read_sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip().lower()
    except OSError:
        return ""


def _discover_bound_dongle_drivers(dongle_vidpids: Set[str]) -> Set[str]:
    """Scan /sys/bus/usb/devices for dongle devices and collect the kernel
    drivers bound to their interface children (bash 1419-1440)."""
    drivers: Set[str] = set()
    base = "/sys/bus/usb/devices"
    try:
        entries = os.listdir(base)
    except OSError:
        return drivers
    for name in entries:
        dev = os.path.join(base, name)
        if not os.path.isdir(dev):
            continue
        if ":" in name:
            continue  # skip interface entries like "1-5:1.0"
        vid = _read_sysfs(os.path.join(dev, "idVendor"))
        pid = _read_sysfs(os.path.join(dev, "idProduct"))
        if not vid or not pid:
            continue
        vidpid = f"{vid}:{pid}"
        if vidpid not in dongle_vidpids:
            continue
        # Interface children: <bus>:1.0, <bus>:1.1, ... → their bound driver.
        for child in entries:
            if not child.startswith(f"{name}:"):
                continue
            drv_link = os.path.join(base, child, "driver")
            try:
                if os.path.islink(drv_link):
                    drv = os.path.basename(os.path.realpath(drv_link))
                    if drv and drv != ".":
                        drivers.add(drv)
            except OSError:
                continue
    return drivers


def _render_blacklist_conf(drivers: List[str]) -> str:
    body = "\n".join(f"blacklist {d}" for d in sorted(drivers))
    return (
        "# Auto-generated by pxmx client-sim agent — do not edit manually\n"
        "# Prevents the host from binding to USB dongles used for VM passthrough\n"
        f"{body}\n"
    )


async def blacklist_dongle_drivers(agent) -> Dict[str, Any]:
    """Write the modprobe blacklist for bound dongle drivers and rmmod them.
    Idempotent (only writes on diff). Returns the blacklisted driver list."""
    import asyncio
    from . import pve_cmds

    dongles = _dongle_vidpids(agent)
    if not dongles:
        return {"action": "blacklist_dongle_drivers", "drivers": [], "note": "no dongle vidpids configured"}
    drivers = _discover_bound_dongle_drivers(dongles)
    if not drivers:
        return {"action": "blacklist_dongle_drivers", "drivers": []}

    rendered = _render_blacklist_conf(sorted(drivers))
    try:
        existing = ""
        if os.path.exists(DONGLE_BLACKLIST_CONF):
            with open(DONGLE_BLACKLIST_CONF) as f:
                existing = f.read()
    except OSError as e:
        logger.warning(f"blacklist_dongle_drivers: read conf failed: {e}")
        existing = ""

    if existing != rendered:
        try:
            with open(DONGLE_BLACKLIST_CONF, "w") as f:
                f.write(rendered)
            await pve_cmds._run(["depmod", "-a"], check=False, timeout=20)
            logger.info(f"Driver blacklist updated: {sorted(drivers)}")
        except OSError as e:
            logger.warning(f"blacklist_dongle_drivers: write failed (non-root?): {e}")
            return {"action": "blacklist_dongle_drivers", "drivers": sorted(drivers), "written": False}

    # Unload currently-loaded drivers so the blacklist takes effect now (not
    # just next boot). Non-fatal if a driver is in use.
    unloaded: List[str] = []
    for drv in sorted(drivers):
        rc, out, _ = await pve_cmds._run(["lsmod"], check=False, timeout=10)
        loaded = any(line.split()[0] == drv for line in out.decode().splitlines() if line.split())
        if not loaded:
            continue
        rc, _, _ = await pve_cmds._run(["rmmod", drv], check=False, timeout=10)
        if rc == 0:
            unloaded.append(drv)
            logger.info(f"Unloaded driver: {drv}")
        else:
            logger.warning(f"Could not unload driver {drv} (in use?) — blacklist takes effect on next boot")

    return {"action": "blacklist_dongle_drivers", "drivers": sorted(drivers), "unloaded": unloaded}


# ── USB provision state + quarantine (Phase E) ────────────────────────────
# The vmid↔bus state doc (usb_state.json) + orphan registry live in
# usb_state_store; the dongle quarantine + destroy-fail + bus-exclusion state
# lives in usb_quarantine (both re-exported below). The provision loop +
# delete/reclone long-op tasks run on the single asyncio event loop so no lock
# is needed.


async def _vm_usb_bus(vid: int) -> Optional[str]:
    """The host USB BUS PATH a qemu VM passes through (``usbN: host=<bus>`` →
    ``<bus>``), or None. Only the bus-path form (e.g. ``5-1.4``) counts — a
    ``host=<vid>:<pid>`` vidpid passthrough isn't our bus-tracked model."""
    from . import pve_cmds
    try:
        cfg = await pve_cmds.qm_config(vid)
    except Exception:  # noqa: BLE001
        return None
    for k, v in (cfg or {}).items():
        if str(k).startswith("usb") and "host=" in str(v):
            val = str(v).split("host=", 1)[1].split(",", 1)[0].strip()
            if val and ":" not in val:
                return val
    return None


async def _vm_has_usb_passthrough(vid: int) -> bool:
    """True if the VM has ANY usb passthrough — ``host=<bus>`` OR
    ``host=<vid:pid>`` — i.e. it's a real dongle VM, NOT a half-cloned zombie.

    Distinct from ``_vm_usb_bus`` (which returns None for the vidpid form): the
    VMID allocator uses this to tell a legit-but-untracked dongle VM (skip it —
    never destroy a real client just because tracking was lost) from a true
    zombie clone (no usb → safe to reclaim). A vidpid-passthrough dongle VM is
    invisible to ``reconcile_bus_map``/``reconcile_vm_configs`` (they re-track
    via ``_vm_usb_bus``), so without this check the allocator would destroy it
    every pass — the 'legit running client keeps getting killed / the reclaim
    hangs and wedges the loop' bug.

    Conservative on uncertainty: a ``qm_config`` failure returns True (treat as
    'might be a real VM, don't destroy') — destroying a real client because a
    transient Proxmox RPC failed is far worse than skipping a real zombie for
    one tick (the next pass retries once the RPC is back)."""
    from . import pve_cmds
    try:
        cfg = await pve_cmds.qm_config(vid)
    except Exception:  # noqa: BLE001 — conservative: assume real, skip reclaim
        return True
    for k, v in (cfg or {}).items():
        if str(k).startswith("usb") and "host=" in str(v):
            return True
    return False


async def reconcile_vm_configs(agent, start: int, end: int,
                               present, now: float, existing) -> bool:
    """Rebuild bus_to_vmid from each sim VM's ACTUAL usb passthrough (source of
    truth = ``qm config``), so a dongle-backed VM that fell out of the state file
    is re-tracked instead of stranded (a stranded VM is invisible to the
    missing-dongle teardown, which only iterates bus_to_vmid — the "12 VMs, 4
    tracked, nothing sheds" case). Only UNTRACKED sim-range VMs are inspected
    (one ``qm config`` each — ~zero cost once everything is tracked). A present
    dongle → active tracking; an absent dongle → tracked + missing_since=now so
    the teardown counts it down and sheds it. Non-passthrough VMs are never
    touched. Returns True if anything was re-tracked."""
    from . import pve_cmds
    st = load_usb_state()
    tracked = {int(v) for v in (st.get("bus_to_vmid") or {}).values()
               if str(v).lstrip("-").isdigit()}
    changed = False
    for vid in sorted(int(x) for x in existing if str(x).lstrip("-").isdigit()):
        if vid < start or vid > end or vid in tracked:
            continue
        if await pve_cmds.is_template(vid):
            continue
        bus = await _vm_usb_bus(vid)
        if not bus:
            continue  # not a dongle VM — never auto-track/shed it
        image = int((st.get("vmid_to_image") or {}).get(str(vid), 1) or 1)
        st.setdefault("bus_to_vmid", {})[bus] = str(vid)
        st.setdefault("vmid_to_bus", {})[str(vid)] = bus
        st.setdefault("vmid_to_image", {})[str(vid)] = image
        if bus in present:
            st.setdefault("missing_since", {}).pop(bus, None)
            _label = "present"
        else:
            st.setdefault("missing_since", {}).setdefault(bus, now)
            _label = "MISSING — shed countdown started"
        changed = True
        logger.info("reconcile_vm_configs: re-tracked VM %s → bus %s [%s]",
                    vid, bus, _label)
    if changed:
        save_usb_state(st)
    return changed


# ── present-dongle discovery ──────────────────────────────────────────────


def scan_present_dongles(dongle_vidpids: Set[str],
                          certified_types: Optional[Dict[str, str]] = None
                          ) -> Dict[str, Dict[str, Any]]:
    """Scan /sys/bus/usb/devices for currently-present dongles whose vid:pid is
    in the configured set (bash ``scan_usb_devices`` 1537-1620). Returns
    ``{bus_path: {vidpid, product, type}}``. ``type`` comes from the certified
    map (default 'wireless')."""
    certified_types = certified_types or {}
    out: Dict[str, Dict[str, Any]] = {}
    base = "/sys/bus/usb/devices"
    try:
        entries = os.listdir(base)
    except OSError:
        return out
    for name in entries:
        if ":" in name:
            continue
        dev = os.path.join(base, name)
        if not os.path.isdir(dev):
            continue
        vid = _read_sysfs(os.path.join(dev, "idVendor"))
        pid = _read_sysfs(os.path.join(dev, "idProduct"))
        if not vid or not pid:
            continue
        vidpid = f"{vid}:{pid}"
        if vidpid not in dongle_vidpids:
            continue
        # Product label from the first interface's product field, else the
        # sysfs product file.
        product = _read_sysfs(os.path.join(dev, "product")) or name
        out[name] = {"vidpid": vidpid, "product": product,
                     "type": str(certified_types.get(vidpid, "wireless"))}
    return out


def cs_usb_telemetry(agent) -> Dict[str, List[Dict[str, Any]]]:
    """Build the USB portion of this host's CS telemetry body by scanning
    ``/sys/bus/usb/devices`` and classifying each present device against the
    hub-delivered certified/ignored vidpid sets:

    * certified  → ``present_usb``  (entry: ``{bus_path, vidpid, product, type}``)
    * ignored    → dropped (never reported)
    * otherwise  → ``unknown_usb``  (entry: ``{bus_path, vidpid, name}``)

    ``usb_state`` is the assigned-dongle state from ``load_usb_state()``
    (entry: ``{vmid, bus_path, missing_since, name, vidpid, prov_status}``,
    prov_status ``missing`` when the bus is past the missing timeout else
    ``active``), with name/vidpid back-filled from the present scan.

    Best-effort: any failure returns empty lists (the cs speak tolerates
    empty). Mirrors the legacy cs bash agent's telemetry body so the cs speak's
    ``_apply_proxmox_telemetry_state`` ingests it unchanged."""
    empty: Dict[str, List[Dict[str, Any]]] = {"usb_state": [], "present_usb": [],
                                              "unknown_usb": [], "quarantine": []}
    try:
        certified = _dongle_vidpids(agent)
        ignored = _ignored_vidpids(agent)
        ctypes = _certified_types(agent)
        present: List[Dict[str, Any]] = []
        unknown: List[Dict[str, Any]] = []
        present_by_bus: Dict[str, Dict[str, Any]] = {}
        base = "/sys/bus/usb/devices"
        try:
            entries = os.listdir(base)
        except OSError:
            entries = []
        for name in entries:
            if ":" in name:
                continue  # interface child, not a device
            dev = os.path.join(base, name)
            if not os.path.isdir(dev):
                continue
            vid = _read_sysfs(os.path.join(dev, "idVendor"))
            pid = _read_sysfs(os.path.join(dev, "idProduct"))
            if not vid or not pid:
                continue
            vidpid = f"{vid}:{pid}"
            product = _read_sysfs(os.path.join(dev, "product")) or name
            if vidpid in certified:
                entry = {"bus_path": name, "vidpid": vidpid,
                         "product": product, "type": str(ctypes.get(vidpid, "wireless"))}
                present.append(entry)
                present_by_bus[name] = entry
            elif vidpid in ignored:
                continue
            else:
                unknown.append({"bus_path": name, "vidpid": vidpid, "name": product})

        usb_state: List[Dict[str, Any]] = []
        try:
            st = load_usb_state()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cs_usb_telemetry: load_usb_state failed: %s", exc)
            st = _new_usb_state()
        missing_since = st.get("missing_since") or {}
        # Missing-dongle shed deadline for the WebUI countdown: the teardown fires
        # when now - missing_since >= missing_timeout, so shed_at = missing_since +
        # missing_timeout (same units the teardown compares — accurate regardless
        # of the min/sec relay convention). 0 = teardown disabled (no deadline).
        usb_cfg = _usb_cfg(agent)
        missing_timeout = int(_cfg_first(usb_cfg,
                              ("usb_missing_timeout_seconds", "usb_missing_timeout",
                               "missing_timeout"), 0) or 0)
        for bus, vmid in (st.get("bus_to_vmid") or {}).items():
            pe = present_by_bus.get(bus) or {}
            ms = missing_since.get(bus)
            usb_state.append({
                "vmid": vmid,
                "bus_path": bus,
                "missing_since": ms,
                "missing_timeout_s": missing_timeout,
                "shed_at": (float(ms) + missing_timeout)
                           if (ms is not None and missing_timeout > 0) else None,
                "name": pe.get("product") or bus,
                "vidpid": pe.get("vidpid") or "",
                "prov_status": "missing" if ms is not None else "active",
            })
        # Quarantined dongles (dmesg kernel USB errors — the ONLY quarantine path)
        # for the WebUI badge: bus-id + reason + when, so an admin can see WHY a
        # dongle is sidelined and that it auto-recovers after QUARANTINE_RECOVERY_S.
        quarantined: List[Dict[str, Any]] = []
        try:
            qt = _read_quarantine()
        except Exception as exc:  # noqa: BLE001
            logger.debug("cs_usb_telemetry: read quarantine failed: %s", exc)
            qt = {}
        _now = time.time()
        for bus, entry in (qt or {}).items():
            e = entry or {}
            permanent = bool(e.get("permanent"))
            if not permanent and int(e.get("fails", 0)) < QUARANTINE_MAX_FAILS:
                continue
            pe = present_by_bus.get(bus) or {}
            since = e.get("since")
            # Absolute epoch the 1h auto-recovery re-eligibles this bus — the
            # WebUI QT badge counts down to it live (since + QUARANTINE_RECOVERY_S).
            # A PERMANENT bus (5 strikes) never auto-recovers → no recovers_at.
            recovers_at = (float(since) + QUARANTINE_RECOVERY_S) \
                if (since is not None and not permanent) else None
            quarantined.append({
                "bus_path": bus,
                "reason": e.get("reason") or "quarantined",
                "since": since,
                # 5-strike state — permanent buses never auto-recover; strikes is
                # the count of quarantine episodes (1..QUARANTINE_PERMANENT_STRIKES).
                "permanent": permanent,
                "strikes": int(e.get("strikes", 0)),
                # Absolute recovery target (epoch seconds) for the live badge.
                "recovers_at": recovers_at,
                # Seconds until the 1h auto-recovery clears it (clamped >=0).
                "recovers_in_s": max(0, int(recovers_at - _now))
                                 if recovers_at is not None else None,
                "present": bus in present_by_bus,
                "name": pe.get("product") or bus,
                "vidpid": pe.get("vidpid") or "",
            })
        return {"usb_state": usb_state, "present_usb": present,
                "unknown_usb": unknown, "quarantine": quarantined}
    except Exception as exc:  # noqa: BLE001
        logger.warning("cs_usb_telemetry: failed: %s", exc)
        return empty


def _sim_phy_accepts(sim_phy: str, device_type: str) -> bool:
    # sim_phy is the sim VM's required physical layer (cs domain:
    # wireless | ethernet | any). device_type is the dongle class from the
    # LM usb_vidpids `type` field (wireless | wired | storage | other). A sim
    # requiring "ethernet" wants a *wired* dongle — map wired <-> ethernet so
    # the wired/wireless selector the tenant sets in LM is actually enforced.
    # "storage" (a real, known-incompatible class) only matches sim_phy == "any".
    if sim_phy == "any":
        return True
    # An UNCLASSIFIED dongle ("other"/unknown/empty) is not filtered out on type:
    # the admin certified it, so provision it regardless of sim_phy. Only a KNOWN
    # mismatch (e.g. a "wired"/"storage" dongle when the sim wants "wireless") is
    # excluded. This stops a wireless dongle that got certified as "other" (the
    # certify UI's per-row type inference) from being wrongly rejected.
    if device_type in ("other", "unknown", ""):
        return True
    if sim_phy == device_type:
        return True
    if sim_phy == "ethernet" and device_type == "wired":
        return True
    return False


async def run_provision_loop(agent) -> Dict[str, Any]:
    """One USB-provision pass — the cs auto-provisioning "brain".

    Ports ``cs/webui-spoke/server.py`` 10020-10294 + ``proxmox-agent.sh``
    5005-5060/2648-2666 into the pxmx agent (the LM cs spoke is only a relay, so
    the brain runs here). Layers, in order:

    1. Reconcile stale usb_state (release buses whose VM no longer exists).
    2. **Toggle gate**: ``usb_auto_provision`` off → telemetry-only pass (reconcile
       only, no clone/teardown/delete/audit — mirrors ``refresh_usb_telemetry_only``).
    3. **provision_halt** + **resource thresholds**: cpu/mem 1h averages vs the
       provision/delete/ceiling thresholds.
    4. **Delete gate**: over the delete threshold → destroy the newest USB VM
       (highest VMID) + enter a 300s cooldown (anti-churn).
    5. **missing-dongle teardown**: destroy VMs whose dongle vanished past the
       (now correctly-read) timeout.
    6. **VMID-gap audit**: every 300s, delete the highest VMID above the lowest
       gap so the next pass refills from the hole.
    7. **resource_ok gate + slot cap**: only clone when resources are under the
       provision threshold, not in cooldown/ceiling, no prov_run already active,
       and under ``usb_max_slots``.

    Returns a summary the ``provision_unassigned`` long-op reports as its result.
    """
    global _prov_run, _provision_reason, _provision_cfg_snapshot, \
        _provision_loop_last_run, _auto_provision_on
    from . import pve_cmds  # local to avoid a top-level import cycle
    cs_cfg = agent.config.get("client_simulation") or {}
    usb_cfg = cs_cfg.get("usb_config") or {}
    dongle_vidpids = _dongle_vidpids(agent)
    # Heartbeat: the loop is alive. Stamped before any gate so
    # current_provision_loop_running() flips true on the very first tick (lets the
    # UI distinguish "loop not running" from "loop running but gated").
    _provision_loop_last_run = time.time()

    # Template-refresh pause: while a REFRESH_TEMPLATE wipes + restores this host,
    # do NOTHING (no shed/clone/teardown) so we don't fight the refresh.
    if _refresh_paused:
        _provision_reason = "template refresh in progress"
        return {"provisioned": 0, "torn_down": 0, "reason": _provision_reason}

    # Safety resource delete gate runs FIRST, before the provisioning
    # preconditions (dongle_vidpids / templates) below — a provisioning config
    # gap must NEVER disable the CPU/mem shed. Only when auto-provision is on.
    # Returns the VMID it shed (or None); counted into torn_down below.
    ap_on = _toggle_on(usb_cfg)
    _auto_provision_on = ap_on
    _early_shed = await _run_delete_gate(agent, usb_cfg) if ap_on else None

    if not dongle_vidpids:
        # Silent gate made loud — this is the #1 cause of "nothing provisions" and
        # previously left no log line at all. Surface it in the log + telemetry.
        _provision_reason = "no dongle_vidpids configured"
        _provision_cfg_snapshot = {"dongle_vidpids": 0, "image1_template_id": False,
                                    "image2_template_id": False, "max_slots": None,
                                    "vmid_range": {}, "active_usb_vms": None}
        logger.warning("auto-provision: no dongle_vidpids configured — certify USB "
                       "vid:pid values in the Simulations UI so dongles can be matched")
        return {"provisioned": 0,
                "torn_down": 1 if _early_shed is not None else 0,
                "reason": "no dongle_vidpids configured"}

    # ap_on already computed above (before the early delete gate).
    # The cs spoke emits the certified list as ``vidpids`` (a list of
    # {vidpid, type} dicts), not a ``certified_types`` {vidpid: type} map — so
    # build the map via the accessor (which reads ``vidpids``) instead of the
    # stale legacy key. Used to tag each present dongle with its dongle class.
    certified_types = _certified_types(agent)
    sim_phy = str(usb_cfg.get("sim_phy") or "any").lower()
    use_all = bool(usb_cfg.get("use_all_dongles", False))
    # Validate the configured template ids are actually runnable Proxmox
    # templates before trusting them as clone sources — a stale/deleted
    # template id would otherwise fail every clone silently (bash
    # resolve_template_vmid, proxmox-agent.sh:901-944). Falls back to the
    # lowest-numbered valid template on the cluster if the configured one no
    # longer checks out; stays None (as before) when nothing is configured.
    img1 = await _resolve_template_vmid(usb_cfg.get("image1_template_id"))
    img2 = await _resolve_template_vmid(usb_cfg.get("image2_template_id"))
    img1_pct = int(usb_cfg.get("image1_pct", 50) or 50)
    # N-image clone sources. Generic image_count + image{i}_template_id/_pct,
    # falling back to the legacy image1/image2 + image1_pct pair. Each entry is
    # {"num": i, "template": <resolved vmid>, "pct": <int 0-100>}; the selection
    # loop below fills the fleet to these proportions. See _resolve_images.
    images = await _resolve_images(usb_cfg, img1, img2, img1_pct)
    # Per-host VMID batch: the cs speak emits vmid_start/vmid_end in usb_config
    # (defaults 90000/99999). When those are at the default the agent derives
    # this host's block from its own hostname suffix (svr-02→90025-90048, stride
    # 24) so each proxmox server runs its own batch and ranges don't collide —
    # a port of proxmox-agent.sh:122-172 that the unified agent was missing. An
    # explicit non-default vmid_start/vmid_end (cs-spoke per-host override or a
    # manual range for >25 slots) wins over the derived block; vm_set_override
    # (1-99, legacy VM_SET_OVERRIDE) replaces the batch id.
    max_slots = int(_cfg_first(usb_cfg, ("usb_max_slots", "max_slots"), 24) or 24)
    start, end, batch_id, _range_derived = _host_vmid_range(
        getattr(agent, "hostname", "") or "",
        max_slots,
        usb_cfg.get("vmid_start"), usb_cfg.get("vmid_end"),
        usb_cfg.get("vm_set_override") or 0,
    )
    # missing_timeout: accept the union of relay key names (webui-spoke sends
    # usb_missing_timeout, lm-spoke sends missing_timeout) — the old single-key
    # read (usb_missing_timeout_seconds, which nothing sends) left the teardown
    # block dead under both relay paths.
    missing_timeout = int(_cfg_first(usb_cfg,
                                     ("usb_missing_timeout_seconds", "usb_missing_timeout",
                                      "missing_timeout"), 0) or 0)
    # How long a dongle stays bus-excluded after an admin VM delete before the
    # loop auto-returns it to service (never permanent). Tenant-configurable.
    exclude_cooldown = int(_cfg_first(usb_cfg,
                                      ("usb_exclude_cooldown", "exclude_cooldown"),
                                      EXCLUDE_COOLDOWN_S) or EXCLUDE_COOLDOWN_S)
    # Default 1 (sequential), matching bash's explicit safety default
    # (RECLONE_CONCURRENCY=1, proxmox-agent.sh:114) — parallel clones are an
    # explicit admin opt-in, not the out-of-the-box behavior; N simultaneous
    # `qm clone` disk copies can swamp host I/O/CPU on a box already running
    # sim VMs.
    concurrency = max(1, int(usb_cfg.get("reclone_concurrency", 1) or 1))

    # Config snapshot for telemetry: lets the UI show WHICH precondition is missing
    # (dongle_vidpids count, template ids set, slot cap, vmid range). active_usb_vms
    # is filled after the slot cap is evaluated below.
    _auto_provision_on = ap_on
    _provision_cfg_snapshot = {
        "dongle_vidpids": len(dongle_vidpids),
        "image1_template_id": bool(img1),
        "image2_template_id": bool(img2),
        "max_slots": max_slots,
        "vmid_range": {"start": start, "end": end, "batch_id": batch_id},
        "active_usb_vms": None,
    }

    # Resource state (sampled each tick by _usb_provision_loop → sample_resources).
    _cpu_hist = usb_resource_gate.cpu_samples()
    _mem_hist = usb_resource_gate.mem_samples()
    cpu_avg = _resource_1h_average(_cpu_hist)
    mem_avg = _resource_1h_average(_mem_hist)
    cpu_instant = _cpu_hist[-1][1] if _cpu_hist else None
    # Surface the exact 1h averages the gates decide on (WebUI shows these next
    # to the display CPU 1H / Mem 1H so the operator sees what auto-prov uses).
    usb_resource_gate.set_1h_averages(cpu_avg, mem_avg)

    state = load_usb_state()
    existing = set(await pve_cmds.list_all_vmids())
    present = scan_present_dongles(dongle_vidpids, certified_types)
    now = time.time()

    # 1. Reconcile: drop every tracked VM no longer on the host. Symmetric prune
    # (bus_to_vmid by value + vmid_to_bus by key) so a ghost stranded in
    # bus_to_vmid after a partial clear is caught — the old vmid_to_bus-only loop
    # missed it, and the delete gate (which selects from bus_to_vmid) then
    # fixated on that ghost forever. Reload after a prune so the rest of the pass
    # sees the cleaned state.
    if prune_ghost_vms(existing):
        state = load_usb_state()
    # 1a. Bijection reconcile: drop bus_to_vmid entries that disagree with
    # vmid_to_bus (a VM re-provisioned onto a new bus left its old reverse entry,
    # so it showed under two vid:pids in the certified-USB table). Self-heals the
    # existing stale entries; set_assignment now prevents new ones.
    _busfix = reconcile_bus_map()
    if _busfix:
        logger.info("provision reconcile: cleared stale bus_to_vmid entries for VMID(s) %s", _busfix)
        state = load_usb_state()
    # 1b. Re-track from source of truth: rebuild bus_to_vmid from each sim VM's
    # actual usb passthrough (qm config), so a dongle-backed VM that fell out of
    # the state file is re-tracked (present → active; absent dongle → missing +
    # countdown) instead of stranded/invisible to the teardown. Self-heals the
    # "N VMs but only M tracked, nothing sheds" case.
    if await reconcile_vm_configs(agent, start, end, present, now, existing):
        state = load_usb_state()
    # Remember each tracked bus's vidpid while it's actually present, so a
    # dongle that later moves to a different bus path (unplugged/replugged
    # into a different physical port) can still be matched by vidpid below
    # (bash build_usb_state_json 1565-1572: "use live-scanned vidpid... update
    # stored value so it persists after the dongle goes physically missing").
    for bus in list(state["bus_to_vmid"]):
        if bus in present:
            state.setdefault("vidpid_by_bus", {})[bus] = present[bus].get("vidpid")
    # Bus exclusions (set on admin delete, anti-churn) are TIME-LIMITED: clear a
    # bus once it goes absent (unplugged) OR the exclude cooldown elapses, so a
    # deleted dongle returns to service automatically instead of staying excluded
    # forever. A legacy bare-1 value (pre-timestamp) has since=0 → treated as
    # already expired, so old permanent exclusions self-heal on the next pass.
    for bus in list(state["excluded_buses"]):
        v = state["excluded_buses"].get(bus)
        since = float(v) if isinstance(v, (int, float)) and float(v) > 1e9 else 0.0
        if bus not in present or now - since >= exclude_cooldown:
            state["excluded_buses"].pop(bus, None)
            logger.info("provision loop: bus exclusion cleared for %s (%s)", bus,
                        "unplugged" if bus not in present
                        else f"cooldown {exclude_cooldown}s elapsed")
    # Auto-recover quarantine after QUARANTINE_RECOVERY_S (1h) — present OR absent.
    # Quarantine is now dmesg-ONLY (a real kernel USB hardware-fault signal), so a
    # still-plugged quarantined dongle MUST be retried eventually: a transient
    # kernel hiccup (cable jiggle, port reset) shouldn't sideline a good dongle
    # forever. After 1h it gets a fresh provisioning attempt; if the kernel errors
    # are still firing, scan_dmesg_usb_errors re-quarantines it next pass. Absent
    # dongles clear on the same 1h clock so a replug starts clean.
    #
    # Strike-aware recovery: a bus that re-quarantines QUARANTINE_PERMANENT_STRIKES
    # times is marked permanent — those NEVER auto-recover (operator clears them).
    # Non-permanent: reset fails=0 (re-eligible for a retry) but PRESERVE the
    # strike/first_strike/last_strike history so the next quarantine increments
    # toward permanent. We no longer pop the entry (that would lose strike history).
    quarantine = _read_quarantine()
    changed = False
    for bus in list(quarantine):
        entry = quarantine[bus] or {}
        if entry.get("permanent"):
            continue  # permanent buses never auto-recover
        if int(entry.get("fails", 0)) < QUARANTINE_MAX_FAILS:
            continue  # already recovered/eligible this cycle
        since = entry.get("since")
        if since is not None and now - float(since) >= QUARANTINE_RECOVERY_S:
            entry["fails"] = 0
            quarantine[bus] = entry
            changed = True
            logger.info("provision loop: quarantine auto-recovered for %s "
                        "(%s — %ds elapsed, %s; strikes=%d/%d)",
                        bus, entry.get("reason", ""), QUARANTINE_RECOVERY_S,
                        "still present" if bus in present else "absent",
                        int(entry.get("strikes", 0)), QUARANTINE_PERMANENT_STRIKES)
    if changed:
        _save_quarantine(quarantine)

    # 1c. Post-provisioning retry queue — runs unconditionally (matches bash
    # calling _run_post_prov_retry_queue independently of the AUTO_PROVISION
    # toggle, proxmox-agent.sh:5068): a VM already cloned before the toggle
    # was switched off still deserves its update.sh retry / 1h reclone.
    if await _run_post_prov_retry_queue(agent, state):
        save_usb_state(state)

    torn_down: List[int] = []
    if _early_shed is not None:
        torn_down.append(_early_shed)

    # 2. Toggle gate — off = telemetry-only (no VM mutations).
    if not ap_on:
        save_usb_state(state)
        usb_resource_gate._provision_halt = None
        _provision_reason = "auto-provision disabled"
        logger.debug("auto-provision: usb_auto_provision=off — telemetry-only pass")
        return {"provisioned": 0, "torn_down": 0, "reason": "auto-provision disabled"}

    # 2b. Migrate to per-host batches: destroy agent-owned sim VMs whose VMIDs
    #     fall outside this host's batch range (created under the old flat
    #     90000-99999 default, before the hostname-suffix derivation was ported).
    #     Only touches VMs tracked in the sim state — never clone templates
    #     (100/200) or the user's real VMs, which aren't in state. Idempotent:
    #     once the out-of-range VMs are gone this is a no-op each tick.
    out_of_range = [(int(v), b) for v, b in state["vmid_to_bus"].items()
                    if not (start <= int(v) <= end)]
    if out_of_range:
        from . import cs_sim  # deferred — cs_sim imports usb_provision
        logger.info("auto-provision: %d sim VM(s) outside batch range %d-%d "
                    "(batch %d) — tearing down (migrating to per-host batches)",
                    len(out_of_range), start, end, batch_id)
        for vmid, bus in out_of_range:
            try:
                await cs_sim.destroy_vm(agent, vmid, bus=bus)
            except Exception as e:  # noqa: BLE001
                logger.warning("auto-provision: migration teardown of %s failed: %s",
                               vmid, e)
            state["vmid_to_bus"].pop(str(vmid), None)
            state["bus_to_vmid"].pop(bus, None)
            state["vmid_to_image"].pop(str(vmid), None)
            state["missing_since"].pop(bus, None)
            torn_down.append(vmid)
        save_usb_state(state)

    # 3. Provision thresholds (cs defaults: prov 80 / ceiling 90). The DELETE
    #    thresholds are read inside _run_delete_gate — the resource shed runs
    #    early now (before these provisioning preconditions).
    cpu_prov_thr = _pct_setting(usb_cfg, "cpu_provision_threshold", 80)
    cpu_prov_ceil = _pct_setting(usb_cfg, "cpu_provision_ceiling", 90)
    mem_prov_thr = _pct_setting(usb_cfg, "mem_provision_threshold", 80)

    # provision_halt: over the provision threshold → halt (cs agent.sh 2648-2666).
    # The published dict must carry the four numeric fields the WebUI
    # AUTO-PROV cell reads (csProvThrottleBadge: cpu_pct/cpu_threshold/mem_pct/
    # mem_threshold) — the bash agent emits these (proxmox-agent.sh:2514-2526);
    # omitting them renders "CPU ?% ≥ ?%" placeholders in the Overview column.
    cpu_over_prov = cpu_avg is not None and cpu_avg >= cpu_prov_thr
    mem_over_prov = mem_avg is not None and mem_avg >= mem_prov_thr
    if cpu_over_prov or mem_over_prov:
        usb_resource_gate._provision_halt = {
            "halted": True,
            "reason": "cpu" if cpu_over_prov else "mem",
            "cpu_pct": round(cpu_avg, 1) if cpu_avg is not None else None,
            "cpu_threshold": cpu_prov_thr,
            "mem_pct": round(mem_avg, 1) if mem_avg is not None else None,
            "mem_threshold": mem_prov_thr,
            "ts": int(now),
        }
    else:
        usb_resource_gate._provision_halt = None

    # 4. Resource delete gate ALREADY RAN early (before the provisioning
    #    preconditions) in _run_delete_gate, which also published the
    #    _delete_gate decision trace. Reload the cooldown it may have set so the
    #    provision gate below still respects "don't clone right after a shed",
    #    and mark delete_queued from that early result.
    cooldown_until = _load_delete_gate_cooldown()
    delete_queued = _early_shed is not None

    # 4b. Bus-migration reconciliation (bash reconcile_present_usb_state,
    # proxmox-agent.sh:1509-1556, called right before the missing-dongle scan
    # below). A dongle unplugged and replugged into a DIFFERENT physical port
    # gets a new bus path from the kernel, even though it's the exact same
    # device (same vidpid). Without this, the old bus just starts accumulating
    # missing_since and eventually tears down + reprovisions a VM the dongle
    # never actually left — while the new bus sits there unrecognized. Follow
    # the vidpid to its new bus instead, as long as that bus isn't already
    # claimed by a different tracked VM.
    vidpid_by_bus = state.setdefault("vidpid_by_bus", {})
    for old_bus, vmid in list(state["bus_to_vmid"].items()):
        if old_bus in present:
            continue  # still on the same bus — nothing to migrate
        vidpid = vidpid_by_bus.get(old_bus)
        if not vidpid:
            continue
        new_bus = next((b for b, info in present.items()
                        if info.get("vidpid") == vidpid and b != old_bus), None)
        if not new_bus:
            continue
        other_vmid = state["bus_to_vmid"].get(new_bus)
        if other_vmid and other_vmid != vmid:
            continue  # new bus already claimed by a different tracked VM
        state["bus_to_vmid"].pop(old_bus, None)
        state["missing_since"].pop(old_bus, None)
        vidpid_by_bus.pop(old_bus, None)
        state["vmid_to_bus"][vmid] = new_bus
        state["bus_to_vmid"][new_bus] = vmid
        state["missing_since"].pop(new_bus, None)
        vidpid_by_bus[new_bus] = vidpid
        logger.info(f"provision loop: USB dongle {vidpid} moved from {old_bus} "
                   f"to {new_bus}, following assignment for VM {vmid}")

    # 4c. Faulty-dongle quarantine from kernel USB errors. A flaky port/dongle
    #     logs 'usb 3-1.2: device descriptor read error -71' etc.; quarantine
    #     that bus so it isn't re-provisioned. Only quarantine buses we actually
    #     care about (currently present OR tracked with a VM) so a random USB
    #     device's error can't strand an unrelated bus. A quarantined dongle that
    #     also drops off enumeration is torn down by the missing-dongle sweep
    #     below; the quarantine keeps it from being immediately re-cloned.
    try:
        dmesg_errs = await scan_dmesg_usb_errors()
    except Exception as _e:  # noqa: BLE001
        dmesg_errs = {}
        logger.debug("provision loop: dmesg USB scan failed: %s", _e)
    if dmesg_errs:
        _watched = set(present) | set(state.get("bus_to_vmid") or {})
        for _bus, _n in dmesg_errs.items():
            if _n >= _DMESG_USB_QUARANTINE_MIN and _bus in _watched:
                if int((_read_quarantine().get(_bus) or {}).get("fails", 0)) < QUARANTINE_MAX_FAILS:
                    quarantine_bus(_bus, f"kernel USB errors ({_n} in {_DMESG_USB_WINDOW_S}s)")
                    logger.warning(
                        "auto-provision: quarantined bus %s — %d kernel USB error(s) "
                        "in %ds (faulty port/dongle; will not be re-provisioned)",
                        _bus, _n, _DMESG_USB_WINDOW_S)

    # 5. Missing-dongle teardown (only when the toggle is on).
    if missing_timeout <= 0 and state["bus_to_vmid"]:
        # missing_timeout=0 DISABLES the dongle-missing shed entirely — a removed/
        # unapproved dongle would never tear down its VM. Surface it so it isn't
        # silently off (Setup → Proxmox → "Destroy after N minutes"; 0 = never).
        logger.warning(
            "auto-provision: usb_missing_timeout is 0 — dongle-missing teardown is "
            "DISABLED; a removed/unapproved dongle will NOT shed its VM. Set a "
            "non-zero 'Destroy after' timeout to enable it. (%d tracked dongles)",
            len(state["bus_to_vmid"]))
    if missing_timeout > 0:
        from . import cs_sim  # deferred — cs_sim imports usb_provision
        for bus, vmid in list(state["bus_to_vmid"].items()):
            if bus in present:
                state["missing_since"].pop(bus, None)
                continue
            since = state["missing_since"].get(bus)
            if since is None:
                state["missing_since"][bus] = now
                continue
            if now - float(since) >= missing_timeout:
                try:
                    await cs_sim.destroy_vm(agent, int(vmid), bus=bus)
                    torn_down.append(int(vmid))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"provision loop: teardown of {vmid} failed: {e}")
                state["bus_to_vmid"].pop(bus, None)
                state["vmid_to_bus"].pop(vmid, None)
                state["missing_since"].pop(bus, None)

    # 6. VMID-gap audit (every VMID_AUDIT_INTERVAL_S; bypasses delete cooldown).
    #    Compaction: shed the highest VMID above the lowest gap so the next pass
    #    refills the hole — but ONLY when every present dongle is already assigned
    #    (full-but-sparse). When a present dongle is unassigned the allocator
    #    refills the lowest gap on its own, so shedding here would only churn a
    #    legit high VM (the "delete 90001 → 90008 destroyed-then-recloned" bug).
    #    May delete a VM and mutate state — persist before the early returns
    #    below so the audit's bookkeeping isn't lost on a "no templates"/"not
    #    ordered"/"resource gate" exit (the next pass's reconcile would
    #    otherwise self-heal it, but saving keeps the state honest immediately).
    # Refresh the host VM list — steps 1-5 above may have destroyed VMs (missing-
    # dongle teardown / batch-range migration), so the existing snapshot from the
    # top of the pass is stale; the audit needs the live list to tell a real
    # untracked dongle VM (occupied → not a gap) from a truly free slot.
    existing = set(await pve_cmds.list_all_vmids())
    await _vmid_gap_audit(agent, state, start, end, now, existing, present)
    save_usb_state(state)

    if not images:
        # Silent gate made loud — the #2 cause of "nothing provisions"; previously
        # returned with no log line. Surface it in the log + telemetry.
        _provision_reason = "no template ids configured"
        logger.warning("auto-provision: no clone-source template_id resolved — set "
                        "the VM Images (clone-source templates) in the Simulations UI "
                        "so VMs can be cloned")
        return {"provisioned": 0, "torn_down": len(torn_down),
                "reason": "no template ids configured"}

    # 7. resource_ok gate + prov_run-already-active + slot cap before cloning.
    in_delete_cooldown = now < cooldown_until
    ceil_hit = cpu_instant is not None and cpu_instant >= cpu_prov_ceil
    # No data yet (cold start, or get_node_stats failing) → DO NOT gate: the card
    # documents "Values apply only after a full hour of telemetry data is
    # available", so absent data means provision freely, not block. The old
    # ``cpu_avg is not None and cpu_avg < thr`` form inverted this — None → False
    # → "resource gate" fired forever on a fresh / failed-sampling agent even
    # though there was no resource pressure at all.
    resource_ok = (
        not delete_queued
        and not in_delete_cooldown
        and not ceil_hit
        and (cpu_avg is None or cpu_avg < cpu_prov_thr)
        and (mem_avg is None or mem_avg < mem_prov_thr)
    )
    if not resource_ok:
        # Pin the sub-cause so "resource gate" is self-diagnosing on the card
        # instead of a generic label — the user can't tell cpu/mem/cooldown/ceil
        # apart from "resource gate" alone (the original "should not fire" report
        # was un-diagnosable from the card).
        if in_delete_cooldown:
            sub = f"delete cooldown ({int(cooldown_until - now)}s left)"
        elif delete_queued:
            sub = "delete just queued"
        elif ceil_hit:
            sub = f"cpu ceiling {cpu_instant:.0f}% >= {cpu_prov_ceil:.0f}%"
        elif cpu_avg is not None and cpu_avg >= cpu_prov_thr:
            sub = f"cpu {cpu_avg:.0f}% >= {cpu_prov_thr:.0f}%"
        elif mem_avg is not None and mem_avg >= mem_prov_thr:
            sub = f"mem {mem_avg:.0f}% >= {mem_prov_thr:.0f}%"
        else:
            sub = "unknown"
        _provision_reason = f"resource gate ({sub})"
        logger.info(
            "auto-provision gate: suppressing clone (cpu_avg=%s mem_avg=%s "
            "cpu_instant=%s delete_queued=%s cooldown=%s ceil=%s halt=%s)",
            cpu_avg, mem_avg, cpu_instant, delete_queued, in_delete_cooldown,
            ceil_hit, usb_resource_gate._provision_halt)
        return {"provisioned": 0, "torn_down": len(torn_down),
                "reason": _provision_reason}
    if _prov_run.get("running"):
        # Stuck-run watchdog: a prior pass that never completed (a clone/reclaim
        # hung) leaves _prov_run.running=True, which would short-circuit this and
        # every future tick at this gate → permanent wedge. If the run is older
        # than PROV_RUN_STUCK_S, force-clear it and let this tick proceed. The
        # orphaned gather writes to its captured run dict, not the global.
        _st = _prov_run.get("started_at")
        if _st and (now - _st) > PROV_RUN_STUCK_S:
            logger.warning(
                "provision loop: prior run stuck %ss (started %s) — force-clearing "
                "to un-wedge (a clone/reclaim likely hung). The orphaned gather's "
                "late writes go to its own run dict, not this fresh one.",
                int(now - _st), _st)
            _prov_run = {"running": False, "items": [], "started_at": 0,
                         "total": 0, "completed": 0, "failed": 0}
        else:
            _provision_reason = "prov_run active"
            logger.info("auto-provision gate: prov_run already active — skipping trigger")
            return {"provisioned": 0, "torn_down": len(torn_down),
                    "reason": "prov_run active"}
    active_usb_vms = len(state["vmid_to_bus"])
    _provision_cfg_snapshot["active_usb_vms"] = active_usb_vms
    if active_usb_vms >= max_slots:
        _provision_reason = "slot cap reached"
        logger.info("auto-provision: slot cap reached (%d >= %d) — stop provisioning",
                    active_usb_vms, max_slots)
        return {"provisioned": 0, "torn_down": len(torn_down),
                "reason": "slot cap reached"}

    # Provisioning pass: pick unassigned, non-excluded, non-quarantined dongles
    # that match sim_phy (preferred) — plus overflow if use_all_dongles.
    quarantine = _read_quarantine()
    preferred, overflow = [], []
    # Per-bus cull reasons so "no eligible dongles" names WHICH gate dropped each
    # dongle (assigned / excluded / quarantined / type-mismatch) instead of a
    # generic label — the exact diagnosability fix the resource gate already got.
    culled: Dict[str, List[str]] = {"assigned": [], "excluded": [],
                                    "quarantined": [], "type": []}
    for bus, info in present.items():
        if state["bus_to_vmid"].get(bus):
            culled["assigned"].append(bus)
            continue
        if state["excluded_buses"].get(bus):
            culled["excluded"].append(bus)
            continue
        # Only skip a bus once it has actually reached QUARANTINE_MAX_FAILS —
        # quarantine is now dmesg-ONLY (quarantine_bus sets fails=MAX in one shot),
        # but gate on the count anyway so a partial/stale entry can't sideline a
        # good dongle. Gate on the count, not mere file presence. A PERMANENT bus
        # (QUARANTINE_PERMANENT_STRIKES reached) is always skipped — its fails is
        # kept at MAX and the recovery sweep never resets it.
        qentry = quarantine.get(bus) or {}
        if qentry.get("permanent") or int(qentry.get("fails", 0)) >= QUARANTINE_MAX_FAILS:
            culled["quarantined"].append(
                f"{bus}({qentry.get('fails')}{'/P' if qentry.get('permanent') else ''})")
            continue
        dtype = info["type"]
        if _sim_phy_accepts(sim_phy, dtype):
            preferred.append(bus)
        elif use_all and sim_phy in ("wireless", "ethernet"):
            overflow.append(bus)
        else:
            culled["type"].append(f"{bus}:{dtype}")

    ordered = preferred + overflow
    if not ordered:
        # Silent gate made loud — every dongle is assigned/excluded/quarantined,
        # type-mismatched, or none is present. Name the per-bus cause so the card
        # + log are self-diagnosing (previously an un-diagnosable generic label).
        detail = "; ".join(f"{k}={v}" for k, v in culled.items() if v)
        if not present:
            detail = "none present"
        elif not detail:
            detail = f"none match sim_phy={sim_phy}"
        _provision_reason = f"no eligible dongles ({detail})"
        logger.info("auto-provision: no eligible dongles (%s)", detail)
        return {"provisioned": 0, "torn_down": len(torn_down)}

    existing_after = set(await pve_cmds.list_all_vmids())
    img1_count = sum(1 for v in state["vmid_to_image"].values() if v == 1)
    protected = _protected_vmids(agent)

    sem = asyncio.Semaphore(concurrency)
    # Serializes the find-a-free-VMID + reserve step across concurrent _do calls
    # (reclone_concurrency > 1) so two dongles can't both pick the same vid — the
    # "two dongles both got 90078" race that desyncs bus_to_vmid/vmid_to_bus and
    # wedges the loop on a colliding clone. The clone itself runs OUTSIDE the lock
    # (only the pick+reserve is the critical section); the reservation is rolled
    # back on clone failure so the vid stays reusable.
    _alloc_lock = asyncio.Lock()
    # Stagger + CPU-ramp-ceiling pacing (bash 2786-2819) — only matters when an
    # admin explicitly raises reclone_concurrency above the sequential default,
    # so N parallel `qm clone`s don't all pile onto the host CPU at once. A
    # shared admission counter (not just the semaphore) gives every clone in
    # the batch a stable "am I the 1st/2nd/3rd..." position regardless of
    # gather()'s scheduling order, and a shared halt flag stops any clone not
    # yet admitted once the ceiling is crossed (bash reverts the whole
    # remaining batch for the same reason).
    _admission_lock = asyncio.Lock()
    _admitted = [0]
    _pacing_halted = [False]

    # prov_run live state (cs _default_provision_run_state 3576-3586): one item
    # per dongle, status provisioning → done/failed as each clone settles.
    items = [{"vmid": None, "vidpid": present[b].get("vidpid", ""),
              "bus": b, "status": "provisioning"} for b in ordered]
    this_run = {"running": True, "items": items,
                "started_at": int(now), "total": len(ordered),
                "completed": 0, "failed": 0}
    _prov_run = this_run   # publish for telemetry
    item_by_bus = {b: items[i] for i, b in enumerate(ordered)}

    async def _do(bus: str) -> bool:
        # The pacing branch below publishes a 'pacing' halt into the resource
        # gate's state (usb_resource_gate._provision_halt) so telemetry sees it.
        info = present[bus]
        async with sem:
            if _pacing_halted[0]:
                item_by_bus[bus]["status"] = "failed"
                return False
            async with _admission_lock:
                my_slot = _admitted[0]
                _admitted[0] += 1
            if my_slot > 0 and concurrency > 1:
                await asyncio.sleep(14)
                cpu_now = await _current_cpu_pct(agent)
                if cpu_now is not None and cpu_now >= cpu_prov_ceil:
                    _pacing_halted[0] = True
                    # Surface pacing in telemetry so the WebUI AUTO-PROV cell's
                    # 'pacing' branch fires (csProvThrottleBadge r==='pacing').
                    # Pacing is a transient in-batch ramp abort, not a sustained
                    # halt; the next provision-loop cycle re-evaluates cpu/mem and
                    # reassigns _provision_halt, so this shows for ~1 cycle.
                    # cpu_threshold is the ramp ceiling (cpu_prov_ceil), matching
                    # the bash agent (proxmox-agent.sh:2803). Pacing can fire on an
                    # instantaneous spike above the ceiling even when the 1h avg is
                    # below the provision threshold — in that case this is the ONLY
                    # halt signal, which is exactly when the pacing badge should show.
                    usb_resource_gate._provision_halt = {
                        "halted": True,
                        "reason": "pacing",
                        "cpu_pct": round(cpu_now, 1),
                        "cpu_threshold": cpu_prov_ceil,
                        "mem_pct": round(mem_avg, 1) if mem_avg is not None else None,
                        "mem_threshold": mem_prov_thr,
                        "ts": int(time.time()),
                    }
                    logger.warning(
                        "provision loop: pacing — CPU %.0f%% >= ramp ceiling %s%% "
                        "— stopping batch after %d clone(s)",
                        cpu_now, cpu_prov_ceil, my_slot)
                    item_by_bus[bus]["status"] = "failed"
                    return False
            # The clone-source templates (image1/2_template_id) must stay OUTSIDE
            # the allocation pool: a sim VM must never grab a template's VMID
            # (cloning "from" a sim VM, or colliding when a deleted template's
            # VMID is reused on reclone). They are fixed, cluster-consistent
            # VMIDs, not allocation candidates — exclude them explicitly in
            # addition to existing_after (which already covers present templates
            # but not a just-deleted one mid-reclone).
            templates = set()
            for _t in ([_im["template"] for _im in images] + [img1, img2]):
                try:
                    _tv = int(_t)
                    if _tv > 0:
                        templates.add(_tv)
                except (TypeError, ValueError):
                    pass
            # Find a free vmid in the sim range AND reserve it atomically BEFORE
            # the clone. A vid in Proxmox (existing_after) but NOT in our own
            # vmid_to_bus tracking is either a true zombie (a leftover from a
            # prior failed clone/destroy — bash clone_vm_for_usb's pre-clone
            # zombie cleanup, proxmox-agent.sh:1900-1949) OR a legit dongle VM
            # that fell out of the state file (e.g. a vidpid-passthrough VM
            # invisible to reconcile_bus_map/reconcile_vm_configs, which re-track
            # via _vm_usb_bus — bus-path form only). Reclaim ONLY a true zombie
            # (no usb passthrough); a real dongle VM is SKIPPED so the allocator
            # moves to the next vid and leaves the running client alone —
            # destroying it every pass was the "legit client keeps getting killed
            # / the reclaim hangs and wedges the loop" bug. The reserve-before-
            # clone (under _alloc_lock) stops two concurrent _do calls both
            # picking the same vid (the "two dongles both got 90078" race); the
            # reservation is rolled back on clone failure so the vid stays
            # reusable next pass.
            async with _alloc_lock:
                vid = start
                while vid <= end:
                    if str(vid) in state["vmid_to_bus"] or vid in templates:
                        vid += 1
                        continue
                    if vid in existing_after:
                        if vid in protected:
                            vid += 1
                            continue
                        if await _vm_has_usb_passthrough(vid):
                            # Real dongle VM, just untracked — DON'T destroy.
                            # Skip to the next vid; reconcile will eventually
                            # re-track it (or it stays harmlessly untracked).
                            vid += 1
                            continue
                        if not await _reclaim_zombie_vmid(agent, vid):
                            vid += 1
                            continue
                        existing_after.discard(vid)
                    # Reserve now, before the clone, so a concurrent _do can't
                    # grab the same vid. Rolled back on clone failure below.
                    state["vmid_to_bus"][str(vid)] = bus
                    break
            if vid > end:
                logger.info("provision loop: no free VM slot — stopping")
                item_by_bus[bus]["status"] = "failed"
                return False
            item_by_bus[bus]["vmid"] = vid
            # N-way weighted fill: pick the configured image whose current share
            # is furthest BELOW its target (ceil of pct% of the post-clone total).
            # Generalizes the old 2-image IMAGE1_PCT split to any number of images
            # (image_count + image{i}_template_id/_pct — see _resolve_images).
            total = len(state["vmid_to_image"]) + 1
            _counts = {}
            for _v in state["vmid_to_image"].values():
                try:
                    _counts[int(_v)] = _counts.get(int(_v), 0) + 1
                except (TypeError, ValueError):
                    pass
            _pick = None
            _best = None
            for _im in images:
                _deficit = ((_im["pct"] * total + 99) // 100) - _counts.get(_im["num"], 0)
                if _pick is None or _deficit > _best:
                    _pick, _best = _im, _deficit
            image_num = _pick["num"] if _pick else 1
            template = _pick["template"] if _pick else None
            if not template:
                # Roll back the pre-clone reservation — no VM was created.
                state["vmid_to_bus"].pop(str(vid), None)
                item_by_bus[bus]["status"] = "failed"
                return False
            try:
                await _clone_and_provision(agent, vid, bus, info, int(template),
                                            image_num, state)
                state["bus_to_vmid"][bus] = str(vid)
                state["vmid_to_image"][str(vid)] = image_num
                existing_after.add(vid)
                item_by_bus[bus]["status"] = "done"
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"provision loop: clone {vid} on {bus} failed: {e}")
                # Roll back the pre-clone reservation so the vid is reusable next
                # pass (the clone never produced a tracked VM).
                state["vmid_to_bus"].pop(str(vid), None)
                # Tear down the half-cloned VM so it doesn't linger as a zombie
                # (bash _teardown 1880-1906 stops+destroys+clears state on failure).
                from . import cs_sim
                try:
                    await cs_sim.destroy_vm(agent, vid, bus=bus)
                except Exception as te:  # noqa: BLE001
                    logger.warning(f"provision loop: teardown of partial {vid}: {te}")
                # Do NOT quarantine the dongle for a clone failure — a failed clone
                # is almost never the dongle's fault (VMID collision, missing
                # template, lock contention, host disk/CPU, etc.). Quarantining
                # here sidelines good dongles for non-dongle reasons and is the
                # root of the "3 idle dongles stuck, not turning up clients" bug.
                # A dongle is quarantined ONLY for kernel USB (dmesg) errors on its
                # bus — a real hardware-fault signal (step 4c above).
                item_by_bus[bus]["status"] = "failed"
                return False

    results = await asyncio.gather(*[_do(b) for b in ordered], return_exceptions=True)
    save_usb_state(state)
    provisioned = sum(1 for r in results if r is True)
    # Write to THIS run's dict (captured at publish), not the global _prov_run.
    # If the stuck-run watchdog reassigns the global to a fresh run while this
    # gather was hung, writing to the global here would clobber the fresh run;
    # this_run keeps the orphan's late writes isolated.
    this_run["running"] = False
    this_run["completed"] = provisioned
    this_run["failed"] = len(ordered) - provisioned
    this_run["completed_at"] = int(time.time())
    _provision_reason = f"provisioning: attempted {len(ordered)}, provisioned {provisioned}"
    return {"provisioned": provisioned, "torn_down": len(torn_down),
            "attempted": len(ordered)}


async def _vmid_gap_audit(agent, state: Dict[str, Any],
                          start: int, end: int, now: float,
                          existing: Set[int],
                          present: Dict[str, Any]) -> None:
    """Detect VMID gaps in the assigned sim range and delete the highest VMID
    above the lowest gap so the next provision pass refills the hole (cs
    10256-10324). Runs at most once per ``VMID_AUDIT_INTERVAL_S``; bypasses the
    delete-gate cooldown (corrective bookkeeping, not load-shedding).

    Two guards keep this a true compaction toward a dense prefix from ``start``
    (N dongles → 90001…9000N) instead of legit-VM churn:

    - **No shed when a dongle can refill the gap.** If any present dongle is
      unassigned, the allocator refills the lowest gap on its own — now if the
      dongle is eligible, or once a bus exclusion/quarantine on it clears. The
      shed is only needed when EVERY present dongle is already assigned
      (full-but-sparse, e.g. 8 dongles all on 90010-90017 with 90001-90009
      empty): the only way to fill the low gap is to free a high slot. Spares
      (dongles > max_slots) and a dongle shortage (dongles < max_slots) both
      fall out of the same "unassigned present dongle → refill, don't shed" rule.

    - **An occupied-but-untracked low vid is not a gap.** A vid on the host with
      a USB passthrough but absent from ``vmid_to_bus`` is a real (untracked
      vidpid-form) dongle VM the allocator now SKIPS (pxmx 96d2144). Counting it
      as a gap made the audit shed the highest tracked VM every pass trying to
      "fill" an occupied slot — perpetual churn. It's filled, not a gap (same
      ``_vm_has_usb_passthrough`` test the allocator uses)."""
    # Offload the vmid-gap state file I/O off the event loop — on a busy
    # Proxmox host with contended storage even this tiny read/write can stall
    # the loop long enough to miss the ACCEPTED window for a relayed command
    # (py-spy caught the loop parked in json.load here during a bulk delete).
    if now - await asyncio.to_thread(_load_vmid_gap_last_run) < VMID_AUDIT_INTERVAL_S:
        return
    await asyncio.to_thread(_save_vmid_gap_last_run, now)
    # Guard 1 — don't churn a legit high VM when a dongle can refill the gap. A
    # present dongle whose bus isn't in bus_to_vmid is unassigned; the allocator
    # will refill the lowest gap from it (now if eligible, or when an
    # exclusion/quarantine on it clears). Only compact when every present dongle
    # is already assigned. A bus still in bus_to_vmid counts as assigned
    # (missing-dongle teardown only drops it after missing_timeout, not same
    # tick), so a dongle that just unplugged doesn't read as "unassigned" here.
    bus_to_vmid = state.get("bus_to_vmid") or {}
    if any(bus not in bus_to_vmid for bus in present):
        return
    try:
        assigned = sorted(int(v) for v in state["vmid_to_bus"].keys()
                          if start <= int(v) <= end)
    except (TypeError, ValueError):
        return
    # Active = not currently provisioning (in-flight clones aren't stable yet).
    provisioning = _provisioning_vmids()
    active = [v for v in assigned if v not in provisioning]
    if len(active) < 2:
        return
    active_set = set(active)
    gap_max = active[-1]
    lowest_gap: Optional[int] = None
    for chk in range(start, gap_max):
        if chk in active_set:
            continue
        # Guard 2 — a vid on the host with a USB passthrough but NOT tracked is a
        # real untracked (vidpid-form) dongle VM: occupied, not a fillable gap.
        # Skip it (the allocator skips it too); otherwise the audit sheds the
        # highest tracked VM every pass trying to fill an occupied slot. A vid
        # not on the host is truly free → a real gap. Only on-host vids cost a
        # qm_config, so the stale-existing race is limited to a 1-tick miss.
        if chk in existing and await _vm_has_usb_passthrough(chk):
            continue
        lowest_gap = chk
        break
    if lowest_gap is None:
        return
    above_gap = [v for v in active if v > lowest_gap]
    if not above_gap:
        return
    target = max(above_gap)
    bus = state["vmid_to_bus"].get(str(target))
    if not bus:
        return
    from . import cs_sim  # deferred — cs_sim imports usb_provision
    try:
        await cs_sim.destroy_vm(agent, target, bus=bus)
        state["bus_to_vmid"].pop(bus, None)
        state["vmid_to_bus"].pop(str(target), None)
        state["vmid_to_image"].pop(str(target), None)
        state["missing_since"].pop(bus, None)
        logger.info("auto-provision vmid-gap audit: deleted VM %s to fill gap at %s",
                    target, lowest_gap)
    except Exception as e:  # noqa: BLE001
        logger.warning("auto-provision vmid-gap audit: delete %s failed: %s",
                       target, e)


async def _clone_and_provision(agent, vmid: int, bus: str,
                                info: Dict[str, Any], template: int,
                                image_num: int, state: Dict[str, Any]) -> None:
    """Clone a template → vmid, attach the USB dongle, start, wait for the guest
    agent, set the hostname (bash ``clone_vm_for_usb`` 1943-2098, slimmed).

    ``state`` is the caller's in-memory usb_state (shared, not reloaded from
    disk here) — a post-prov-retry entry is written straight into it rather
    than through its own load/save round trip, because the caller's `_do()`
    saves the WHOLE state object once at the end of the tick; a separate
    load+save here would get silently overwritten by that later blanket save.
    """
    from . import pve_cmds
    protected = _protected_vmids(agent)
    name = _vm_name(vmid) or f"sim-{vmid}-{info.get('type', 'wireless')}"
    await pve_cmds.qm_clone(template, vmid, name, protected=protected, timeout=600)
    await pve_cmds.qm_set(vmid, "--onboot", "1", "--startup", "order=2,up=60",
                         protected=protected)
    await pve_cmds.qm_set(vmid, "-usb0", f"host={bus}", protected=protected)
    # Optional VLAN NIC (usb_cfg.vlan_nic e.g. "vlan20") — best-effort.
    vlan = (agent.config.get("client_simulation") or {}).get("usb_config", {}).get("vlan_nic")
    if vlan:
        await pve_cmds.qm_set(vmid, "-net0", f"virtio,bridge={vlan}",
                              protected=protected)
    await pve_cmds.qm_start(vmid, protected=protected)
    # Wait for the guest agent (bounded — bash waits ~10 min via 40 pings at
    # `timeout 10` ping + `sleep 5` ≈ 15s/iter, proxmox-agent.sh 1999-2009).
    for _ in range(40):
        if await pve_cmds.qm_agent_ping(vmid, protected=protected, timeout=10):
            break
        await asyncio.sleep(5)
    # Set the hostname inside the guest — write /etc/hostname + /etc/hosts +
    # cloud-init preserve_hostname via `qm guest exec --timeout 60 -- bash -c`
    # (bash 2025-2036). hostnamectl is deliberately avoided (D-Bus may be
    # unready post-boot and can hang the task). Retry up to 3× like bash.
    dtype = info.get("type", "wireless")
    host_script = (
        f"echo '{name}' > /etc/hostname; "
        f"sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1\\t{name}/' /etc/hosts 2>/dev/null || true; "
        "mkdir -p /etc/cloud/cloud.cfg.d; "
        "echo 'preserve_hostname: true' > /etc/cloud/cloud.cfg.d/99_preserve_hostname.cfg; "
        "rm -f /var/lib/cloud/sem/config_set_hostname 2>/dev/null || true"
    )
    for _ in range(3):
        if await pve_cmds.qm_guest_exec_shell(vmid, host_script, exec_timeout=60,
                                              outer_timeout=90, protected=protected):
            break
        await asyncio.sleep(5)
    # Best-effort: tell the guest's startup.sh which USB phy type it's bound to
    # (bash 2046). Best-effort — /usr/local/scripts may not exist on every image.
    # qm_guest_exec_shell is a coroutine — missing await here meant this was
    # created and immediately discarded, so the guest never actually received
    # this write and startup.sh could never see which phy type it was bound to.
    await pve_cmds.qm_guest_exec_shell(
        vmid, f"echo 'sim_phy={dtype}' > /usr/local/scripts/usb-phy-override.conf",
        exec_timeout=60, outer_timeout=90, protected=protected)
    set_assignment(vmid, bus, image_num)
    remove_orphan_vm(vmid)

    # Reboot so the guest picks up the hostname/sim_phy changes, then run
    # update.sh once it comes back — it needs the latest scripts before
    # startup.sh runs for the first time (bash clone_vm_for_usb 2050-2097).
    # `qm guest exec ... reboot` never replies (the guest agent dies with the
    # reboot), so this is fire-and-forget like bash's `|| true`.
    try:
        await pve_cmds.qm_guest_exec(vmid, "reboot", protected=protected)
    except Exception:
        pass
    came_back = False
    deadline = time.time() + 300  # bash's 5-minute reboot deadline
    while time.time() < deadline:
        await asyncio.sleep(5)
        if await pve_cmds.qm_agent_ping(vmid, protected=protected, timeout=10):
            came_back = True
            break
    if came_back:
        try:
            await pve_cmds.qm_guest_exec_shell(
                vmid, "bash /usr/local/scripts/update.sh",
                exec_timeout=300, outer_timeout=360, protected=protected)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"provision loop: update.sh failed on VM {vmid} "
                          f"(will retry on next boot): {e}")
    else:
        # Guest didn't come back in time — queue for the post-prov retry loop
        # instead of failing the whole provision (bash 2086-2097): check every
        # 10 min, reclone after 1h if it never responds.
        now_ts = time.time()
        state.setdefault("post_prov_retry", {})[str(vmid)] = {
            "start_ts": now_ts, "last_ts": now_ts, "bus": bus,
            "image_num": image_num, "device_type": dtype,
        }
        logger.warning(f"provision loop: VM {vmid} did not come back after reboot "
                       "— queued for post-prov retry (10-min interval, reclone after 1h)")


async def _run_post_prov_retry_queue(agent, state: Dict[str, Any]) -> bool:
    """Retry VMs whose post-clone reboot didn't come back in time (bash
    ``_run_post_prov_retry_queue``, proxmox-agent.sh:2188-2272). Every 10
    minutes: ping the guest again; if it responds, run update.sh and clear
    the retry entry; past 1 hour unresponsive, destroy the VM so the normal
    provisioning pass reclones it fresh. Returns True if ``state`` was
    mutated (caller should persist it)."""
    retry = state.get("post_prov_retry") or {}
    if not retry:
        return False
    from . import cs_sim, pve_cmds  # local: pve_cmds is imported per-function
    protected = _protected_vmids(agent)
    now = time.time()
    mutated = False
    for vmid_s, entry in list(retry.items()):
        vmid = int(vmid_s)
        if now - float(entry.get("last_ts", 0)) < 600:
            continue
        bus = entry.get("bus")
        if state["vmid_to_bus"].get(vmid_s) != bus:
            logger.info(f"post-prov retry: VM {vmid} bus mismatch (stale entry) — dropping")
            retry.pop(vmid_s, None)
            mutated = True
            continue
        cfg = await pve_cmds.qm_config(vmid)
        if not cfg:
            logger.info(f"post-prov retry: VM {vmid} no longer exists — dropping retry entry")
            retry.pop(vmid_s, None)
            mutated = True
            continue
        elapsed = now - float(entry.get("start_ts", now))
        responded = await pve_cmds.qm_agent_ping(vmid, protected=protected, timeout=10)
        if responded:
            logger.info(f"post-prov retry: VM {vmid} guest agent responded after "
                       f"{int(elapsed)}s — running update.sh")
            try:
                await pve_cmds.qm_guest_exec_shell(
                    vmid, "bash /usr/local/scripts/update.sh",
                    exec_timeout=300, outer_timeout=360, protected=protected)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"post-prov retry: update.sh failed on VM {vmid}: {e}")
            retry.pop(vmid_s, None)
            mutated = True
        elif elapsed > 3600:
            logger.warning(f"post-prov retry: VM {vmid} unresponsive for >1h — "
                           "destroying; provision loop will reclone")
            retry.pop(vmid_s, None)
            mutated = True
            try:
                await cs_sim.destroy_vm(agent, vmid, bus=bus, protected=protected)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"post-prov retry: destroy of {vmid} failed: {e}")
            # destroy_vm persists its own clear_assignment write independently;
            # mirror it into OUR in-memory state too so the caller's very next
            # save_usb_state(state) call doesn't stomp that write with a stale
            # copy that still shows this vmid assigned (same pattern as the
            # out-of-range batch-migration teardown above).
            state["bus_to_vmid"].pop(bus, None)
            state["vmid_to_bus"].pop(vmid_s, None)
            state["vmid_to_image"].pop(vmid_s, None)
            state["missing_since"].pop(bus, None)
            state.get("vidpid_by_bus", {}).pop(bus, None)
        else:
            entry["last_ts"] = now
            mutated = True
    return mutated


def _protected_vmids(agent) -> Set[int]:
    from .cs_guard import resolve_protected_vmids
    return resolve_protected_vmids(agent.config.get("client_simulation"))


async def _is_runnable_template(vmid: int) -> bool:
    """True if ``vmid`` exists and is marked as a Proxmox template (bash
    vmid_is_runnable_template: ``qm status`` succeeds + ``template: 1`` in
    config). ``qm_config`` already returns ``{}`` for a nonexistent vmid, so
    a missing/deleted template id falls straight through to False."""
    from . import pve_cmds  # local: pve_cmds is imported per-function
    cfg = await pve_cmds.qm_config(vmid)
    return cfg.get("template") == "1"


def _normalize_image_pcts(images: list) -> None:
    """Coerce each image's ``pct`` to 0-100. Images with a missing/invalid pct
    split the remaining share evenly; if NOTHING is set, split 100 evenly. Keeps
    the weighted-fill selection sane regardless of what the UI/config sent."""
    if not images:
        return
    unassigned = []
    assigned = 0
    for im in images:
        try:
            im["pct"] = max(0, min(100, int(im.get("pct"))))
            assigned += im["pct"]
        except (TypeError, ValueError):
            im["pct"] = None
            unassigned.append(im)
    if unassigned:
        rem = max(0, 100 - assigned)
        share = rem // len(unassigned)
        for im in unassigned:
            im["pct"] = share
        unassigned[-1]["pct"] += rem - share * len(unassigned)
    if sum(im["pct"] for im in images) == 0:
        share = 100 // len(images)
        for im in images:
            im["pct"] = share
        images[-1]["pct"] += 100 - share * len(images)


async def _resolve_images(usb_cfg: Dict[str, Any], legacy_img1: Any = None,
                          legacy_img2: Any = None, legacy_img1_pct: int = 50) -> list:
    """Resolve the configured clone-source images into an ordered list of
    ``{"num": i, "template": <vmid>, "pct": <int>}``.

    Generic shape: ``image_count`` + ``image{i}_template_id`` + ``image{i}_pct``
    (i = 1..count). Legacy fallback (image_count absent/<=0): the already-resolved
    ``image1``/``image2`` + ``image1_pct`` pair (image2 gets the remaining %).
    Images whose template can't be resolved are dropped; pcts are normalized to
    sum ~100 so the fleet fills to the intended proportions."""
    try:
        count = int(usb_cfg.get("image_count") or 0)
    except (TypeError, ValueError):
        count = 0
    images: list = []
    if count > 0:
        for i in range(1, count + 1):
            t = await _resolve_template_vmid(usb_cfg.get(f"image{i}_template_id"))
            if not t:
                continue
            images.append({"num": i, "template": int(t), "pct": usb_cfg.get(f"image{i}_pct")})
    else:
        if legacy_img1:
            images.append({"num": 1, "template": int(legacy_img1), "pct": legacy_img1_pct})
        if legacy_img2:
            images.append({"num": 2, "template": int(legacy_img2), "pct": None})
    _normalize_image_pcts(images)
    return images


async def _resolve_template_vmid(configured: Any) -> Optional[int]:
    """Resolve the configured clone-source — accepts EITHER a vmid (numeric) OR
    a template NAME (text). Returns the resolved vmid, or None when nothing is
    configured (callers' existing "no template configured" gates still apply)
    or when a NAME can't be resolved to exactly one vmid.

    Numeric entry (``100``, ``101``): the original behavior — use the vmid
    directly if it exists (template-flagged or NOT; ``qm clone`` clones a plain
    stopped VM just as well), else fall back to the lowest-numbered
    ``template: 1`` VM on the cluster (bash resolve_template_vmid,
    proxmox-agent.sh:933-944). A missing numeric id is recoverable, so the
    fallback is appropriate.

    Name entry (text with chars+digits, e.g. ``debian-12-template``): look up
    the vmid whose ``qm list`` NAME matches exactly, on THIS host.
      * exactly one match  → that vmid.
      * no match           → log an error + return None (NO silent fallback to
        a random template — a name typo shouldn't clone from the wrong image).
      * multiple matches   → log an error + return None (Proxmox names aren't
        unique; refuse to pick rather than clone from the wrong source). The
        operator must make the name unique (rename one VM) or use the VMID."""
    from . import pve_cmds  # local: pve_cmds is imported per-function
    raw = str(configured).strip() if configured is not None else ""
    if not raw:
        return None
    # ── numeric → vmid lookup (original path) ─────────────────────────────
    if raw.lstrip("-").isdigit():
        cvid = int(raw)
        # Exists as any VM (qm_config returns {} only for a nonexistent vmid) →
        # use it directly, template-flagged or not.
        if await pve_cmds.qm_config(cvid):
            return cvid
        logger.warning(f"provision loop: configured clone-source vmid {cvid} does "
                       "not exist on this host — searching the cluster for a "
                       "template to fall back to")
        for vid in sorted(await pve_cmds.list_qemu_vmids()):
            if vid != cvid and await _is_runnable_template(vid):
                logger.warning(f"provision loop: falling back to template vmid {vid}")
                return vid
        logger.error(f"provision loop: clone-source vmid {cvid} does not exist and "
                    "no template was found on the cluster — clones will fail")
        return None
    # ── non-numeric → resolve by NAME across qemu VMs on this host ────────
    matches = [vid for vid, name in await pve_cmds.list_qemu_vms()
               if name == raw]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        logger.error(f"provision loop: configured clone-source template name "
                    f"{raw!r} not found on this host — clones will fail "
                    "(check the VM name / use the VMID instead)")
        return None
    logger.error(f"provision loop: configured clone-source template name "
                f"{raw!r} matches multiple vmids {sorted(matches)} — refusing "
                "to pick; clones will fail (make the template name unique or "
                "use the VMID)")
    return None


async def _reclaim_zombie_vmid(agent, vid: int) -> bool:
    """Force-destroy a leftover VM config at ``vid`` that Proxmox still has
    but our own state no longer tracks — almost always a zombie left behind
    by a prior failed clone/destroy (bash clone_vm_for_usb's pre-clone
    zombie cleanup, proxmox-agent.sh:1900-1949). Returns True if ``vid`` is
    now free to clone into; False to leave it alone and try the next vmid
    (mirrors bash giving up on this one vmid, not the whole dongle)."""
    from . import cs_sim
    try:
        r = await cs_sim.destroy_vm(agent, vid)
    except Exception as e:  # noqa: BLE001 — GuardError or a pve_cmds failure
        logger.warning(f"provision loop: zombie reclaim of {vid} failed: {e}")
        return False
    if r.get("ok"):
        logger.warning(f"provision loop: reclaimed zombie VMID {vid} "
                       "(existed in Proxmox with no tracked assignment)")
        return True
    logger.warning(f"provision loop: could not destroy zombie VMID {vid} — skipping")
    return False