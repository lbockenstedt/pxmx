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

# json reads and writes the state and orphan files.
import json
# logging reports write failures and orphan declarations.
import logging
# os handles file existence checks, sizes, directory creation and env overrides.
import os
# time stamps orphan entries and post-clone reboot schedules.
import time
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, List, Optional, Set

# Share the main agent logger so state messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# Mirrors usb_provision.PXMLIB (this module owns its JSON files); watchdogs.py
# independently defines the same base path — a fixed deployment location.
PXMLIB = "/var/lib/pxmx"
# Files this module owns inside that directory.
ORPHAN_VMS_FILE = f"{PXMLIB}/orphan_vms.json"
USB_STATE_FILE = f"{PXMLIB}/usb_state.json"

# Post-clone settle reboot: seconds after a clone completes (set_assignment)
# before the provision-loop sweep reboots the VM, so a freshly-cloned box gets
# a clean restart after its settle/update window. Env-overridable.
_POST_PROV_REBOOT_DELAY_S = int(os.environ.get("POST_PROV_REBOOT_DELAY_S", "900"))


# ── orphan registry ───────────────────────────────────────────────────────
def _read_orphans() -> List[Dict[str, Any]]:
    """Load the orphan registry (a list of {vmid, bus, ts}), returning [] if unavailable."""
    # Read only a non-empty file; anything other than a list is ignored.
    try:
        if os.path.exists(ORPHAN_VMS_FILE) and os.path.getsize(ORPHAN_VMS_FILE) > 0:
            with open(ORPHAN_VMS_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    # Missing or corrupt file means no orphans recorded.
    except (OSError, json.JSONDecodeError):
        pass
    # Fallback: empty registry.
    return []


def _write_orphans(entries: List[Dict[str, Any]]) -> None:
    """Write the orphan registry to disk, logging (not raising) on failure."""
    # Create the directory if needed, then overwrite the file.
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(ORPHAN_VMS_FILE, "w") as f:
            json.dump(entries, f)
    # Disk/permission problem: warn and carry on.
    except OSError as e:
        logger.warning(f"orphan registry: cannot write {ORPHAN_VMS_FILE}: {e}")


def add_orphan_vm(vmid: int, bus: str) -> Dict[str, Any]:
    """Declare a VM an orphan: dedup by vmid and append to the registry
    (bash 1335-1346). Caller (Phase E destroy path) force-releases the bus."""
    # Load the registry and drop any previous entry for this VM (so it appears once).
    entries = _read_orphans()
    entries = [e for e in entries if int(e.get("vmid", -1)) != int(vmid)]
    # Append the new orphan record with a timestamp and save.
    entries.append({"vmid": int(vmid), "bus": bus, "ts": int(time.time())})
    _write_orphans(entries)
    # Orphans are an error condition worth an operator's attention.
    logger.error(f"VM {vmid} declared orphan (bus {bus}) — released for re-provisioning")
    # Tell the caller the VM was orphaned.
    return {"vmid": int(vmid), "bus": bus, "orphaned": True}


def remove_orphan_vm(vmid: int) -> None:
    """Drop a vmid from the orphan registry (bash 1358-1371) — called when a VM
    is later successfully re-provisioned or destroyed."""
    # Keep every entry except this VM's, then save.
    entries = [e for e in _read_orphans() if int(e.get("vmid", -1)) != int(vmid)]
    _write_orphans(entries)


def get_orphan_vms() -> List[Dict[str, Any]]:
    """Snapshot of the orphan registry (surfaced in CS telemetry, Phase E)."""
    # Return the current registry as stored on disk.
    return _read_orphans()


# ── USB provision state (usb_state.json) ──────────────────────────────────
def _new_usb_state() -> Dict[str, Any]:
    """Return a fresh, empty USB state document with every expected map present."""
    return {"vmid_to_bus": {}, "bus_to_vmid": {}, "vmid_to_image": {},
            "excluded_buses": {}, "quarantined": {}, "missing_since": {},
            "vidpid_by_bus": {}, "post_prov_retry": {}, "post_prov_reboot": {},
            # Per-bus in-guest health classification from _guest_health_probe
            # (healthy/no_driver/no_assoc/no_gateway/not_visible) — surfaced in
            # CS telemetry so the Hub UI can flag "USB present but no driver", etc.
            "guest_health": {},
            # Per-VMID hostname-audit strikes {attempts,last,expected,stamped} —
            # throttles the re-stamp+reboot of a clone that kept its template
            # hostname (hostname_audit_and_restamp).
            "hostname_fix": {}}


def load_usb_state() -> Dict[str, Any]:
    """Load usb_state.json, filling in any missing maps; returns a fresh state on any error."""
    # Read only a non-empty file.
    try:
        if os.path.exists(USB_STATE_FILE) and os.path.getsize(USB_STATE_FILE) > 0:
            with open(USB_STATE_FILE) as f:
                data = json.load(f)
            # Start from a complete empty state and overlay the saved maps (non-dict values become
            # {}).
            if isinstance(data, dict):
                st = _new_usb_state()
                st.update({k: (v if isinstance(v, dict) else {}) for k, v in data.items()})
                return st
    # Missing or corrupt file: fall through to a fresh state.
    except (OSError, json.JSONDecodeError):
        pass
    # Fallback: empty state.
    return _new_usb_state()


def save_usb_state(state: Dict[str, Any]) -> None:
    """Write the USB state document to disk, logging (not raising) on failure."""
    # Create the directory if needed, then overwrite the file.
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        with open(USB_STATE_FILE, "w") as f:
            json.dump(state, f)
    # Disk/permission problem: warn and carry on.
    except OSError as e:
        logger.warning(f"usb_state: cannot write {USB_STATE_FILE}: {e}")


def clear_assignment(vmid: int, bus: Optional[str] = None) -> None:
    """Drop a vmid↔bus assignment from the USB state (bash 2166-2172)."""
    # Load the state and remove the VM's bus mapping (falling back to the caller's bus hint).
    st = load_usb_state()
    b = st["vmid_to_bus"].pop(str(int(vmid)), None) or bus
    # Clear every per-bus record for that bus.
    if b:
        st["bus_to_vmid"].pop(b, None)
        st["missing_since"].pop(b, None)
        st.get("guest_health", {}).pop(b, None)
    # Clear the per-VM records (template image, pending reboot, hostname-fix strikes).
    st["vmid_to_image"].pop(str(int(vmid)), None)
    st.get("post_prov_reboot", {}).pop(str(int(vmid)), None)
    st.get("hostname_fix", {}).pop(str(int(vmid)), None)
    # Persist the cleaned state.
    save_usb_state(st)


def clear_recovery_state(bus: Optional[str]) -> None:
    """Wipe a bus's RECOVERY/strike bookkeeping (``detach_reclones`` reclone
    strikes + ``dongle_health`` guest-blind ladder counters). Called only by an
    explicit admin WebUI delete so a manual delete gives the dongle a CLEAN slate
    — it must never nudge the bus toward quarantine. Deliberately NOT part of
    ``clear_assignment`` (reclone-on-detach relies on the strike count surviving
    the VM being destroyed each reclone; only an operator delete resets it)."""
    # No bus given: nothing to clear.
    if not bus:
        return
    # Load the state and track whether anything was removed.
    st = load_usb_state()
    changed = False
    # Drop this bus from each recovery/health map that has it.
    for m in ("detach_reclones", "dongle_health", "guest_health"):
        if isinstance(st.get(m), dict) and st[m].pop(bus, None) is not None:
            changed = True
    # Only write when something changed.
    if changed:
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
    # Imported lazily to avoid a circular import with usb_provision.
    from . import usb_provision  # deferred — clear_destroy_fails lives in the quarantine leaf (re-exported by core)

    def _gone(v: Any) -> bool:
        """True when a tracked VMID is numeric and no longer exists on the host."""
        # Non-numeric keys are left alone; numeric ones are "gone" if absent from the host's VM
        # list.
        return str(v).lstrip("-").isdigit() and int(v) not in existing
    # Load the state and collect every VMID we prune.
    st = load_usb_state()
    pruned: Set[int] = set()
    # Reverse map: drop buses whose VM no longer exists, plus their per-bus records.
    for bus, v in list(st.get("bus_to_vmid", {}).items()):
        if _gone(v):
            st["bus_to_vmid"].pop(bus, None)
            st.get("missing_since", {}).pop(bus, None)
            st.get("vidpid_by_bus", {}).pop(bus, None)
            pruned.add(int(v))
    # Forward map: drop VMs that no longer exist, plus their bus's per-bus records.
    for vmid, bus in list(st.get("vmid_to_bus", {}).items()):
        if _gone(vmid):
            st["vmid_to_bus"].pop(vmid, None)
            st.get("missing_since", {}).pop(bus, None)
            st.get("vidpid_by_bus", {}).pop(bus, None)
            pruned.add(int(vmid))
    # Per-VM template-image records for vanished VMs.
    for vmid in list(st.get("vmid_to_image", {})):
        if _gone(vmid):
            st["vmid_to_image"].pop(vmid, None)
            pruned.add(int(vmid))
    # Pending post-clone reboots for vanished VMs.
    for vmid in list(st.get("post_prov_reboot", {})):
        if _gone(vmid):
            st["post_prov_reboot"].pop(vmid, None)
            pruned.add(int(vmid))
    # Persist only if anything was pruned.
    if pruned:
        save_usb_state(st)
        # destroy-fail counters + orphan registry live in separate files.
        for v in pruned:
            usb_provision.clear_destroy_fails(v)
            remove_orphan_vm(v)
    # Return the pruned VMIDs in a stable order.
    return sorted(pruned)


def set_assignment(vmid: int, bus: str, image_num: int) -> None:
    """Record that ``vmid`` now owns ``bus`` (cloned from template ``image_num``), keeping maps 1:1.
    """
    # Load the state; VMIDs are stored as string keys in JSON.
    st = load_usb_state()
    v = str(int(vmid))
    # Clear this VM's PRIOR bus from the reverse map before re-pointing it, else a
    # re-provision onto a new bus/dongle leaves a stale bus_to_vmid entry — which
    # made the VM show under TWO vid:pids in the certified-USB "Active VMs" column
    # (it reads bus_to_vmid). vmid_to_bus is single-bus-per-VM (authoritative), so
    # the old reverse entry is pure cruft. Also detach any OTHER vmid that somehow
    # held THIS bus so the maps stay a clean bijection.
    old_bus = st["vmid_to_bus"].get(v)
    # The VM moved to a different bus: forget its old bus everywhere.
    if old_bus and old_bus != bus:
        st["bus_to_vmid"].pop(old_bus, None)
        st.get("missing_since", {}).pop(old_bus, None)
        st.get("vidpid_by_bus", {}).pop(old_bus, None)
    # Another VM claimed this bus before: detach that VM from it.
    prev_vmid = st["bus_to_vmid"].get(bus)
    if prev_vmid is not None and str(prev_vmid) != v:
        st["vmid_to_bus"].pop(str(prev_vmid), None)
        st.get("vmid_to_image", {}).pop(str(prev_vmid), None)
    # Write the new assignment in both directions plus the template image used.
    st["vmid_to_bus"][v] = bus
    st["bus_to_vmid"][bus] = v
    st["vmid_to_image"][v] = int(image_num)
    # The dongle is present again, so it is no longer "missing".
    st["missing_since"].pop(bus, None)
    # Post-clone SETTLE reboot: stamp a deferred reboot 15 min (default) after
    # this clone completes. The provision-loop sweep (_run_post_prov_reboot_queue
    # in usb_provision) fires it when due, then pops the entry. Re-stamping on a
    # reclone overwrites the prior entry — a fresh clone resets the window.
    #
    # Why a SECOND reboot: see usb_provision.finish_clone (every clone path,
    # USB/PCI/reclone, runs it and re-stamps this entry there too).
    stamp_post_prov_reboot(st, vmid, bus, image_num)
    # Persist the updated state.
    save_usb_state(st)


def stamp_post_prov_reboot(st: Dict[str, Any], vmid: int, bus: Optional[str],
                           image_num: int) -> None:
    """Queue the +15-min post-clone settle reboot for ``vmid`` in ``st``.
    ``bus`` is None for a T1/T3 (PCI) VM."""
    # Schedule the reboot for the configured delay after now, keeping the clone details.
    cloned_at = time.time()
    st.setdefault("post_prov_reboot", {})[str(int(vmid))] = {
        "cloned_at": cloned_at,
        "reboot_at": cloned_at + _POST_PROV_REBOOT_DELAY_S,
        "bus": bus,
        "image_num": int(image_num),
    }


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
    # Load the state and both direction maps.
    st = load_usb_state()
    b2v = st.get("bus_to_vmid") or {}
    v2b = st.setdefault("vmid_to_bus", {})
    # Group the reverse map by VMID to find VMs that appear under more than one bus.
    buses_by_vmid: Dict[str, List[str]] = {}
    for bus, vmid in b2v.items():
        buses_by_vmid.setdefault(str(vmid), []).append(bus)
    # VMIDs whose duplicate entries were removed, and whether anything changed.
    pruned: List[int] = []
    changed = False
    # Examine each VM's list of buses.
    for vmid, buses in buses_by_vmid.items():
        # Exactly one bus: make sure the forward map agrees with it.
        if len(buses) == 1:
            if v2b.get(vmid) != buses[0]:          # repair, don't orphan
                v2b[vmid] = buses[0]
                changed = True
            continue
        # Duplicates: keep the bus the forward map names, else the most recently added one.
        keep = v2b.get(vmid) if v2b.get(vmid) in buses else buses[-1]
        # Remove every other bus for this VM, along with its per-bus records.
        for b in buses:
            if b == keep:
                continue
            b2v.pop(b, None)
            st.get("missing_since", {}).pop(b, None)
            st.get("vidpid_by_bus", {}).pop(b, None)
            changed = True
            # Record the VMID (only numeric ones) as pruned.
            if str(vmid).lstrip("-").isdigit():
                pruned.append(int(vmid))
        # Make the forward map point at the kept bus.
        if v2b.get(vmid) != keep:
            v2b[vmid] = keep
            changed = True
    # Persist only if anything changed.
    if changed:
        save_usb_state(st)
    # Unique, sorted list of pruned VMIDs.
    return sorted(set(pruned))


def bus_for_vmid(vmid: int) -> Optional[str]:
    """Return the bus currently assigned to ``vmid``, or None if it has none."""
    # Look it up in the freshly loaded forward map.
    return load_usb_state()["vmid_to_bus"].get(str(int(vmid)))
