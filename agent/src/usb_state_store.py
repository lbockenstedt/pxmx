"""USB provision state store: bus-map + orphan-registry persistence.

Owns the two JSON files that back the host-side USB-dongle → sim-VM bookkeeping
(extracted from ``usb_provision`` so the provisioning "brain" is separable from
its persistence layer):

  * ``usb_state.json``  — the vmid↔bus bijection + per-bus tracking maps
  * ``orphan_vms.json`` — VMs a destroy could not release (declared orphans)

Ports the host-side state machine from ``cs/proxmox/proxmox-agent.sh``
(``_usb_provision_loop_impl``, ``clone_vm_for_usb``). The bash agent kept its
state in associative arrays + a flock-guarded state file; here it is one JSON
document under ``/var/lib/pxmx/usb_state.json``:

  vmid_to_bus   {str(vmid): bus_path}     which sim VM holds which dongle
  bus_to_vmid   {bus_path: str(vmid)}     reverse map
  vmid_to_image {str(vmid): 1|2}          which template image it was cloned from
  excluded_buses {bus_path: ts}           hub-deleted → skip provisioning
  quarantined   {bus_path: {fails, since}} too many provision failures → skip
  missing_since {bus_path: ts}            when a bound dongle disappeared

Single asyncio event loop → no lock needed (the only writers are the provision
loop and the delete/reclone long-op tasks, both on the same loop). These
functions are re-exported from ``usb_provision`` so existing ``usb_provision.X``
callers (and its own internal unqualified calls) are unchanged.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("PxmxAgent")

# Mirrors usb_provision.PXMLIB (this module owns its JSON files); watchdogs.py
# independently defines the same base path — a fixed deployment location.
PXMLIB = "/var/lib/pxmx"
ORPHAN_VMS_FILE = f"{PXMLIB}/orphan_vms.json"
USB_STATE_FILE = f"{PXMLIB}/usb_state.json"

# Post-clone settle reboot: seconds after a clone completes (set_assignment)
# before the provision-loop sweep reboots the VM, so a freshly-cloned box gets
# a clean restart after its settle/update window. Env-overridable.
_POST_PROV_REBOOT_DELAY_S = int(os.environ.get("POST_PROV_REBOOT_DELAY_S", "900"))


# ── orphan registry ───────────────────────────────────────────────────────
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


# ── USB provision state (usb_state.json) ──────────────────────────────────
def _new_usb_state() -> Dict[str, Any]:
    return {"vmid_to_bus": {}, "bus_to_vmid": {}, "vmid_to_image": {},
            "excluded_buses": {}, "quarantined": {}, "missing_since": {},
            "vidpid_by_bus": {}, "post_prov_retry": {}, "post_prov_reboot": {}}


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
    st.get("post_prov_reboot", {}).pop(str(int(vmid)), None)
    save_usb_state(st)


def prune_ghost_vms(existing: Set[int]) -> List[int]:
    """Drop every tracked VM no longer present on the host from ALL state maps,
    iterating ``bus_to_vmid`` BY VALUE (the vmid) so an entry stranded there
    after a partial clear — ``vmid_to_bus`` already removed but ``bus_to_vmid``
    retained — is still pruned.

    The main-loop reconcile iterated ``vmid_to_bus`` only, but the delete gate
    selects candidates from ``bus_to_vmid``; a ghost stranded in ``bus_to_vmid``
    was therefore re-selected every pass and never destroyed (it no longer
    exists), fixating the gate on a VMID shed hours earlier and never advancing
    to the real next candidate. Called at the top of the delete gate (refresh
    the tracked list each pass) and from the main-loop reconcile. Returns the
    pruned VMIDs. ``existing`` = ``set(await pve_cmds.list_all_vmids())``."""
    from . import usb_provision  # deferred — clear_destroy_fails lives in the quarantine leaf (re-exported by core)

    def _gone(v: Any) -> bool:
        return str(v).lstrip("-").isdigit() and int(v) not in existing
    st = load_usb_state()
    pruned: Set[int] = set()
    for bus, v in list(st.get("bus_to_vmid", {}).items()):
        if _gone(v):
            st["bus_to_vmid"].pop(bus, None)
            st.get("missing_since", {}).pop(bus, None)
            st.get("vidpid_by_bus", {}).pop(bus, None)
            pruned.add(int(v))
    for vmid, bus in list(st.get("vmid_to_bus", {}).items()):
        if _gone(vmid):
            st["vmid_to_bus"].pop(vmid, None)
            st.get("missing_since", {}).pop(bus, None)
            st.get("vidpid_by_bus", {}).pop(bus, None)
            pruned.add(int(vmid))
    for vmid in list(st.get("vmid_to_image", {})):
        if _gone(vmid):
            st["vmid_to_image"].pop(vmid, None)
            pruned.add(int(vmid))
    for vmid in list(st.get("post_prov_reboot", {})):
        if _gone(vmid):
            st["post_prov_reboot"].pop(vmid, None)
            pruned.add(int(vmid))
    if pruned:
        save_usb_state(st)
        # destroy-fail counters + orphan registry live in separate files.
        for v in pruned:
            usb_provision.clear_destroy_fails(v)
            remove_orphan_vm(v)
    return sorted(pruned)


def set_assignment(vmid: int, bus: str, image_num: int) -> None:
    st = load_usb_state()
    v = str(int(vmid))
    # Clear this VM's PRIOR bus from the reverse map before re-pointing it, else a
    # re-provision onto a new bus/dongle leaves a stale bus_to_vmid entry — which
    # made the VM show under TWO vid:pids in the certified-USB "Active VMs" column
    # (it reads bus_to_vmid). vmid_to_bus is single-bus-per-VM (authoritative), so
    # the old reverse entry is pure cruft. Also detach any OTHER vmid that somehow
    # held THIS bus so the maps stay a clean bijection.
    old_bus = st["vmid_to_bus"].get(v)
    if old_bus and old_bus != bus:
        st["bus_to_vmid"].pop(old_bus, None)
        st.get("missing_since", {}).pop(old_bus, None)
        st.get("vidpid_by_bus", {}).pop(old_bus, None)
    prev_vmid = st["bus_to_vmid"].get(bus)
    if prev_vmid is not None and str(prev_vmid) != v:
        st["vmid_to_bus"].pop(str(prev_vmid), None)
        st.get("vmid_to_image", {}).pop(str(prev_vmid), None)
    st["vmid_to_bus"][v] = bus
    st["bus_to_vmid"][bus] = v
    st["vmid_to_image"][v] = int(image_num)
    st["missing_since"].pop(bus, None)
    # Post-clone SETTLE reboot: stamp a deferred reboot 15 min (default) after
    # this clone completes. The provision-loop sweep (_run_post_prov_reboot_queue
    # in usb_provision) fires it when due, then pops the entry. Re-stamping on a
    # reclone overwrites the prior entry — a fresh clone resets the window.
    #
    # Why a SECOND reboot after the clone (both intentional — see the comment
    # at the immediate post-clone reboot in _clone_and_provision): the first
    # reboot only applies hostname + sim_phy + first-boot bits and runs
    # update.sh; the guest doesn't stay up long enough to have pulled a
    # placement/config push from the engine yet. This +15-min reboot is the
    # one that restarts the box AFTER it has settled, pulled engine config,
    # and run update.sh — so it comes back fully configured. (Reclone does no
    # first reboot, so this is its only post-clone restart — still correct:
    # the settle+config window is what matters, not which reboot is "first".)
    cloned_at = time.time()
    st.setdefault("post_prov_reboot", {})[v] = {
        "cloned_at": cloned_at,
        "reboot_at": cloned_at + _POST_PROV_REBOOT_DELAY_S,
        "bus": bus,
        "image_num": int(image_num),
    }
    save_usb_state(st)


def reconcile_bus_map() -> List[int]:
    """Make bus_to_vmid a clean 1:1 with vmid_to_bus WITHOUT orphaning VMs.

    A VM re-provisioned onto a new bus can leave its OLD reverse entry behind, so
    a vmid ends up mapped to TWO buses in bus_to_vmid (the "shown under two
    vid:pids" bug). Only THOSE true duplicates are pruned — keep the bus
    vmid_to_bus points at (else the newest), drop the rest.

    A vmid with a SINGLE bus_to_vmid entry is always kept; if its vmid_to_bus is
    missing/stale it is REPAIRED (set to that bus), never removed. The prior
    version dropped single entries whose vmid_to_bus was absent, which orphaned
    legitimately-tracked VMs out of bus_to_vmid so the missing-dongle teardown
    (which iterates bus_to_vmid) could no longer shed them. Returns the vmids
    whose duplicate reverse entries were pruned."""
    st = load_usb_state()
    b2v = st.get("bus_to_vmid") or {}
    v2b = st.setdefault("vmid_to_bus", {})
    buses_by_vmid: Dict[str, List[str]] = {}
    for bus, vmid in b2v.items():
        buses_by_vmid.setdefault(str(vmid), []).append(bus)
    pruned: List[int] = []
    changed = False
    for vmid, buses in buses_by_vmid.items():
        if len(buses) == 1:
            if v2b.get(vmid) != buses[0]:          # repair, don't orphan
                v2b[vmid] = buses[0]
                changed = True
            continue
        keep = v2b.get(vmid) if v2b.get(vmid) in buses else buses[-1]
        for b in buses:
            if b == keep:
                continue
            b2v.pop(b, None)
            st.get("missing_since", {}).pop(b, None)
            st.get("vidpid_by_bus", {}).pop(b, None)
            changed = True
            if str(vmid).lstrip("-").isdigit():
                pruned.append(int(vmid))
        if v2b.get(vmid) != keep:
            v2b[vmid] = keep
            changed = True
    if changed:
        save_usb_state(st)
    return sorted(set(pruned))


def bus_for_vmid(vmid: int) -> Optional[str]:
    return load_usb_state()["vmid_to_bus"].get(str(int(vmid)))
