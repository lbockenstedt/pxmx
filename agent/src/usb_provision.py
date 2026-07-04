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
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("PxmxAgent")

PXMLIB = "/var/lib/pxmx"
ORPHAN_VMS_FILE = f"{PXMLIB}/orphan_vms.json"
DONGLE_BLACKLIST_CONF = "/etc/modprobe.d/cs-dongle-blacklist.conf"
DESTROY_MAX_FAILS = 3  # bash line 43, hardcoded

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


# ── auto-provisioning brain (cs webui-spoke/server.py brain-loop port) ────
# The cs spoke's brain gates cloning on the ``usb_auto_provision`` toggle and
# host resource thresholds, and can auto-delete the newest sim VM under load
# (cs ``server.py`` 10020-10294 + ``proxmox-agent.sh`` 2648-2666/5005-5060). In
# the LM topology the cs spoke is only a relay, so the brain runs here, inside
# the pxmx agent's ``run_provision_loop`` (called every ~60s by
# ``_usb_provision_loop``). The hub side (toggle/store/push/status) is already
# complete; this is the missing consumer.

_RESOURCE_SAMPLE_WINDOW = 3600  # 1h rolling window (cs _RESOURCE_SAMPLE_WINDOW)
DELETE_GATE_COOLDOWN_S = 300    # cs line 3615
VMID_AUDIT_INTERVAL_S = 300    # cs line 3619

DELETE_GATE_FILE = f"{PXMLIB}/delete_gate.json"
VMID_AUDIT_FILE = f"{PXMLIB}/vmid_audit.json"

# Rolling resource samples pruned to the 1h window: [(ts, pct), ...].
_cpu_samples: Deque[Tuple[float, float]] = deque()
_mem_samples: Deque[Tuple[float, float]] = deque()

# In-process brain state, reported up via telemetry (rebuilt each pass).
_provision_halt: Optional[Dict[str, Any]] = None
_prov_run: Dict[str, Any] = {"running": False, "items": []}

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


async def sample_resources(agent) -> None:
    """Append a CPU + memory sample to the rolling 1h deques, sourced from the
    SAME Proxmox node figures the CS telemetry displays (``get_node_stats`` →
    /cluster/resources: ``cpu_usage`` + ``mem_pct``). Called once per
    ``_usb_provision_loop`` tick. Best-effort — a failure leaves the deques
    untouched (averages degrade to None/cold-start, which the gate treats as
    "no data yet → don't block", matching the card's "applies only after a full
    hour" help text).

    Why Proxmox not psutil: the gate used to read ``psutil.virtual_memory()`` /
    ``cpu_percent`` (the agent OS view), but the user-visible CPU/Mem 1h tiles
    read Proxmox's own node stats. On a Proxmox host the two diverge — esp.
    memory, where psutil counts VM RAM + page cache as "used" and routinely
    reads 80%+ while Proxmox's ``mem_used`` reads far lower — so the gate fired
    "resource gate" while the card showed low load. Sourcing the gate from the
    same Proxmox figures makes "below threshold" mean below threshold. Uses
    ``nodes[0]`` to mirror ``_cs_telemetry_body`` so the gate sees exactly what
    the card renders."""
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
        _cpu_samples.append((now, cpu_pct))
        _cpu_samples[:] = [(ts, v) for ts, v in _cpu_samples if ts >= cutoff]
        _mem_samples.append((now, mem_pct))
        _mem_samples[:] = [(ts, v) for ts, v in _mem_samples if ts >= cutoff]
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


def _resource_1h_average(samples: Deque[Tuple[float, float]]) -> Optional[float]:
    if not samples:
        return None
    cutoff = time.time() - _RESOURCE_SAMPLE_WINDOW
    recent = [v for ts, v in samples if ts >= cutoff]
    return (sum(recent) / len(recent)) if recent else None


def current_provision_halt() -> Optional[Dict[str, Any]]:
    """Agent-computed resource halt (``{halted, reason}`` or ``None``) for the
    telemetry body. Set by ``run_provision_loop`` when cpu/mem cross the
    provision threshold (cs ``proxmox-agent.sh`` 2648-2666 writes the cache)."""
    return _provision_halt


