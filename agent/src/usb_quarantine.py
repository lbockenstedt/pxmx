"""USB dongle quarantine + bus-exclusion + destroy-fail bookkeeping.

Owns the two quarantine-related JSON files (extracted from ``usb_provision`` so
the provisioning "brain" is separable from its fault-tracking state):

  * ``usb_quarantine.json`` — buses sidelined after kernel USB errors
  * ``destroy_fails.json``  — per-VM destroy-failure counters

Also holds the bus-EXCLUSION helpers (hub-deleted buses skipped for a cooldown),
which live on the shared ``usb_state.json`` document — those read/write it via
``usb_state_store``. These functions are re-exported from ``usb_provision`` so
existing ``usb_provision.X`` callers (and its own internal unqualified calls) are
unchanged.
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional

from . import usb_state_store

logger = logging.getLogger("PxmxAgent")

# Mirrors usb_provision.PXMLIB (this module owns its JSON files).
PXMLIB = "/var/lib/pxmx"
USB_QUARANTINE_FILE = f"{PXMLIB}/usb_quarantine.json"
DESTROY_FAILS_FILE = f"{PXMLIB}/destroy_fails.json"
QUARANTINE_MAX_FAILS = 3  # bash line 1217: a bus is quarantined after 3 fails
DESTROY_MAX_FAILS = 3     # bash line 43, hardcoded — VM declared orphan after this
# A quarantined dongle (dmesg kernel USB errors — the ONLY quarantine path now)
# auto-recovers after this long, present OR absent: a still-plugged dongle gets a
# fresh provisioning attempt, and if the kernel errors persist it re-quarantines.
# 1h keeps a genuinely faulty dongle sidelined long enough to be noticed while
# never stranding a good dongle that hit a transient kernel hiccup.
QUARANTINE_RECOVERY_S = 3600

# Kernel USB errors for a SPECIFIC device/port → quarantine that dongle so a
# faulty port/dongle isn't re-provisioned. The kernel logs the bus id (e.g.
# "usb 3-1.2: device descriptor read/64, error -71") — that id IS the sysfs
# bus_path dongles are tracked by. Distinct from the watchdog's subsystem-level
# scrape (xhci_hcd died); this is per-dongle. -71 = EPROTO, -110 = ETIMEDOUT.
_USB_DMESG_ERROR_RE = re.compile(
    r"usb (\d+-[\d.]+):.*?("
    r"device descriptor read|unable to enumerate|not accepting address|"
    r"error -71\b|error -110\b|can't set config|cannot enable port|reset .*fail"
    r")", re.IGNORECASE)
_DMESG_USB_WINDOW_S = 180        # look back this far in the kernel log
_DMESG_USB_QUARANTINE_MIN = 3    # >= this many error lines in-window → quarantine


# ── bus exclusion (on the shared usb_state.json document) ──────────────────
def clear_excluded_buses() -> int:
    """Wipe all bus exclusions (bash ``provision_unassigned`` dispatch 4078-4084).
    Returns the count cleared."""
    st = usb_state_store.load_usb_state()
    n = len(st.get("excluded_buses", {}))
    st["excluded_buses"] = {}
    usb_state_store.save_usb_state(st)
    return n


def exclude_bus(bus: str) -> None:
    # Store the exclusion TIMESTAMP (not a bare 1) so the provision loop can
    # auto-return the bus after EXCLUDE_COOLDOWN_S. Legacy bare-1 values are
    # treated as already-expired by the reconcile cooldown clear.
    st = usb_state_store.load_usb_state()
    st["excluded_buses"][bus] = time.time()
    usb_state_store.save_usb_state(st)


# ── dongle quarantine (usb_quarantine.json) ───────────────────────────────
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


async def scan_dmesg_usb_errors(window_s: int = _DMESG_USB_WINDOW_S) -> Dict[str, int]:
    """Kernel-log per-device USB errors → ``{bus_path: error_line_count}`` over
    the last *window_s*. A faulty port/dongle logs ``usb 3-1.2: device
    descriptor read/64, error -71`` etc.; the bus id (3-1.2) IS the sysfs
    bus_path dongles are tracked by, so a persistently-erroring bus can be
    quarantined and not re-provisioned. Best-effort via ``journalctl -k``; empty
    dict on any failure (never quarantines on missing data)."""
    from . import pve_cmds  # deferred — avoid a top-level import cycle
    try:
        rc, out, _ = await pve_cmds._run(
            ["journalctl", "-k", "--no-pager", "-o", "cat",
             "--since", f"-{int(window_s)}s"], check=False, timeout=10)
    except Exception:  # noqa: BLE001
        return {}
    if rc != 0 or not out:
        return {}
    errors: Dict[str, int] = {}
    for line in out.splitlines():
        m = _USB_DMESG_ERROR_RE.search(line)
        if m:
            errors[m.group(1)] = errors.get(m.group(1), 0) + 1
    return errors


def quarantine_bus(bus: str, reason: str) -> None:
    """Force a bus into quarantine (fails = QUARANTINE_MAX_FAILS) so the provision
    loop skips it, tagged with a reason. Idempotent — no-ops if already quarantined
    at/above the threshold. Auto-clears via the loop's absent-dongle sweep."""
    q = _read_quarantine()
    entry = q.get(bus) or {}
    if int(entry.get("fails", 0)) >= QUARANTINE_MAX_FAILS:
        return
    q[bus] = {"fails": QUARANTINE_MAX_FAILS, "since": int(time.time()), "reason": reason}
    _save_quarantine(q)


# ── destroy-fail counters (destroy_fails.json) ────────────────────────────
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
        usb_state_store.add_orphan_vm(int(vmid), bus)
    _save_destroy_fails(fails)
    return {"count": count, "orphaned": orphaned}


def clear_destroy_fails(vmid: int) -> None:
    fails = _read_destroy_fails()
    fails.pop(str(int(vmid)), None)
    _save_destroy_fails(fails)
