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
    cs-spoke ``usb_config.usb_vidpids`` array of ``{vidpid,...}`` dicts, with a
    legacy ``dongle_vidpids``/``certified_types`` fallback. Empty until the hub
    delivers usb_config — the blacklist + telemetry classify as no-op/unknown."""
    cfg = _usb_cfg(agent)
    items = _parse_vidpid_items(cfg.get("usb_vidpids"))
    if not items:  # legacy fallback
        items = _parse_vidpid_items(cfg.get("dongle_vidpids")) or \
                list(cfg.get("certified_types") or [])
    out: Set[str] = set()
    for v in items:
        vp = (v.get("vidpid") if isinstance(v, dict) else v)
        s = str(vp or "").strip().lower()
        if _VIDPID_RE.match(s):
            out.add(s)
    return out


def _certified_types(agent) -> Dict[str, str]:
    """``{vidpid: type}`` from the certified list (default ``wireless``)."""
    cfg = _usb_cfg(agent)
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
    """The ignored dongle VID:PID set (lowercased) from ``usb_ignored_vidpids``."""
    cfg = _usb_cfg(agent)
    out: Set[str] = set()
    for v in _parse_vidpid_items(cfg.get("usb_ignored_vidpids")):
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


def sample_resources(agent) -> None:
    """Append a CPU + memory sample (agent host == proxmox host) to the rolling
    1h deques. Called once per ``_usb_provision_loop`` tick. Best-effort — a
    failure leaves the deques untouched (averages degrade to None/cold-start)."""
    try:
        import psutil  # local: not every host has psutil at import time
        now = time.time()
        cutoff = now - _RESOURCE_SAMPLE_WINDOW
        _cpu_samples.append((now, float(psutil.cpu_percent(interval=None))))
        _cpu_samples[:] = [(ts, v) for ts, v in _cpu_samples if ts >= cutoff]
        _mem_samples.append((now, float(psutil.virtual_memory().percent)))
        _mem_samples[:] = [(ts, v) for ts, v in _mem_samples if ts >= cutoff]
    except Exception as exc:  # noqa: BLE001
        logger.debug("sample_resources failed: %s", exc)


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
            "excluded_buses": {}, "quarantined": {}, "missing_since": {}}


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
    return sim_phy == "any" or sim_phy == device_type


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
    global _provision_halt, _prov_run
    from . import pve_cmds  # local to avoid a top-level import cycle
    cs_cfg = agent.config.get("client_simulation") or {}
    usb_cfg = cs_cfg.get("usb_config") or {}
    dongle_vidpids = _dongle_vidpids(agent)
    if not dongle_vidpids:
        return {"provisioned": 0, "torn_down": 0, "reason": "no dongle_vidpids configured"}

    ap_on = _toggle_on(usb_cfg)
    certified_types = usb_cfg.get("certified_types") or {}
    if not isinstance(certified_types, dict):
        certified_types = {}
    sim_phy = str(usb_cfg.get("sim_phy") or "any").lower()
    use_all = bool(usb_cfg.get("use_all_dongles", False))
    img1 = usb_cfg.get("image1_template_id")
    img2 = usb_cfg.get("image2_template_id")
    img1_pct = int(usb_cfg.get("image1_pct", 50) or 50)
    vr = cs_cfg.get("vmid_range") or {}
    start = int((vr or {}).get("start", 90000) or 90000)
    end = int((vr or {}).get("end", 99999) or 99999)
    # missing_timeout: accept the union of relay key names (webui-spoke sends
    # usb_missing_timeout, lm-spoke sends missing_timeout) — the old single-key
    # read (usb_missing_timeout_seconds, which nothing sends) left the teardown
    # block dead under both relay paths.
    missing_timeout = int(_cfg_first(usb_cfg,
                                     ("usb_missing_timeout_seconds", "usb_missing_timeout",
                                      "missing_timeout"), 0) or 0)
    concurrency = max(1, int(usb_cfg.get("reclone_concurrency", 2) or 2))
    max_slots = int(_cfg_first(usb_cfg, ("usb_max_slots", "max_slots"), 24) or 24)

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
    # Clear exclusions/quarantine for buses no longer present.
    for bus in list(state["excluded_buses"]):
        if bus not in present:
            state["excluded_buses"].pop(bus, None)
    quarantine = _read_quarantine()
    for bus in list(quarantine):
        if bus not in present:
            quarantine.pop(bus, None)
    _save_quarantine(quarantine)

    torn_down: List[int] = []

    # 2. Toggle gate — off = telemetry-only (no VM mutations).
    if not ap_on:
        save_usb_state(state)
        _provision_halt = None
        logger.info("auto-provision: usb_auto_provision=off — telemetry-only pass")
        return {"provisioned": 0, "torn_down": 0, "reason": "auto-provision disabled"}

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
        return {"provisioned": 0, "torn_down": len(torn_down),
                "reason": "no template ids configured"}

    # 7. resource_ok gate + prov_run-already-active + slot cap before cloning.
    in_delete_cooldown = now < cooldown_until
    ceil_hit = cpu_instant is not None and cpu_instant >= cpu_prov_ceil
    resource_ok = (
        not delete_queued
        and not in_delete_cooldown
        and not ceil_hit
        and cpu_avg is not None and cpu_avg < cpu_prov_thr
        and mem_avg is not None and mem_avg < mem_prov_thr
    )
    if not resource_ok:
        logger.info(
            "auto-provision gate: suppressing clone (cpu_avg=%s mem_avg=%s "
            "cpu_instant=%s delete_queued=%s cooldown=%s ceil=%s halt=%s)",
            cpu_avg, mem_avg, cpu_instant, delete_queued, in_delete_cooldown,
            ceil_hit, _provision_halt)
        return {"provisioned": 0, "torn_down": len(torn_down),
                "reason": "resource gate"}
    if _prov_run.get("running"):
        logger.info("auto-provision gate: prov_run already active — skipping trigger")
        return {"provisioned": 0, "torn_down": len(torn_down),
                "reason": "prov_run active"}
    active_usb_vms = len(state["vmid_to_bus"])
    if active_usb_vms >= max_slots:
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
        return {"provisioned": 0, "torn_down": len(torn_down)}

    existing_after = set(await pve_cmds.list_all_vmids())
    img1_count = sum(1 for v in state["vmid_to_image"].values() if v == 1)

    sem = asyncio.Semaphore(concurrency)

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
            # Find a free vmid in the sim range.
            vid = start
            while vid <= end and (str(vid) in state["vmid_to_bus"]
                                  or vid in existing_after):
                vid += 1
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
                                            image_num)
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
                                image_num: int) -> None:
    """Clone a template → vmid, attach the USB dongle, start, wait for the guest
    agent, set the hostname (bash ``clone_vm_for_usb`` 1943-2098, slimmed)."""
    from . import pve_cmds
    protected = _protected_vmids(agent)
    name = f"sim-{vmid}-{info.get('type', 'wireless')}"
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
    pve_cmds.qm_guest_exec_shell(
        vmid, f"echo 'sim_phy={dtype}' > /usr/local/scripts/usb-phy-override.conf",
        exec_timeout=60, outer_timeout=90, protected=protected)
    set_assignment(vmid, bus, image_num)
    remove_orphan_vm(vmid)


def _protected_vmids(agent) -> Set[int]:
    from .cs_guard import resolve_protected_vmids
    return resolve_protected_vmids(agent.config.get("client_simulation"))