def current_prov_run() -> Dict[str, Any]:
    """Live provision-run state (``{running, items:[{vmid,vidpid,status}]}``)
    for the telemetry body (cs ``_default_provision_run_state`` 3576-3586)."""
    return dict(_prov_run)


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


# ── Orphan-VM registry ─────────────────────────────────────────────────────

def _read_orphans() -> List[Dict[str, Any]]:
    try:
        if os.path.exists(ORPHAN_VMS_FILE) and os.path.getsize(ORPHAN_VMS_FILE) > 0:
            with open(ORPHAN_VMS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _write_orphans(entries: List[Dict[str, Any]]) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(ORPHAN_VMS_FILE, "w") as f:
            json.dump(entries, f)
    except OSError as e:
        logger.warning(f"orphan registry: cannot write {ORPHAN_VMS_FILE}: {e}")


def add_orphan_vm(vmid: int, bus: str) -> Dict[str, Any]:
    """Declare a VM an orphan: dedup by vmid and append to the registry
    (bash 1335-1346). Caller (Phase E destroy path) force-releases the bus."""
    entries = _read_orphans()
    entries = [e for e in entries if int(e.get("vmid", -1)) != int(vmid)]
    entries.append({"vmid": int(vmid), "bus": bus, "ts": int(time.time())})
    _write_orphans(entries)
    logger.error(f"VM {vmid} declared orphan (bus {bus}) — released for re-provisioning")
    return {"vmid": int(vmid), "bus": bus, "orphaned": True}


def remove_orphan_vm(vmid: int) -> None:
    """Drop a vmid from the orphan registry (bash 1358-1371) — called when a VM
    is later successfully re-provisioned or destroyed."""
    entries = [e for e in _read_orphans() if int(e.get("vmid", -1)) != int(vmid)]
    _write_orphans(entries)


def get_orphan_vms() -> List[Dict[str, Any]]:
    """Snapshot of the orphan registry (surfaced in CS telemetry, Phase E)."""
    return _read_orphans()


# ── USB provision state + loop (Phase E) ──────────────────────────────────
#
# Ports the host-side state machine from ``cs/proxmox/proxmox-agent.sh``
# (``_usb_provision_loop_impl`` 2530-2914, ``clone_vm_for_usb`` 1868-2101). The
# bash agent kept its state in associative arrays + a flock-guarded state file;
# here it is one JSON document under /var/lib/pxmx/usb_state.json:
#
#   vmid_to_bus   {str(vmid): bus_path}     which sim VM holds which dongle
#   bus_to_vmid   {bus_path: str(vmid)}     reverse map
#   vmid_to_image {str(vmid): 1|2}          which template image it was cloned from
#   excluded_buses {bus_path: 1}           hub-deleted → skip provisioning
#   quarantined   {bus_path: {fails, since}} too many provision failures → skip
#   missing_since {bus_path: ts}            when a bound dongle disappeared
#
# Single asyncio event loop → no lock needed (the only writers are the provision
# loop and the delete/reclone long-op tasks, both on the same loop).

USB_STATE_FILE = f"{PXMLIB}/usb_state.json"
USB_QUARANTINE_FILE = f"{PXMLIB}/usb_quarantine.json"
DESTROY_FAILS_FILE = f"{PXMLIB}/destroy_fails.json"
QUARANTINE_MAX_FAILS = 3  # bash line 1217: a bus is quarantined after 3 fails


def _new_usb_state() -> Dict[str, Any]:
    return {"vmid_to_bus": {}, "bus_to_vmid": {}, "vmid_to_image": {},
            "excluded_buses": {}, "quarantined": {}, "missing_since": {},
            "vidpid_by_bus": {}, "post_prov_retry": {}}


def load_usb_state() -> Dict[str, Any]:
    try:
        if os.path.exists(USB_STATE_FILE) and os.path.getsize(USB_STATE_FILE) > 0:
            with open(USB_STATE_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                st = _new_usb_state()
                st.update({k: (v if isinstance(v, dict) else {}) for k, v in data.items()})
                return st
    except (OSError, json.JSONDecodeError):
        pass
    return _new_usb_state()


def save_usb_state(state: Dict[str, Any]) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(USB_STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning(f"usb_state: cannot write {USB_STATE_FILE}: {e}")


def clear_assignment(vmid: int, bus: Optional[str] = None) -> None:
    """Drop a vmid↔bus assignment from the USB state (bash 2166-2172)."""
    st = load_usb_state()
    b = st["vmid_to_bus"].pop(str(int(vmid)), None) or bus
    if b:
        st["bus_to_vmid"].pop(b, None)
        st["missing_since"].pop(b, None)
    st["vmid_to_image"].pop(str(int(vmid)), None)
    save_usb_state(st)


def set_assignment(vmid: int, bus: str, image_num: int) -> None:
    st = load_usb_state()
    st["vmid_to_bus"][str(int(vmid))] = bus
    st["bus_to_vmid"][bus] = str(int(vmid))
    st["vmid_to_image"][str(int(vmid))] = int(image_num)
    st["missing_since"].pop(bus, None)
    save_usb_state(st)


def bus_for_vmid(vmid: int) -> Optional[str]:
    return load_usb_state()["vmid_to_bus"].get(str(int(vmid)))


def clear_excluded_buses() -> int:
    """Wipe all bus exclusions (bash ``provision_unassigned`` dispatch 4078-4084).
    Returns the count cleared."""
    st = load_usb_state()
    n = len(st.get("excluded_buses", {}))
    st["excluded_buses"] = {}
    save_usb_state(st)
    return n


def exclude_bus(bus: str) -> None:
    st = load_usb_state()
    st["excluded_buses"][bus] = 1
    save_usb_state(st)


def clear_quarantine(bus: Optional[str] = None) -> None:
    path = USB_QUARANTINE_FILE
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        if bus:
            data: Dict[str, Any] = {}
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path) as f:
                    data = json.load(f) or {}
            data.pop(bus, None)
            with open(path, "w") as f:
                json.dump(data, f)
        else:
            with open(path, "w") as f:
                json.dump({}, f)
    except (OSError, json.JSONDecodeError):
        pass


def _read_quarantine() -> Dict[str, Any]:
    try:
        if os.path.exists(USB_QUARANTINE_FILE) and os.path.getsize(USB_QUARANTINE_FILE) > 0:
            with open(USB_QUARANTINE_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_quarantine(d: Dict[str, Any]) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(USB_QUARANTINE_FILE, "w") as f:
            json.dump(d, f)
    except OSError:
        pass


def record_usb_failure(bus: str) -> int:
    """Increment a bus's provision-failure count; quarantine past the threshold
    (bash 1211-1268). Returns the new count."""
    q = _read_quarantine()
    entry = q.get(bus) or {"fails": 0, "since": int(time.time())}
    entry["fails"] = int(entry.get("fails", 0)) + 1
    entry["since"] = int(time.time())
    q[bus] = entry
    _save_quarantine(q)
    return entry["fails"]


def _read_destroy_fails() -> Dict[str, int]:
    try:
        if os.path.exists(DESTROY_FAILS_FILE) and os.path.getsize(DESTROY_FAILS_FILE) > 0:
            with open(DESTROY_FAILS_FILE) as f:
                d = json.load(f)
            return {str(k): int(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_destroy_fails(d: Dict[str, int]) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(DESTROY_FAILS_FILE, "w") as f:
            json.dump(d, f)
    except OSError:
        pass


def record_destroy_fail(vmid: int, bus: str) -> Dict[str, Any]:
    """Count a destroy failure; on reaching DESTROY_MAX_FAILS declare the VM an
    orphan (bash 1321-1351). Returns {count, orphaned}."""
    fails = _read_destroy_fails()
    count = int(fails.get(str(int(vmid)), 0)) + 1
    fails[str(int(vmid))] = count
    orphaned = count >= DESTROY_MAX_FAILS
    if orphaned:
        fails.pop(str(int(vmid)), None)
        add_orphan_vm(int(vmid), bus)
    _save_destroy_fails(fails)
    return {"count": count, "orphaned": orphaned}


def clear_destroy_fails(vmid: int) -> None:
    fails = _read_destroy_fails()
    fails.pop(str(int(vmid)), None)
    _save_destroy_fails(fails)


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
    empty: Dict[str, List[Dict[str, Any]]] = {"usb_state": [], "present_usb": [], "unknown_usb": []}
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
        for bus, vmid in (st.get("bus_to_vmid") or {}).items():
            pe = present_by_bus.get(bus) or {}
            usb_state.append({
                "vmid": vmid,
                "bus_path": bus,
                "missing_since": missing_since.get(bus),
                "name": pe.get("product") or bus,
                "vidpid": pe.get("vidpid") or "",
                "prov_status": "missing" if missing_since.get(bus) is not None else "active",
            })
        return {"usb_state": usb_state, "present_usb": present, "unknown_usb": unknown}
    except Exception as exc:  # noqa: BLE001
        logger.warning("cs_usb_telemetry: failed: %s", exc)
        return empty


def _sim_phy_accepts(sim_phy: str, device_type: str) -> bool:
    # sim_phy is the sim VM's required physical layer (cs domain:
    # wireless | ethernet | any). device_type is the dongle class from the
    # LM usb_vidpids `type` field (wireless | wired | storage | other). A sim
    # requiring "ethernet" wants a *wired* dongle — map wired <-> ethernet so
    # the wired/wireless selector the tenant sets in LM is actually enforced.
    # "storage"/"other" only match sim_phy == "any".
    if sim_phy == "any":
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
    global _provision_halt, _prov_run, _provision_reason, _provision_cfg_snapshot, \
        _provision_loop_last_run, _auto_provision_on
    from . import pve_cmds  # local to avoid a top-level import cycle
    cs_cfg = agent.config.get("client_simulation") or {}
    usb_cfg = cs_cfg.get("usb_config") or {}
    dongle_vidpids = _dongle_vidpids(agent)
    # Heartbeat: the loop is alive. Stamped before any gate so
    # current_provision_loop_running() flips true on the very first tick (lets the
    # UI distinguish "loop not running" from "loop running but gated").
    _provision_loop_last_run = time.time()
    if not dongle_vidpids:
        # Silent gate made loud — this is the #1 cause of "nothing provisions" and
        # previously left no log line at all. Surface it in the log + telemetry.
        _provision_reason = "no dongle_vidpids configured"
        _provision_cfg_snapshot = {"dongle_vidpids": 0, "image1_template_id": False,
                                    "image2_template_id": False, "max_slots": None,
                                    "vmid_range": {}, "active_usb_vms": None}
        logger.warning("auto-provision: no dongle_vidpids configured — certify USB "
                       "vid:pid values in the Simulations UI so dongles can be matched")
        return {"provisioned": 0, "torn_down": 0, "reason": "no dongle_vidpids configured"}

    ap_on = _toggle_on(usb_cfg)
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
    cpu_avg = _resource_1h_average(_cpu_samples)
    mem_avg = _resource_1h_average(_mem_samples)
    cpu_instant = _cpu_samples[-1][1] if _cpu_samples else None

    state = load_usb_state()
    existing = set(await pve_cmds.list_all_vmids())
    present = scan_present_dongles(dongle_vidpids, certified_types)
    now = time.time()

    # 1. Reconcile: release buses whose VM no longer exists.
    for vmid, bus in list(state["vmid_to_bus"].items()):
        if int(vmid) not in existing:
            state["bus_to_vmid"].pop(bus, None)
            state["vmid_to_bus"].pop(vmid, None)
            state["vmid_to_image"].pop(vmid, None)
            state.setdefault("vidpid_by_bus", {}).pop(bus, None)
    # Remember each tracked bus's vidpid while it's actually present, so a
    # dongle that later moves to a different bus path (unplugged/replugged
    # into a different physical port) can still be matched by vidpid below
    # (bash build_usb_state_json 1565-1572: "use live-scanned vidpid... update
    # stored value so it persists after the dongle goes physically missing").
    for bus in list(state["bus_to_vmid"]):
        if bus in present:
            state.setdefault("vidpid_by_bus", {})[bus] = present[bus].get("vidpid")
    # Clear exclusions/quarantine for buses no longer present.
    for bus in list(state["excluded_buses"]):
        if bus not in present:
            state["excluded_buses"].pop(bus, None)
    # Auto-clear quarantine only after it's been BOTH absent AND quarantined
    # for >= 2x missing_timeout (bash load_usb_quarantine, proxmox-agent.sh:
    # 1231-1244) — clearing the instant a quarantined dongle is merely
    # unplugged defeated the point of quarantine: a flaky/bad dongle bouncing
    # in and out would get a fresh provisioning attempt on every replug.
    quarantine = _read_quarantine()
    for bus in list(quarantine):
        if bus in present:
            continue
        since = (quarantine[bus] or {}).get("since")
        if missing_timeout > 0 and since is not None and \
                now - float(since) >= missing_timeout * 2:
            quarantine.pop(bus, None)
            logger.info(f"provision loop: quarantine auto-cleared for {bus} "
                       f"(absent >= {missing_timeout * 2}s since last failure)")
    _save_quarantine(quarantine)

    # 1c. Post-provisioning retry queue — runs unconditionally (matches bash
    # calling _run_post_prov_retry_queue independently of the AUTO_PROVISION
    # toggle, proxmox-agent.sh:5068): a VM already cloned before the toggle
    # was switched off still deserves its update.sh retry / 1h reclone.
    if await _run_post_prov_retry_queue(agent, state):
        save_usb_state(state)

    torn_down: List[int] = []

    # 2. Toggle gate — off = telemetry-only (no VM mutations).
    if not ap_on:
        save_usb_state(state)
        _provision_halt = None
        _provision_reason = "auto-provision disabled"
        logger.info("auto-provision: usb_auto_provision=off — telemetry-only pass")
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

    # 3. Thresholds (cs defaults: prov 80 / delete 90 / ceiling 90).
    cpu_prov_thr = _pct_setting(usb_cfg, "cpu_provision_threshold", 80)
    cpu_del_thr = _pct_setting(usb_cfg, "cpu_delete_threshold", 90)
    cpu_prov_ceil = _pct_setting(usb_cfg, "cpu_provision_ceiling", 90)
    mem_prov_thr = _pct_setting(usb_cfg, "mem_provision_threshold", 80)
    mem_del_thr = _pct_setting(usb_cfg, "mem_delete_threshold", 90)

    # provision_halt: over the provision threshold → halt (cs agent.sh 2648-2666).
    cpu_over_prov = cpu_avg is not None and cpu_avg >= cpu_prov_thr
    mem_over_prov = mem_avg is not None and mem_avg >= mem_prov_thr
    if cpu_over_prov or mem_over_prov:
        _provision_halt = {"halted": True,
                            "reason": "cpu" if cpu_over_prov else "mem"}
    else:
        _provision_halt = None

    # 4. Delete gate — shed the newest USB VM when over the delete threshold,
    #    unless a delete is already in the cooldown window (cs 10055-10110).
    cooldown_until = _load_delete_gate_cooldown()
    delete_queued = False
    threshold_exceeded = (
        (cpu_avg is not None and cpu_avg >= cpu_del_thr)
        or (mem_avg is not None and mem_avg >= mem_del_thr)
    )
    if threshold_exceeded and now >= cooldown_until:
        provisioning = _provisioning_vmids()
        candidates = [int(v) for v in state["bus_to_vmid"].values()
                     if str(v).lstrip("-").isdigit() and int(v) not in provisioning]
        candidates = sorted(set(candidates))
        if candidates:
            target_vmid = max(candidates)  # newest = highest VMID
            bus = state["bus_to_vmid"].get(str(target_vmid))
            from . import cs_sim  # deferred — cs_sim imports usb_provision
            try:
                await cs_sim.destroy_vm(agent, target_vmid, bus=bus)
                torn_down.append(target_vmid)
                state["bus_to_vmid"].pop(bus, None)
                state["vmid_to_bus"].pop(str(target_vmid), None)
                state["vmid_to_image"].pop(str(target_vmid), None)
                state["missing_since"].pop(bus, None)
                delete_queued = True
                cooldown_until = now + DELETE_GATE_COOLDOWN_S
                _save_delete_gate_cooldown(cooldown_until)
                logger.warning(
                    "auto-provision delete gate: destroyed newest USB VM %s "
                    "(cpu_avg=%.1f mem_avg=%.1f) — 300s cooldown",
                    target_vmid, cpu_avg or 0.0, mem_avg or 0.0)
            except Exception as e:  # noqa: BLE001
                logger.warning("auto-provision delete gate: destroy %s failed: %s",
                               target_vmid, e)

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

    # 5. Missing-dongle teardown (only when the toggle is on).
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
                    # Bump the bus's quarantine failure counter on teardown (bash
                    # 2615) so a flapping physical port accumulates toward
                    # QUARANTINE_MAX_FAILS and stops being re-provisioned.
                    record_usb_failure(bus)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"provision loop: teardown of {vmid} failed: {e}")
                state["bus_to_vmid"].pop(bus, None)
                state["vmid_to_bus"].pop(vmid, None)
                state["missing_since"].pop(bus, None)

    # 6. VMID-gap audit (every VMID_AUDIT_INTERVAL_S; bypasses delete cooldown).
    #    May delete a VM and mutate state — persist before the early returns
    #    below so the audit's bookkeeping isn't lost on a "no templates"/"not
    #    ordered"/"resource gate" exit (the next pass's reconcile would
    #    otherwise self-heal it, but saving keeps the state honest immediately).
    await _vmid_gap_audit(agent, state, start, end, now)
    save_usb_state(state)

    if not img1 and not img2:
        # Silent gate made loud — the #2 cause of "nothing provisions"; previously
        # returned with no log line. Surface it in the log + telemetry.
        _provision_reason = "no template ids configured"
        logger.warning("auto-provision: no image1/image2 template_id configured — "
                        "set clone-source templates in the Simulations UI so VMs can be cloned")
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
            ceil_hit, _provision_halt)
        return {"provisioned": 0, "torn_down": len(torn_down),
                "reason": _provision_reason}
    if _prov_run.get("running"):
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
    for bus, info in present.items():
        if state["bus_to_vmid"].get(bus):
            continue
        if state["excluded_buses"].get(bus):
            continue
        if bus in quarantine:
            continue
        dtype = info["type"]
        if _sim_phy_accepts(sim_phy, dtype):
            preferred.append(bus)
        elif use_all and sim_phy in ("wireless", "ethernet"):
            overflow.append(bus)

    ordered = preferred + overflow
    if not ordered:
        # Silent gate made loud — every dongle is assigned/excluded/quarantined or
        # none is present. Previously returned with no log line.
        _provision_reason = "no eligible dongles"
        logger.info("auto-provision: no eligible dongles "
                    "(all assigned/excluded/quarantined or none present)")
        return {"provisioned": 0, "torn_down": len(torn_down)}

    existing_after = set(await pve_cmds.list_all_vmids())
    img1_count = sum(1 for v in state["vmid_to_image"].values() if v == 1)
    protected = _protected_vmids(agent)

    sem = asyncio.Semaphore(concurrency)
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
    _prov_run = {"running": True, "items": items,
                 "started_at": int(now), "total": len(ordered),
                 "completed": 0, "failed": 0}
    item_by_bus = {b: items[i] for i, b in enumerate(ordered)}

    async def _do(bus: str) -> bool:
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
            for _t in (img1, img2):
                try:
                    _tv = int(_t)
                    if _tv > 0:
                        templates.add(_tv)
                except (TypeError, ValueError):
                    pass
            # Find a free vmid in the sim range. A vid that's in Proxmox
            # (existing_after) but NOT in our own vmid_to_bus tracking is a
            # zombie — a leftover from a prior failed clone/destroy that
            # never fully cleaned up (bash clone_vm_for_usb's pre-clone
            # zombie cleanup, proxmox-agent.sh:1900-1949). Reclaim it via a
            # force stop+destroy instead of permanently losing that VMID
            # from the pool; give up on this vid (not the whole dongle) if
            # reclamation fails and move to the next candidate.
            vid = start
            while vid <= end:
                if str(vid) in state["vmid_to_bus"] or vid in templates:
                    vid += 1
                    continue
                if vid in existing_after:
                    if vid in protected or not await _reclaim_zombie_vmid(agent, vid):
                        vid += 1
                        continue
                    existing_after.discard(vid)
                break
            if vid > end:
                logger.info("provision loop: no free VM slot — stopping")
                item_by_bus[bus]["status"] = "failed"
                return False
            item_by_bus[bus]["vmid"] = vid
            # Image 1 vs 2 by IMAGE1_PCT ceiling (bash 2729-2735).
            total = len(state["vmid_to_image"]) + 1
            target_img1 = (img1_pct * total + 99) // 100
            image_num = 2 if img1_count >= target_img1 and img2 else 1
            template = img1 if image_num == 1 else (img2 or img1)
            if not template:
                item_by_bus[bus]["status"] = "failed"
                return False
            try:
                await _clone_and_provision(agent, vid, bus, info, int(template),
                                            image_num, state)
                state["vmid_to_bus"][str(vid)] = bus
                state["bus_to_vmid"][bus] = str(vid)
                state["vmid_to_image"][str(vid)] = image_num
                existing_after.add(vid)
                item_by_bus[bus]["status"] = "done"
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning(f"provision loop: clone {vid} on {bus} failed: {e}")
                # Tear down the half-cloned VM so it doesn't linger as a zombie
                # (bash _teardown 1880-1906 stops+destroys+clears state on failure).
                from . import cs_sim
                try:
                    await cs_sim.destroy_vm(agent, vid, bus=bus)
                except Exception as te:  # noqa: BLE001
                    logger.warning(f"provision loop: teardown of partial {vid}: {te}")
                record_usb_failure(bus)
                item_by_bus[bus]["status"] = "failed"
                return False

    results = await asyncio.gather(*[_do(b) for b in ordered], return_exceptions=True)
    save_usb_state(state)
    provisioned = sum(1 for r in results if r is True)
    _prov_run["running"] = False
    _prov_run["completed"] = provisioned
    _prov_run["failed"] = len(ordered) - provisioned
    _prov_run["completed_at"] = int(time.time())
    _provision_reason = f"provisioning: attempted {len(ordered)}, provisioned {provisioned}"
    return {"provisioned": provisioned, "torn_down": len(torn_down),
            "attempted": len(ordered)}


async def _vmid_gap_audit(agent, state: Dict[str, Any],
                          start: int, end: int, now: float) -> None:
    """Detect VMID gaps in the assigned sim range and delete the highest VMID
    above the lowest gap so the next provision pass refills the hole (cs
    10256-10324). Runs at most once per ``VMID_AUDIT_INTERVAL_S``; bypasses the
    delete-gate cooldown (corrective bookkeeping, not load-shedding)."""
    if now - _load_vmid_gap_last_run() < VMID_AUDIT_INTERVAL_S:
        return
    _save_vmid_gap_last_run(now)
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
        if chk not in active_set:
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
    from . import cs_sim
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
    cfg = await pve_cmds.qm_config(vmid)
    return cfg.get("template") == "1"


async def _resolve_template_vmid(configured: Any) -> Optional[int]:
    """Validate a configured template vmid before trusting it as a clone
    source; fall back to the lowest-numbered valid template on the cluster
    if it no longer checks out (bash resolve_template_vmid, proxmox-agent.sh:
    933-944). Returns None, unchanged, when nothing is configured — callers'
    existing "no template configured" gates still apply."""
    try:
        cvid = int(configured) if configured else None
    except (TypeError, ValueError):
        cvid = None
    if cvid is None:
        return None
    if await _is_runnable_template(cvid):
        return cvid
    logger.warning(f"provision loop: configured template vmid {cvid} is not a "
                   "runnable template (deleted or no longer a template) — "
                   "searching the cluster for a fallback")
    for vid in sorted(await pve_cmds.list_qemu_vmids()):
        if vid != cvid and await _is_runnable_template(vid):
            logger.warning(f"provision loop: falling back to template vmid {vid}")
            return vid
    logger.error(f"provision loop: template vmid {cvid} is invalid and no "
                "fallback template exists on the cluster — clones using it will fail")
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