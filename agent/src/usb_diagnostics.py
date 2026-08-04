"""Missing-dongle diagnostics + probable-cause correlation for the Proxmox host.

Answers the operator question the existing surfaces can't: *"``lsusb`` showed 10
dongles on Monday and 7 today — where did they go, and why?"*

The provisioning health ladder (``usb_provision._HEALTH_LADDER``) only recovers
dongles it can still SEE: its ``usb_reset`` rung writes
``/sys/bus/usb/devices/<bus>/authorized``, a path that stops existing the moment
the device falls off the bus. A dongle that vanishes from the kernel's device
list is therefore invisible to every rung, which is why only a host reboot (or a
physical re-plug) brings it back. This module makes that class of loss visible
and attributes a cause.

Three collectors, all best-effort and non-fatal:

* **presence roster** (``usb_presence.json``) — every certified dongle bus ever
  seen, with first/last-seen and ``missing_since``. Persisted, so a loss that
  accumulates over DAYS survives agent restarts. This is the only record that a
  dongle used to be here at all.
* **environment** — per-bus USB autosuspend settings, the xHCI controllers and
  how many devices each currently carries, and whether ``uhubctl`` is installed
  AND has any PPPS-capable (per-port-power-switching) hub to act on. PPPS is the
  gate on the "power-cycle the ports instead of rebooting" path: most on-board
  root hubs do NOT support it, so this reports whether that option exists on
  THIS host rather than leaving it to be discovered by hand.
* **kernel evidence** — categorised ``journalctl -k`` counts over a long window
  (the quarantine scanner's window is 180s, which catches a drop in the act but
  keeps no history for a multi-day decay).

``correlate()`` then ranks probable causes from that evidence. It deliberately
reports "no kernel evidence" as its own outcome rather than guessing: a silent
disappearance with a clean log points at VBUS/PHY (→ power-cycle territory),
which is a materially different fix from link errors or autosuspend.
"""

import asyncio
import copy as _copy
import glob
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PxmxAgent")


def _text(raw) -> str:
    """Decode ``pve_cmds._run`` output. It returns (rc, stdout, stderr) as
    BYTES; treating stdout as str raised
    ``TypeError: a bytes-like object is required, not 'str'`` on the first
    ``"usb" not in line`` and took the whole collect() down, so every host
    reported an EMPTY diagnostic while the presence roster kept updating."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw or ""

PXMLIB = "/var/lib/pxmx"
USB_PRESENCE_FILE = f"{PXMLIB}/usb_presence.json"
# Boot-anchored baseline: what the host could see shortly after THIS boot.
# Kept in its own file rather than as a key inside the roster, whose every key
# is treated as a bus path by missing_dongles().
USB_BOOT_FILE = f"{PXMLIB}/usb_boot_baseline.json"
# Rolling kernel-event accumulator. Each pass reads only the log SINCE THE LAST
# SCAN and folds it into hourly buckets, so a 24h view costs one ~5-minute read
# per pass instead of re-reading the whole day every time (which is what pulled
# 2.4 GB into the agent and got it watchdog-killed). Persisted because the agent
# restarts often enough that an in-memory accumulator would keep resetting.
USB_KERNEL_FILE = f"{PXMLIB}/usb_kernel_events.json"
# First run has no "last scan" to read from; seed with this much rather than the
# full window, so a cold start is cheap too.
KERNEL_SEED_WINDOW_S = 3600
# A baseline captured more than this long into a boot is NOT a boot snapshot
# (the agent was restarted mid-session); reported as such rather than trusted.
BOOT_BASELINE_GRACE_S = 900

# How far back the diagnostic kernel scan looks. The quarantine scanner uses
# 180s (it must react fast); a dongle count that decays over days needs a window
# wide enough to hold the event that removed one. journalctl reads a compressed
# on-disk journal, so this stays cheap.
DIAG_KERNEL_WINDOW_S = 86400

# A dongle only counts as INVENTORY once it has been around this long. A stick
# plugged in briefly — bench-testing, a swap, a wrong port — should not become a
# permanent "missing" entry the moment it is unplugged again. Anything younger
# is reported as transient and kept out of the headline loss count.
INVENTORY_MIN_AGE_S = 4 * 3600

# A roster entry not seen for this long is dropped — a dongle genuinely removed
# from the host shouldn't be reported "missing" forever.
ROSTER_TTL_S = 30 * 86400

# A dongle that returns after being CONTINUOUSLY absent for at least this long is
# treated as a physical swap rather than a fault flap. The discriminator is
# duration: a failing controller drops a port and re-enumerates it within
# seconds, while a human walking to the rack cannot do it in under minutes.
REPLACEMENT_MIN_ABSENT_S = float(
    os.environ.get("LM_PXMX_REPLACEMENT_MIN_ABSENT_S", "300") or 300)
# Absence samples kept per bus. Turns "I think they drop and come back quickly"
# into a measurement — a port whose absences are all single-digit seconds is
# flapping, not being serviced.
_ABSENCE_SAMPLES = 10

# Kernel lines that explain a disappearance, by category. Ordered most-specific
# first; a line is counted once, under the first category that matches.
_KERNEL_PATTERNS = [
    ("over_current", re.compile(
        r"(over-?current|overcurrent)", re.IGNORECASE)),
    ("insufficient_power", re.compile(
        r"(insufficient available bus power|rejecting .*insufficient)", re.IGNORECASE)),
    ("link_error", re.compile(
        r"(device descriptor read|unable to enumerate|not accepting address|"
        r"error -71\b|error -110\b|can't set config|cannot enable port|"
        r"reset .*fail)", re.IGNORECASE)),
    ("disconnect", re.compile(
        r"USB disconnect, device number", re.IGNORECASE)),
    ("controller_fault", re.compile(
        r"(xhci_hcd.*(died|halt|Host (controller )?(not responding|halt)|"
        r"HC died|TRB DMA|command.*abort))|ehci_hcd.*(died|halt)", re.IGNORECASE)),
]
# Bus id on a kernel USB line ("usb 3-1.2: ..."), matching the sysfs bus_path
# every other module keys dongles by.
_KERNEL_BUS_RE = re.compile(r"usb (\d+-[\d.]+)")


# ── boot baseline ────────────────────────────────────────────────────────────

def boot_id() -> str:
    """Kernel boot id — changes on every reboot, stable across agent restarts.
    Anchors the baseline to a BOOT rather than to a process lifetime."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _uptime_s() -> Optional[float]:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _load_boot_baseline() -> Dict[str, Any]:
    try:
        if os.path.exists(USB_BOOT_FILE) and os.path.getsize(USB_BOOT_FILE) > 0:
            with open(USB_BOOT_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("usb_diagnostics: boot baseline read failed: %s", e)
    return {}


def capture_boot_baseline(present_by_bus: Dict[str, Any],
                          now: Optional[float] = None) -> Dict[str, Any]:
    """Snapshot the dongles visible for THIS boot; re-captured only on reboot.

    The roster answers "was this ever here", which spans reboots and therefore
    mixes real losses with bus RENUMBERING and with controllers that were handed
    to a VM. A boot baseline answers the unambiguous question instead: what could
    this host see at boot, and what has it lost since? Bus ids are stable within
    a boot, so a difference against this set is a real loss with no caveats.

    ``trusted`` is False when the baseline had to be taken well into a boot
    (agent installed or restarted mid-session) — it is then a mid-life sample,
    not a boot snapshot, and the UI must not present it as one.
    """
    now = time.time() if now is None else now
    bid = boot_id()
    cur = _load_boot_baseline()
    if cur.get("boot_id") == bid and bid:
        return cur
    up = _uptime_s()
    baseline = {
        "boot_id": bid,
        "captured_at": now,
        "uptime_s_at_capture": up,
        "trusted": bool(up is not None and up <= BOOT_BASELINE_GRACE_S),
        "count": len(present_by_bus or {}),
        # Controller per bus, captured NOW — the baseline is taken at boot, which
        # is BEFORE the VMs claim their PCI controllers, so the ~4 dongles per
        # host destined for T1/T3 passthrough ARE in this snapshot. Recording
        # where each one lived is what lets them be recognised as "handed to a
        # VM" later instead of counted as losses. Held here rather than read from
        # the roster so the classification survives a roster reset.
        "buses": {b: {"vidpid": (i or {}).get("vidpid") or "",
                      "product": (i or {}).get("product") or b,
                      "pci_controller": usb_device_controller(b)}
                  for b, i in (present_by_bus or {}).items()},
    }
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        tmp = f"{USB_BOOT_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(baseline, f)
        os.replace(tmp, USB_BOOT_FILE)
        logger.info("usb_diagnostics: boot baseline captured — %d dongle(s), "
                    "%.0fs into boot%s", baseline["count"], up or -1,
                    "" if baseline["trusted"] else " (NOT a boot snapshot)")
    except OSError as e:
        logger.debug("usb_diagnostics: boot baseline write failed: %s", e)
    return baseline


def lost_since_boot(baseline: Dict[str, Any], present_by_bus: Dict[str, Any],
                    passthrough_addrs: Optional[set] = None,
                    roster: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Buses present at boot that are gone NOW — the unambiguous loss count.

    Bus ids are stable within a boot, so unlike the roster this cannot be
    confused by renumbering. Entries whose controller has since been passed
    through to a VM are still excluded: those left the host on purpose.
    """
    pt = passthrough_addrs or set()
    ros = roster or {}
    out: List[Dict[str, Any]] = []
    for bus, info in ((baseline or {}).get("buses") or {}).items():
        if bus in (present_by_bus or {}):
            continue
        # Baseline's own record first — written while the dongle was present at
        # boot. The roster is a fallback for baselines predating this stamping.
        ctrl = ((info or {}).get("pci_controller")
                or (ros.get(bus) or {}).get("pci_controller") or "")
        out.append({"bus_path": bus,
                    "vidpid": (info or {}).get("vidpid") or "",
                    "product": (info or {}).get("product") or bus,
                    "pci_controller": ctrl,
                    "observed_s": _observed_s(ros.get(bus) or {}),
                    "established": _is_established(ros.get(bus) or {}),
                    # Absent because its controller went to a VM — expected, not
                    # a loss. Reported rather than dropped so the count stays
                    # explainable ("4 of 5 absent are passthrough").
                    "passed_through": bool(ctrl and ctrl in pt)})
    out.sort(key=lambda x: x["bus_path"])
    return out


# ── presence roster ──────────────────────────────────────────────────────────

_ROSTER_CACHE: Optional[tuple] = None


def _load_roster() -> Dict[str, Any]:
    """Presence roster, cached on the file's (mtime_ns, size).

    Re-parsed only when the file actually changes. cs_usb_telemetry loads this
    on every telemetry tick -- every 3s while provisioning is active -- and a
    JSON parse per tick for a file that changes once every few minutes is the
    kind of steady drain that does not look like a bug in any single profile.
    A DEEP copy is returned: record_presence mutates the entries it is handed,
    and a shallow dict() shares the nested per-bus dicts, so the cache would be
    poisoned by every caller. Deep-copying ~20 small dicts is microseconds --
    still far cheaper than the file read and JSON parse it replaces.
    """
    global _ROSTER_CACHE
    try:
        st = os.stat(USB_PRESENCE_FILE)
        if st.st_size <= 0:
            return {}
        key = (st.st_mtime_ns, st.st_size)
        if _ROSTER_CACHE is not None and _ROSTER_CACHE[0] == key:
            return _copy.deepcopy(_ROSTER_CACHE[1])
        with open(USB_PRESENCE_FILE) as f:
            d = json.load(f)
        d = d if isinstance(d, dict) else {}
        _ROSTER_CACHE = (key, d)
        return _copy.deepcopy(d)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("usb_diagnostics: roster read failed: %s", e)
    return {}


def _save_roster(d: Dict[str, Any]) -> None:
    # Invalidate the read cache FIRST. _load_roster keys on (mtime_ns, size),
    # and record_presence is read-modify-write: a rewrite of the same byte
    # length inside one mtime tick is indistinguishable from no change, so the
    # next read would serve the pre-write roster. Caught by a test where a
    # dongle's return was silently lost. The writer owning invalidation makes
    # the cache correct regardless of filesystem timestamp resolution.
    global _ROSTER_CACHE
    _ROSTER_CACHE = None
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        tmp = f"{USB_PRESENCE_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, USB_PRESENCE_FILE)   # atomic: a torn write loses the history
    except OSError as e:
        logger.debug("usb_diagnostics: roster write failed: %s", e)


def record_presence(present_by_bus: Dict[str, Dict[str, Any]],
                    now: Optional[float] = None) -> Dict[str, Any]:
    """Fold this tick's certified-dongle scan into the persisted roster.

    ``present_by_bus`` is ``cs_usb_telemetry``'s certified map (bus_path → entry).
    Returns the updated roster. A bus that reappears clears its ``missing_since``
    so a flapping dongle doesn't read as permanently gone.
    """
    now = time.time() if now is None else now
    roster = _load_roster()
    _slots = _pci_slot_map()          # read once, identical for every device
    _bid = boot_id()
    try:
        _pt_addrs = set(passthrough_controllers())
    except Exception:  # noqa: BLE001 — a missing set must not block the roster
        _pt_addrs = set()
    for bus, info in (present_by_bus or {}).items():
        e = roster.get(bus) or {}
        e["vidpid"] = (info or {}).get("vidpid") or e.get("vidpid") or ""
        e["product"] = (info or {}).get("product") or e.get("product") or ""
        e["type"] = (info or {}).get("type") or e.get("type") or ""
        # Owning PCI controller, captured WHILE PRESENT — a device that is gone
        # can no longer be walked up the sysfs tree, so this is the only chance
        # to record where it lived. It is what lets a missing dongle be
        # attributed to a passed-through controller rather than reported lost.
        _ctrl = usb_device_controller(bus)
        if _ctrl:
            e["pci_controller"] = _ctrl
            # Physical location, recorded for the SAME reason as the controller:
            # once the device is gone the sysfs walk is impossible, so "which
            # card / which port was it in" has to be captured while it is here.
            e["location"] = usb_location(bus, _ctrl, _slots)
            e["port"] = usb_port_of(bus)
        # Self-healing rename. When a reboot renumbers the bus, THIS dongle shows
        # up under a new path while its old entry lingers as a phantom "missing"
        # row. Same physical port (same stable id) + the old path no longer
        # present => it is the same device, so absorb the old entry's history and
        # drop it. Without this the roster grows by a few entries per reboot and
        # every one of them reports as a permanent loss.
        _sid = _stable_id(bus, _ctrl)
        if _sid:
            e["stable_id"] = _sid
            for _ob, _oe in list(roster.items()):
                if (_ob != bus and (_oe or {}).get("stable_id") == _sid
                        and _ob not in (present_by_bus or {})):
                    _of = (_oe or {}).get("first_seen")
                    if isinstance(_of, (int, float)):
                        e["first_seen"] = min(e.get("first_seen", _of), _of)
                    roster.pop(_ob, None)
                    logger.info("usb_diagnostics: %s renumbered to %s (same port "
                                "%s) — merged history", _ob, bus, _sid)
        e.setdefault("first_seen", now)
        e["last_seen"] = now
        _ms = e.get("missing_since")
        if _ms is not None:
            try:
                _absent = max(0.0, now - float(_ms))
            except (TypeError, ValueError):
                _absent = 0.0
            e["last_absence_s"] = round(_absent, 1)
            _hist = list(e.get("recent_absences") or [])
            _hist.append(round(_absent, 1))
            e["recent_absences"] = _hist[-_ABSENCE_SAMPLES:]
            # A swap, or a flap? Duration decides. Two guards, because both of
            # these produce a LONG absence that is not a swap:
            #   * the controller was passed through to a VM -- every dongle
            #     behind it vanishes for the life of that VM, so a teardown
            #     would otherwise read as a fleet-wide dongle swap;
            #   * the host rebooted -- every port repopulates at once, which
            #     would clear quarantine everywhere in one go.
            _same_boot = (e.get("missing_boot_id") or "") == (_bid or "")
            _was_pt = bool(e.get("missing_passthrough"))
            if (_absent >= REPLACEMENT_MIN_ABSENT_S and _same_boot and not _was_pt):
                e["replaced_at"] = now
                e["replaced_after_s"] = round(_absent, 1)
                logger.info("usb_diagnostics: %s returned after %.0fs absent — "
                            "treating as a REPLACED dongle (flaps return in seconds)",
                            bus, _absent)
        e["missing_since"] = None
        e.pop("missing_boot_id", None)
        e.pop("missing_passthrough", None)
        roster[bus] = e
    # Stamp the moment each known-but-absent bus went away, and expire entries
    # whose dongle has been gone long enough to count as removed, not lost.
    for bus in list(roster):
        e = roster.get(bus) or {}
        if bus in (present_by_bus or {}):
            continue
        if e.get("missing_since") is None:
            e["missing_since"] = now
            # Captured when it goes absent: on return this is the only way to
            # tell a serviced port from a reboot.
            e["missing_boot_id"] = _bid
        # Passthrough is re-evaluated on EVERY absent pass and latched, not
        # sampled once. A controller can be handed to a VM after its dongles
        # have already dropped, and a single sample at disappearance would miss
        # that -- then the VM teardown hours later would read as a dongle swap
        # and clear quarantine across the whole card. Sticky OR: if the
        # controller was passed through at ANY point during the absence, the
        # absence is not evidence of a human.
        if (e.get("pci_controller") or "") in _pt_addrs:
            e["missing_passthrough"] = True
        if now - float(e.get("last_seen") or now) > ROSTER_TTL_S:
            roster.pop(bus, None)
            continue
        roster[bus] = e
    _save_roster(roster)
    return roster


def _stable_id(bus: str, controller: str) -> str:
    """A dongle identity that survives a reboot: ``<pci_addr>:<port chain>``.

    The kernel BUS NUMBER in a path like ``3-1`` is assigned in controller
    enumeration order and can shift between boots; the port chain after the dash
    (``1``, ``2.3``) is the physical topology and does not. Keying history on the
    raw bus path therefore invents a NEW dongle every time the numbering moves —
    a host with 18 physical dongles and two reboots reported 23 "ever seen",
    which is where the phantom missing entries came from.
    """
    if not controller or "-" not in bus:
        return ""
    return f"{controller}:{bus.split('-', 1)[1]}"


def _observed_s(entry: Dict[str, Any]) -> Optional[int]:
    """Seconds between a roster entry's first and last sighting."""
    fs, ls = (entry or {}).get("first_seen"), (entry or {}).get("last_seen")
    if isinstance(fs, (int, float)) and isinstance(ls, (int, float)) and ls >= fs:
        return int(ls - fs)
    return None


def _is_established(entry: Dict[str, Any]) -> bool:
    """True once a dongle has been present long enough to count as inventory.

    A dongle can legitimately be connected and then removed. Without this, any
    stick plugged in for ten minutes became a permanent "missing" row the moment
    it was pulled — noise that would bury the real losses.
    """
    obs = _observed_s(entry)
    return obs is not None and obs >= INVENTORY_MIN_AGE_S


def purge_history() -> Dict[str, Any]:
    """Delete the presence roster + boot baseline. Operator-triggered reset for
    after a deliberate hardware change (dongles moved, ports rewired, a card
    pulled), where the recorded history describes a machine that no longer
    exists and would otherwise report permanent phantom losses. The next pass
    rebuilds both from what is actually attached."""
    removed = []
    for f in (USB_PRESENCE_FILE, USB_BOOT_FILE):
        try:
            if os.path.exists(f):
                os.remove(f)
                removed.append(os.path.basename(f))
        except OSError as e:
            logger.warning("usb_diagnostics: purge failed for %s: %s", f, e)
    logger.info("usb_diagnostics: history purged (%s)", ", ".join(removed) or "nothing to remove")
    return {"purged": removed}


def missing_dongles(roster: Dict[str, Any], present_by_bus: Dict[str, Any],
                    now: Optional[float] = None,
                    passthrough_addrs: Optional[set] = None) -> List[Dict[str, Any]]:
    """Roster entries whose bus is no longer on the host, newest loss first.

    Each entry carries ``passed_through``: True when the dongle's last-known PCI
    controller is now bound to vfio-pci, i.e. the whole controller was handed to
    a VM (T1/T3 PCI passthrough) and the host is SUPPOSED to stop seeing it.
    Roughly 4 dongles per host are permanently in this state, so counting them
    as losses would keep the panel permanently red for normal operation.
    """
    now = time.time() if now is None else now
    pt = passthrough_addrs or set()
    out: List[Dict[str, Any]] = []
    for bus, e in (roster or {}).items():
        if bus in (present_by_bus or {}):
            continue
        e = e or {}
        ms = e.get("missing_since")
        out.append({
            "bus_path": bus,
            "vidpid": e.get("vidpid") or "",
            "product": e.get("product") or bus,
            "type": e.get("type") or "",
            "first_seen": e.get("first_seen"),
            "last_seen": e.get("last_seen"),
            "missing_since": ms,
            "missing_for_s": int(now - float(ms)) if isinstance(ms, (int, float)) else None,
            # How long it was actually around before vanishing. Below
            # INVENTORY_MIN_AGE_S it was never really part of the inventory.
            "observed_s": _observed_s(e),
            "established": _is_established(e),
            "pci_controller": e.get("pci_controller") or "",
            "passed_through": bool(e.get("pci_controller") and e["pci_controller"] in pt),
        })
    out.sort(key=lambda x: -(x.get("missing_since") or 0))
    return out


# ── environment probes ───────────────────────────────────────────────────────

def usb_power_settings() -> List[Dict[str, Any]]:
    """Per-device USB runtime-PM settings from sysfs.

    ``power/control == "auto"`` means the kernel may autosuspend that device. A
    dongle whose firmware doesn't resume cleanly then disappears — the classic
    slow multi-day decay — so this is the first thing to rule in or out.
    """
    out: List[Dict[str, Any]] = []
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        bus = os.path.basename(dev)
        if ":" in bus or bus.startswith("usb"):
            continue  # interface child / root hub
        def _read(rel):
            try:
                with open(os.path.join(dev, rel)) as f:
                    return f.read().strip()
            except OSError:
                return None
        vid, pid = _read("idVendor"), _read("idProduct")
        if not vid or not pid:
            continue
        ctrl = _read("power/control")
        _delay_raw = _read("power/autosuspend_delay_ms")
        try:
            _delay_ms = int(_delay_raw) if _delay_raw not in (None, "") else None
        except (TypeError, ValueError):
            _delay_ms = None
        out.append({
            "bus_path": bus,
            "vidpid": f"{vid}:{pid}",
            "product": _read("product") or bus,
            "control": ctrl,
            "autosuspend_delay_ms": _delay_raw,
            # control=auto ALONE does not mean the device will suspend.
            # usbcore.autosuspend=-1 disables autosuspend by setting a NEGATIVE
            # default delay while power/control legitimately stays "auto" — so
            # testing control alone false-flags a host that is already fixed
            # (observed: a host booted with usbcore.autosuspend=-1 still showed
            # 4/17 "auto" and was told to set the parameter it already had).
            # Both conditions are required: auto AND a non-negative delay.
            "autosuspend_delay_ms_val": _delay_ms,
            "autosuspend_enabled": (ctrl == "auto"
                                    and _delay_ms is not None and _delay_ms >= 0),
        })
    return out


# PCI drivers that mean "this controller has been handed to a VM". A controller
# bound to vfio-pci is no longer the host's: every USB device behind it leaves
# the host's device list BY DESIGN (T1/T3 PCI passthrough hands a whole USB
# controller card to a guest). Those dongles are not lost — they are exactly
# where they are supposed to be.
_PASSTHROUGH_DRIVERS = ("vfio-pci", "pci-stub")


def passthrough_controllers() -> List[str]:
    """PCI addresses of USB controllers currently handed to a VM.

    Same derivation usb_controllers() uses for its ``passthrough`` flag, hoisted
    so record_presence can consult it WITHOUT the recursive sysfs walk that
    function does per controller -- it runs on the telemetry path.
    """
    out: List[str] = []
    for path in glob.glob("/sys/bus/pci/devices/0000:*"):
        addr = os.path.basename(path)
        try:
            with open(os.path.join(path, "class")) as f:
                if not f.read().strip().startswith("0x0c03"):
                    continue
        except OSError:
            continue
        if _pci_driver(addr) in _PASSTHROUGH_DRIVERS:
            out.append(addr)
    return out


def _pci_driver(addr: str) -> str:
    """Driver currently bound to a PCI address ('' when unbound)."""
    try:
        return os.path.basename(os.path.realpath(
            f"/sys/bus/pci/devices/{addr}/driver"))
    except OSError:
        return ""


# ── Physical location of a USB controller ───────────────────────────────────
# "Which card is this dongle on, and which port of it?" A bare PCI address
# (0000:80:14.0) does not answer that for anyone standing at the rack.
#
# Deliberately sysfs-only — NO subprocess. lspci/dmidecode would give prettier
# vendor strings and the board's printed slot label, but this module already
# cost the fleet an outage by doing expensive work on a watchdog-fed loop, and
# every field below is a file read of a few bytes.

# ACPI exposes physical slots at /sys/bus/pci/slots/<N>/address, where the
# address is domain:bus:device (NO function). A controller that appears there
# is in a physical slot; a chipset controller does not appear at all. That
# presence/absence IS the onboard-vs-card answer where firmware provides it.
_SLOT_MAP_CACHE: Optional[Dict[str, str]] = None


def _pci_slot_map() -> Dict[str, str]:
    """``{'0000:04:00': '1'}`` — physical slot number by domain:bus:device.

    Cached for the life of the process: the ACPI slot table is fixed while the
    box is up, and this is called from cs_usb_telemetry, which the telemetry
    loop runs every 3s while provisioning is active (20x/min). A glob per tick
    for a constant is pure waste.
    """
    global _SLOT_MAP_CACHE
    if _SLOT_MAP_CACHE is not None:
        return _SLOT_MAP_CACHE
    out: Dict[str, str] = {}
    for p in glob.glob("/sys/bus/pci/slots/*/address"):
        try:
            with open(p) as f:
                addr = f.read().strip()
        except OSError:
            continue
        if addr:
            out[addr] = os.path.basename(os.path.dirname(p))
    _SLOT_MAP_CACHE = out
    return out


# Chipset silicon — an Intel/AMD USB controller is integrated in practice.
# Only used as a FALLBACK when firmware exposes no slot table at all; the slot
# map is authoritative whenever it exists.
_CHIPSET_VENDORS = {"0x8086": "Intel", "0x1022": "AMD"}
_PCI_VENDOR_NAMES = {
    "0x8086": "Intel", "0x1022": "AMD", "0x1b21": "ASMedia",
    "0x1912": "Renesas", "0x1106": "VIA", "0x1b73": "Fresco Logic",
    "0x104c": "TI", "0x12d8": "Pericom",
}


def _sysfs_read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def pci_location(addr: str, slots: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Where a USB controller physically lives.

    Returns ``slot`` (physical slot number, None when onboard/unknown),
    ``onboard`` (bool or None when genuinely undetermined), ``vendor``, and
    ``label`` — a human string like ``"PCIe slot 1"`` or ``"onboard"``.

    ``label`` is '' when the answer is genuinely unknown. It NEVER hedges: a
    string like "add-in card or onboard" tells an operator nothing they didn't
    already know, so an empty label (which callers omit entirely) is better than
    a confident-looking non-answer.
    """
    unknown = {"pci_address": addr or "", "slot": None, "onboard": None,
               "vendor": "", "vidpid": "", "label": ""}
    if not addr:
        return unknown
    if slots is None:
        slots = _pci_slot_map()
    dbd = addr.rsplit(".", 1)[0]                  # strip the function
    slot = slots.get(dbd)
    vid = _sysfs_read(f"/sys/bus/pci/devices/{addr}/vendor")
    did = _sysfs_read(f"/sys/bus/pci/devices/{addr}/device")
    vendor = _PCI_VENDOR_NAMES.get(vid, vid or "")
    # PCI vid:pid of the CONTROLLER CARD itself (not the dongle). Identifies the
    # exact model, so a card can be matched to a purchase order or an errata
    # note without opening the chassis -- 'ASMedia' narrows it, '1b21:2142'
    # names it. Same 4-hex-digit form lspci prints.
    vidpid = f"{vid[2:]}:{did[2:]}" if (vid[:2] == "0x" and did[:2] == "0x") else ""
    card = " ".join(p for p in (vendor if vendor != vid else "", vidpid) if p)
    # Firmware-provided name (ACPI _DSM), e.g. "Onboard USB" on server boards.
    fw_label = _sysfs_read(f"/sys/bus/pci/devices/{addr}/label")
    # PCI bus number. Everything on bus 00 hangs off the root complex, i.e. it
    # is integrated silicon; an add-in card always sits on a higher bus behind a
    # PCIe root port. This is what makes onboard-vs-card answerable on boards
    # that publish NO ACPI slot table -- which is most of them, and is why this
    # used to return the useless "add-in card or onboard" hedge.
    try:
        bus_no = int(addr.split(":")[1], 16)
    except (IndexError, ValueError):
        bus_no = -1
    if slot:
        onboard, label = False, f"PCIe slot {slot}"
    elif slots:
        # The board DOES publish a slot table and this controller is not in it,
        # so it is integrated. The most reliable branch.
        onboard, label = True, "onboard"
    elif bus_no == 0:
        onboard, label = True, "onboard"
    elif vid in _CHIPSET_VENDORS:
        onboard, label = True, "onboard"
    elif bus_no > 0:
        # Definitely a card, just no slot NUMBER without an ACPI slot table.
        # Still worth saying: it narrows a hunt to the expansion slots.
        onboard, label = False, "add-in card"
    else:
        return unknown
    # Name the card on every branch -- knowing an onboard controller is an
    # Intel a36d is as useful as knowing the add-in card is an ASMedia 2142.
    if card:
        label = f"{label} ({card})"
    if fw_label:
        label = f"{label} · {fw_label}"
    return {"pci_address": addr, "slot": slot, "onboard": onboard,
            "vendor": vendor, "vidpid": vidpid, "label": label}


def usb_port_of(bus: str) -> str:
    """Physical port on the controller's root hub. ``'16-3'`` → ``'3'``;
    ``'16-2.4'`` → ``'2.4'`` (port 4 of a hub plugged into port 2)."""
    return bus.split("-", 1)[1] if "-" in bus else ""


def usb_location(bus: str, controller: str = "",
                 slots: Optional[Dict[str, str]] = None) -> str:
    """One-line physical location: ``'PCIe slot 1 · port 3'``.

    Either half may be unknown; joins only the parts that are, so an
    undeterminable controller yields ``'port 3'`` and not ``' · port 3'``.
    """
    loc = pci_location(controller or usb_device_controller(bus), slots)
    port = usb_port_of(bus)
    return " · ".join(p for p in (loc["label"], f"port {port}" if port else "") if p)


def location_for(bus: str, slots: Optional[Dict[str, str]] = None,
                 roster: Optional[Dict[str, Any]] = None) -> str:
    """Physical location of a bus — live when present, else from the roster.

    The dongles an operator actually needs to FIND are the quarantined and
    excluded ones, and those are frequently ABSENT: a dongle that dropped off
    the bus cannot be walked up the sysfs tree. Falling back to the location
    recorded while it was still present is the whole reason that field is
    persisted. Returns '' when neither source knows.
    """
    ctrl = usb_device_controller(bus)
    if ctrl:
        return usb_location(bus, ctrl, slots)
    r = roster if roster is not None else _load_roster()
    return ((r or {}).get(bus) or {}).get("location") or ""


def controller_for(bus: str, roster: Optional[Dict[str, Any]] = None) -> str:
    """Owning controller PCI address, live if present else from the roster.

    Sidelined dongles are grouped by this to tell 'three bad dongles' apart from
    'one bad card'. A display string would collide across two identical cards;
    the PCI address never does.
    """
    ctrl = usb_device_controller(bus)
    if ctrl:
        return ctrl
    r = roster if roster is not None else _load_roster()
    return ((r or {}).get(bus) or {}).get("pci_controller") or ""


# bus_path -> controller PCI address. A sysfs realpath per problem dongle per
# 3s tick; the mapping cannot change without the device re-enumerating, and a
# re-enumeration under a NEW bus path gets its own key.
_CTRL_CACHE: Dict[str, str] = {}


def usb_device_controller(bus: str) -> str:
    """PCI address of the controller a USB bus hangs off, '' if undetermined.

    Resolved from the sysfs topology: /sys/bus/usb/devices/3-1 realpaths to
    .../pci0000:80/0000:80:14.0/usb3/3-1, so the LAST PCI address before the
    usbN component owns the device. This is what lets a missing dongle be
    attributed to a controller — and therefore tell a genuine loss apart from a
    controller that was passed through to a VM.
    """
    cached = _CTRL_CACHE.get(bus)
    if cached is not None:
        return cached
    try:
        real = os.path.realpath(f"/sys/bus/usb/devices/{bus}")
    except OSError:
        return ""
    last = ""
    for part in real.split(os.sep):
        if re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]", part):
            last = part
        elif part.startswith("usb") and last:
            break
    # Only cache a POSITIVE result. A miss means the device is not on the bus
    # right now, and caching that would pin a dongle as "unknown controller"
    # for the rest of the process even after it comes back.
    if last:
        _CTRL_CACHE[bus] = last
    return last


def _usb_device_names() -> List[str]:
    """Every USB device name from the FLAT /sys/bus/usb/devices listing.

    One listdir. Entries are named by port chain -- ``usb16``, ``16-1``,
    ``16-1.2``, ``16-0:1.0`` -- so a device behind an external hub is already
    present at the top level and needs no tree walk to find.
    """
    try:
        return os.listdir("/sys/bus/usb/devices")
    except OSError:
        return []


def ports_on_bus(bus: str) -> int:
    """Physical downstream ports on the root hub owning *bus*.

    ``maxchild`` on usbN is the port count the controller actually exposes, so
    "3 of 4 ports down" can be stated as a fraction instead of a bare count.
    0 when it cannot be read.
    """
    num = str(bus or "").split("-", 1)[0].replace("usb", "")
    if not num.isdigit():
        return 0
    try:
        return int(_sysfs_read(f"/sys/bus/usb/devices/usb{num}/maxchild") or 0)
    except ValueError:
        return 0


def _devices_behind_root_hub(root_hub: str, names: Optional[List[str]] = None) -> int:
    """Count USB devices behind ``usbN``, INCLUDING those behind external hubs.

    This replaced a ``glob(usbdir + "/**/*-*", recursive=True)``. That walk did
    produce the right answer, and it very nearly took the fleet down: sysfs
    device directories nest (16-1 -> 16-1.2 -> ...) and carry per-endpoint and
    per-power subdirectories, so the recursive descent ran 60+ levels deep and
    effectively never returned. Because usb_controllers runs in a thread, every
    subsequent call added ANOTHER stuck thread -- 11 of them observed pinned at
    ~85% CPU each, 934% total, with RSS climbing as each accumulated results.
    py-spy showed all eleven inside _rlistdir.

    The recursion was never necessary. /sys/bus/usb/devices is FLAT: a device
    behind a hub still appears there as '16-1.2'. Matching the bus-number prefix
    is one listdir and gives the identical count. Names containing ':' are
    INTERFACES (16-0:1.0), not devices, and are excluded exactly as before.
    """
    m = re.match(r"usb(\d+)$", root_hub or "")
    if not m:
        return 0
    prefix = m.group(1) + "-"
    return sum(1 for n in (names if names is not None else _usb_device_names())
               if n.startswith(prefix) and ":" not in n)


def usb_controllers() -> List[Dict[str, Any]]:
    """USB controllers with device counts AND their current PCI driver binding.

    The PCI address is what an operator would unbind/rebind to force a full
    re-enumeration (the closest no-reboot equivalent of a power cycle), so it is
    surfaced verbatim. The device count makes a controller-wide loss visible:
    all dongles vanishing off ONE controller reads very differently from losses
    scattered across several.

    ``passthrough`` is the important one. A controller bound to vfio-pci has
    been handed to a VM (T1/T3 PCI passthrough), so the host CORRECTLY stops
    seeing every dongle behind it — historically ~4 per host. Without this the
    diagnostic reported those as missing dongles, which is a false alarm that
    would train an operator to ignore the panel.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    # Read the ACPI slot table ONCE and pass it down — it is identical for every
    # controller and does not change while the box is up.
    slots = _pci_slot_map()
    # Host-bound controllers, enumerated through their driver as before.
    for drv in ("xhci_hcd", "ehci-pci", "ehci_hcd"):
        for path in sorted(glob.glob(f"/sys/bus/pci/drivers/{drv}/0000:*")):
            addr = os.path.basename(path)
            seen.add(addr)
            buses, devices = [], 0
            for usbdir in sorted(glob.glob(os.path.join(path, "usb*"))):
                buses.append(os.path.basename(usbdir))
                devices += _devices_behind_root_hub(os.path.basename(usbdir))
            out.append({"driver": drv, "pci_address": addr, "root_hubs": buses,
                        "device_count": devices,
                        # Physical ports the card exposes, so a fault can be
                        # reported as "3 of 4" rather than a bare count.
                        "ports": sum(ports_on_bus(b) for b in buses),
                        "passthrough": False,
                        **{f"loc_{k}": v for k, v in pci_location(addr, slots).items()
                           if k != "pci_address"}})
    # Passed-through controllers are NOT under an xhci_hcd driver dir — find them
    # by PCI class (0c03 = USB controller) and report them explicitly, so a card
    # that vanished from the host is visible as "given to a VM" rather than
    # simply absent.
    for path in sorted(glob.glob("/sys/bus/pci/devices/0000:*")):
        addr = os.path.basename(path)
        if addr in seen:
            continue
        try:
            with open(os.path.join(path, "class")) as f:
                if not f.read().strip().startswith("0x0c03"):
                    continue
        except OSError:
            continue
        drv = _pci_driver(addr)
        out.append({"driver": drv or "(unbound)", "pci_address": addr,
                    "root_hubs": [], "device_count": 0,
                    "passthrough": drv in _PASSTHROUGH_DRIVERS,
                    **{f"loc_{k}": v for k, v in pci_location(addr, slots).items()
                       if k != "pci_address"}})
    return out


# ── USB hub topology ────────────────────────────────────────────────────────
# Built ahead of the planned hub rollout so the diagnostic is ready the day one
# is fitted, rather than discovered afterwards from a confusing symptom.
#
# A hub changes three things this module already cares about:
#   * bus paths gain a tier ("16-2.1" = port 1 of a hub in port 2) and those
#     DO renumber across reboots, unlike a PCI card's direct ports;
#   * a POWERED hub is usually PPPS-capable, which is the only way to cut VBUS
#     on one port -- a far less destructive recovery rung than usb_reset, which
#     yanks the device out from under a VM that holds it;
#   * an UNPOWERED hub splits one 500mA upstream port across everything behind
#     it, and an 802.11ac dongle draws ~300-450mA under load.

# Lab-tested ceiling: more than this many dongles behind one hub misbehaves.
# Matches the USB spec's 7 downstream ports per tier and the power budget above.
HUB_DONGLE_CEILING = int(os.environ.get("LM_PXMX_HUB_DONGLE_CEILING", "7") or 7)

_USB_HUB_CLASS = "09"


def hub_topology(ppps_hubs: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Every USB hub on the host, what hangs off it, and whether it can cut power.

    Pure sysfs, no subprocess -- this module runs on a watchdog-fed path and has
    already cost the fleet one outage by doing expensive work there.

    Per hub: ``ports`` (maxchild), the child DEVICE paths (interfaces excluded),
    ``self_powered`` and ``max_power_mA`` from the USB descriptor, whether
    ``uhubctl`` reported it PPPS-capable, and any ``warnings`` worth acting on.
    Root hubs (usbN) are included but flagged, since they are the controller
    itself and mostly do NOT support per-port power switching.
    """
    names = _usb_device_names()
    devices = [n for n in names if ":" not in n]
    # uhubctl identifies hubs as "<bus>-<port>" too, so a direct match works.
    ppps = {str((h or {}).get("hub") or "") for h in (ppps_hubs or [])}
    out: List[Dict[str, Any]] = []
    for name in sorted(devices):
        d = f"/sys/bus/usb/devices/{name}"
        if _sysfs_read(f"{d}/bDeviceClass") != _USB_HUB_CLASS:
            continue
        try:
            ports = int(_sysfs_read(f"{d}/maxchild") or 0)
        except ValueError:
            ports = 0
        # Children are one tier deeper: "16-2" -> "16-2.1"; a root hub "usb16"
        # owns the bare "16-N" paths.
        m = re.match(r"usb(\d+)$", name)
        if m:
            prefix, is_root = m.group(1) + "-", True
        else:
            prefix, is_root = name + ".", False
        kids = [n for n in devices
                if n.startswith(prefix) and "." not in n[len(prefix):]]
        # bmAttributes bit 6 (0x40) = self-powered. A bus-powered hub shares one
        # upstream 500mA budget across everything behind it.
        try:
            attrs = int(_sysfs_read(f"{d}/bmAttributes") or "0", 16)
        except ValueError:
            attrs = 0
        self_powered = bool(attrs & 0x40)
        raw_pw = _sysfs_read(f"{d}/bMaxPower") or ""
        try:
            max_power = int(re.sub(r"[^0-9]", "", raw_pw) or 0)
        except ValueError:
            max_power = 0
        warnings: List[str] = []
        if len(kids) > HUB_DONGLE_CEILING:
            warnings.append(
                f"{len(kids)} devices behind this hub — tested ceiling is "
                f"{HUB_DONGLE_CEILING}; expect enumeration and power problems above it")
        if not is_root and not self_powered and len(kids) > 2:
            warnings.append(
                "bus-powered hub carrying multiple devices — one 500mA upstream "
                "budget shared across them; an 802.11ac dongle draws ~300-450mA "
                "under load. Use a POWERED hub")
        out.append({
            "bus_path": name, "is_root_hub": is_root,
            "product": _sysfs_read(f"{d}/product"),
            "ports": ports, "attached": len(kids), "children": kids,
            "self_powered": self_powered, "max_power_mA": max_power,
            "ppps": name in ppps,
            "pci_controller": usb_device_controller(name),
            "warnings": warnings,
        })
    return out


async def uhubctl_support() -> Dict[str, Any]:
    """Whether the ``uhubctl`` per-port power-cycle path is available HERE.

    Two independent gates, reported separately so the answer is actionable:

    * ``installed`` — is the binary present at all;
    * ``ppps_hubs`` — hubs that actually support per-port power switching. This
      is the real gate: ``uhubctl`` can only cut VBUS on a hub whose descriptor
      advertises PPPS, and most on-board root hubs do not. An external POWERED
      hub usually does.

    ``supported`` is true only when both hold, i.e. `uhubctl -a cycle` would do
    something on this host. When it's false the fallback is an xHCI
    unbind/rebind (see ``usb_controllers``), which resets everything on the
    controller rather than one port.
    """
    from . import pve_cmds  # deferred — avoids a top-level import cycle
    out: Dict[str, Any] = {"installed": False, "supported": False,
                           "version": "", "ppps_hubs": [], "hubs_seen": 0,
                           "install_hint": "apt-get install -y uhubctl",
                           "error": ""}
    try:
        rc, _ver, _ = await pve_cmds._run(["uhubctl", "-v"], check=False, timeout=10)
        ver = _text(_ver)
    except FileNotFoundError:
        out["error"] = "uhubctl not installed"
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"version probe failed: {e}"
        return out
    if rc != 0 and not ver:
        out["error"] = "uhubctl not installed"
        return out
    out["installed"] = True
    out["version"] = (ver or "").strip().splitlines()[0] if ver else ""
    try:
        rc, _listing, _err = await pve_cmds._run(["uhubctl"], check=False, timeout=20)
        listing, err = _text(_listing), _text(_err)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"hub listing failed: {e}"
        return out
    if rc != 0 and not listing:
        # uhubctl exits nonzero with "no compatible devices detected"
        out["error"] = (err or "no compatible smart hubs detected").strip()
        return out
    for line in (listing or "").splitlines():
        if "Current status for hub" not in line:
            continue
        out["hubs_seen"] += 1
        m = re.search(r"Current status for hub ([\w.:-]+)", line)
        hub = m.group(1) if m else "?"
        # uhubctl prints the power-switching mode in the bracketed descriptor:
        # ppps = per-port (what we need), ganged = all-or-nothing, else none.
        mode = ("ppps" if re.search(r"\bppps\b", line, re.IGNORECASE)
                else "ganged" if re.search(r"\bganged\b", line, re.IGNORECASE)
                else "")
        desc = ""
        dm = re.search(r"\[(.+)\]", line)
        if dm:
            desc = dm.group(1)
        if mode == "ppps":
            out["ppps_hubs"].append({"hub": hub, "mode": mode, "description": desc})
    out["supported"] = bool(out["ppps_hubs"])
    if not out["supported"] and not out["error"]:
        out["error"] = ("no PPPS-capable hub — uhubctl cannot cut port power on "
                        "this host's hubs (an externally powered hub usually can)")
    return out


# Server-side filter so journalctl returns only the lines we categorise. Without
# it a 24h kernel read pulled the WHOLE journal into the agent (2.4 GB peak,
# ~1.7 cores) and the per-line regex sweep ran ON THE EVENT LOOP — starving the
# systemd watchdog and getting the agent SIGABRT'd every ~110s. Keep in sync with
# _KERNEL_PATTERNS.
_JOURNAL_GREP = (r"over-?current|overcurrent|insufficient available bus power|"
                 r"device descriptor read|unable to enumerate|not accepting address|"
                 r"error -71|error -110|can't set config|cannot enable port|"
                 r"USB disconnect, device number|xhci_hcd|ehci_hcd")
# Hard ceiling on lines pulled back even after filtering, so a pathological host
# can never balloon the agent again. 5000 is far more than the counts need.
_JOURNAL_MAX_LINES = int(os.environ.get("LM_PXMX_JOURNAL_MAX_LINES", "5000") or 5000)


# ── attribution: which kernel events did WE cause? ──────────────────────────
# The recovery ladder's usb_reset rung writes /sys/.../authorized 0 then 1. The
# kernel logs that as "USB disconnect" followed by "new ... USB device" -- and
# "disconnect" is one of the categories _KERNEL_PATTERNS counts as evidence.
#
# That makes the diagnostic CIRCULAR: the agent resets a dongle because it looks
# unhealthy, the reset logs a disconnect, and the disconnect is then counted as
# evidence the dongle is unhealthy. On one host 1005 such events accumulated in
# 24h, every one of them self-inflicted, which is exactly the trap this closes.
#
# The agent knows when it reset a bus, so it can subtract its own work. Resets
# are recorded here and kernel events on that bus within the attribution window
# are reported SEPARATELY rather than being silently dropped -- a reset that
# provokes a genuine link error is still worth seeing.
USB_RESET_LOG_FILE = f"{PXMLIB}/usb_resets.json"
# A reset's disconnect + re-enumeration land within a second or two; 15s is
# generous without swallowing an unrelated fault minutes later.
RESET_ATTRIBUTION_S = float(os.environ.get("LM_PXMX_RESET_ATTRIBUTION_S", "15") or 15)
_RESET_LOG_TTL_S = DIAG_KERNEL_WINDOW_S      # only useful as far back as we scan


def record_port_reset(bus: str, now: Optional[float] = None) -> None:
    """Note that WE just re-enumerated *bus*. Called by the usb_reset rung.

    Best-effort and never raises: failing to record makes an event look
    spontaneous, which is the safe direction (it over-reports hardware faults
    rather than hiding them).
    """
    if not bus:
        return
    now = time.time() if now is None else now
    try:
        d = {}
        if os.path.exists(USB_RESET_LOG_FILE):
            with open(USB_RESET_LOG_FILE) as f:
                d = json.load(f) or {}
        if not isinstance(d, dict):
            d = {}
        hist = [t for t in (d.get(bus) or []) if isinstance(t, (int, float))
                and now - t < _RESET_LOG_TTL_S]
        hist.append(now)
        d[bus] = hist[-200:]
        for k in list(d):
            d[k] = [t for t in (d.get(k) or [])
                    if isinstance(t, (int, float)) and now - t < _RESET_LOG_TTL_S]
            if not d[k]:
                d.pop(k, None)
        os.makedirs(PXMLIB, exist_ok=True)
        tmp = f"{USB_RESET_LOG_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, USB_RESET_LOG_FILE)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("usb_diagnostics: reset log write failed: %s", e)


def agent_reset_recently(bus: str, within_s: float, now: Optional[float] = None) -> bool:
    """Did the AGENT re-enumerate *bus* within the last *within_s* seconds?

    Lets a detector tell its own side effects apart from a hardware fault. Both
    usb_reset (authorized 0/1) and reattach (qm set -delete usbN / -usbN host=)
    take the device away from the VM holding it, which is indistinguishable from
    the dongle dropping its own passthrough unless the agent remembers doing it.
    """
    if not bus:
        return False
    now = time.time() if now is None else now
    try:
        hist = (_load_reset_log() or {}).get(bus) or []
        return any(isinstance(t, (int, float)) and 0 <= (now - t) <= within_s
                   for t in hist)
    except Exception:  # noqa: BLE001 — never block a detector on this
        return False


def _load_reset_log() -> Dict[str, List[float]]:
    try:
        if os.path.exists(USB_RESET_LOG_FILE):
            with open(USB_RESET_LOG_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def attribute_kernel_events(by_bus: Dict[str, Dict[str, int]],
                            resets: Optional[Dict[str, List[float]]] = None,
                            now: Optional[float] = None) -> Dict[str, Any]:
    """Split per-bus kernel event counts into self-inflicted vs spontaneous.

    A bus we reset N times in the window is credited with up to N disconnects --
    each reset produces exactly one. Anything beyond that, and every non-
    disconnect category, is reported as spontaneous: a reset explains a
    disconnect, it does not explain an over-current.
    """
    resets = _load_reset_log() if resets is None else resets
    now = time.time() if now is None else now
    self_n, spon_n = 0, 0
    detail: Dict[str, Dict[str, int]] = {}
    for bus, cats in (by_bus or {}).items():
        n_resets = len([t for t in (resets.get(bus) or [])
                        if isinstance(t, (int, float)) and (now - t) < DIAG_KERNEL_WINDOW_S])
        disc = int((cats or {}).get("disconnect", 0))
        credited = min(disc, n_resets)
        other = sum(int(v) for k, v in (cats or {}).items() if k != "disconnect")
        spont = (disc - credited) + other
        self_n += credited
        spon_n += spont
        if credited or spont:
            detail[bus] = {"self_inflicted": credited, "spontaneous": spont,
                           "resets_by_agent": n_resets}
    return {"self_inflicted": self_n, "spontaneous": spon_n, "by_bus": detail}


def _parse_kernel_lines(log: str) -> Dict[str, Any]:
    """Pure CPU: categorise pre-filtered kernel lines. Run OFF the event loop."""
    out: Dict[str, Any] = {"totals": {}, "by_bus": {}, "samples": []}
    for line in log.splitlines():
        # "hub" matters as much as "usb"/"hcd": the kernel logs over-current
        # against the HUB (e.g. "hub 2-0:1.0: over-current condition on port 3"),
        # which contains neither of the other two.
        _l = line.lower()
        if "usb" not in _l and "hcd" not in _l and "hub " not in _l:
            continue
        for cat, rx in _KERNEL_PATTERNS:
            if not rx.search(line):
                continue
            out["totals"][cat] = out["totals"].get(cat, 0) + 1
            bm = _KERNEL_BUS_RE.search(line)
            if bm:
                bus = bm.group(1)
                out["by_bus"].setdefault(bus, {})
                out["by_bus"][bus][cat] = out["by_bus"][bus].get(cat, 0) + 1
            if len(out["samples"]) < 25:
                out["samples"].append(line.strip()[:300])
            break
    return out


def _load_kernel_state() -> Dict[str, Any]:
    try:
        if os.path.exists(USB_KERNEL_FILE) and os.path.getsize(USB_KERNEL_FILE) > 0:
            with open(USB_KERNEL_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("usb_diagnostics: kernel state read failed: %s", e)
    return {}


def _save_kernel_state(d: Dict[str, Any]) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        tmp = f"{USB_KERNEL_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, USB_KERNEL_FILE)
    except OSError as e:
        logger.debug("usb_diagnostics: kernel state write failed: %s", e)


def _fold_kernel_delta(state: Dict[str, Any], parsed: Dict[str, Any],
                       now: float, window_s: int) -> Dict[str, Any]:
    """Merge one delta scan into hourly buckets and drop anything past *window_s*.

    Bucketing by hour is what makes the rolling window cheap AND bounded: at most
    24 small dicts, evicted by age, so the accumulator can never grow with log
    volume the way a raw event list would.
    """
    buckets = {k: v for k, v in (state.get("buckets") or {}).items()
               if str(k).isdigit() and (now - int(k)) <= window_s}
    hour = str(int(now // 3600) * 3600)
    b = buckets.setdefault(hour, {"totals": {}, "by_bus": {}})
    for cat, n in (parsed.get("totals") or {}).items():
        b["totals"][cat] = int(b["totals"].get(cat, 0)) + int(n)
    for bus, cats in (parsed.get("by_bus") or {}).items():
        dst = b["by_bus"].setdefault(bus, {})
        for cat, n in (cats or {}).items():
            dst[cat] = int(dst.get(cat, 0)) + int(n)
    # Newest samples win, capped — they are illustrative, not a log.
    samples = (parsed.get("samples") or []) + (state.get("samples") or [])
    return {"buckets": buckets, "samples": samples[:25], "last_scan_ts": now}


def _sum_kernel_buckets(state: Dict[str, Any]) -> Dict[str, Any]:
    totals: Dict[str, int] = {}
    by_bus: Dict[str, Dict[str, int]] = {}
    for b in (state.get("buckets") or {}).values():
        for cat, n in ((b or {}).get("totals") or {}).items():
            totals[cat] = totals.get(cat, 0) + int(n)
        for bus, cats in ((b or {}).get("by_bus") or {}).items():
            dst = by_bus.setdefault(bus, {})
            for cat, n in (cats or {}).items():
                dst[cat] = dst.get(cat, 0) + int(n)
    return {"totals": totals, "by_bus": by_bus}


async def kernel_usb_events(window_s: int = DIAG_KERNEL_WINDOW_S,
                            now: Optional[float] = None) -> Dict[str, Any]:
    """Categorised kernel USB events over a rolling *window_s*.

    Reads ONLY the journal since the last scan and folds it into hourly buckets,
    so a 24h view costs one ~5-minute read per pass. The original re-read the
    entire day every pass: 2.4 GB resident, ~1.7 cores, and the regex sweep ran
    on the event loop — which starved the systemd watchdog and got the agent
    SIGABRT'd every ~110s. Now bounded three ways: delta-only read, server-side
    ``--grep``, and a hard line cap; the parse runs in a worker thread.

    ``available`` false means the journal could not be read AT ALL — the UI must
    say "no data", not "no errors". ``covers_s`` is the span actually
    accumulated so far, which is < window_s until the buckets fill.
    """
    from . import pve_cmds
    now = time.time() if now is None else now
    state = await asyncio.to_thread(_load_kernel_state)
    last = state.get("last_scan_ts")
    # Delta since the last scan, clamped: never longer than the window (an agent
    # down for days must not ask for days), never shorter than a few seconds of
    # overlap so an event landing between passes is not missed.
    if isinstance(last, (int, float)) and 0 < last <= now:
        since = min(window_s, max(60.0, (now - last) + 10.0))
    else:
        since = min(window_s, float(KERNEL_SEED_WINDOW_S))   # cold start

    out: Dict[str, Any] = {"window_s": int(window_s), "totals": {}, "by_bus": {},
                           "samples": [], "available": False, "truncated": False,
                           "filtered": True, "scanned_s": int(since),
                           "covers_s": 0}
    base = ["journalctl", "-k", "--no-pager", "-o", "cat",
            "--since", f"-{int(since)}s", "-n", str(_JOURNAL_MAX_LINES)]
    try:
        rc, _log, _err = await pve_cmds._run(base + ["-g", _JOURNAL_GREP],
                                             check=False, timeout=30)
        if rc != 0:
            # Older systemd has no --grep. Fall back to the capped unfiltered
            # read — still bounded, so it cannot balloon the agent.
            logger.debug("usb_diagnostics: journalctl -g unsupported (%s) — "
                         "capped unfiltered read instead",
                         _text(_err).strip()[:120])
            rc, _log, _ = await pve_cmds._run(base, check=False, timeout=30)
            out["filtered"] = False
        log = _text(_log)
    except Exception as e:  # noqa: BLE001
        logger.debug("usb_diagnostics: journalctl failed: %s", e)
        return out
    if rc != 0:
        return out
    out["available"] = True
    parsed = {"totals": {}, "by_bus": {}, "samples": []}
    if log:
        out["truncated"] = (log.count("\n") + 1) >= _JOURNAL_MAX_LINES
        parsed = await asyncio.to_thread(_parse_kernel_lines, log)
    state = _fold_kernel_delta(state, parsed, now, window_s)
    await asyncio.to_thread(_save_kernel_state, state)
    out.update(_sum_kernel_buckets(state))
    # Split the totals by who caused them. Reported alongside, never subtracted
    # silently: a reset that provokes a genuine link error is still worth
    # seeing, and hiding events would trade one blind spot for another.
    try:
        out["attribution"] = attribute_kernel_events(out.get("by_bus") or {}, now=now)
    except Exception as e:  # noqa: BLE001
        logger.debug("usb_diagnostics: attribution failed: %s", e)
    out["samples"] = state.get("samples") or []
    _keys = [int(k) for k in (state.get("buckets") or {}) if str(k).isdigit()]
    out["covers_s"] = int(now - min(_keys)) if _keys else 0
    return out


# ── correlation ──────────────────────────────────────────────────────────────

def correlate(missing: List[Dict[str, Any]], kernel: Dict[str, Any],
              power: List[Dict[str, Any]], controllers: List[Dict[str, Any]],
              uhubctl: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rank probable causes for the missing dongles.

    Each entry is ``{cause, confidence, detail, evidence, remedy}``. Ordered
    most-confident first. Returns ``[]`` when nothing is missing — an empty list
    means "no problem", never "no idea"; the caller distinguishes those via
    ``kernel["available"]``.
    """
    if not missing:
        return []
    out: List[Dict[str, Any]] = []
    missing_buses = {m["bus_path"] for m in missing}
    by_bus = (kernel or {}).get("by_bus") or {}
    totals = (kernel or {}).get("totals") or {}

    # Evidence tied to the buses that actually went away is far stronger than
    # host-wide counts, so score those first.
    hit = {}
    for bus in missing_buses:
        for cat, n in (by_bus.get(bus) or {}).items():
            hit[cat] = hit.get(cat, 0) + n

    if hit.get("over_current"):
        out.append({
            "cause": "Port over-current",
            "confidence": "high",
            "detail": f"The kernel logged over-current on {hit['over_current']} "
                      "event(s) for the exact bus(es) that disappeared. The port "
                      "shut itself off to protect the controller.",
            "evidence": "kernel: over-current on the missing bus",
            "remedy": "Move these dongles to an externally POWERED hub — the port "
                      "cannot supply what they draw.",
        })
    if hit.get("insufficient_power"):
        out.append({
            "cause": "Bus power budget exhausted",
            "confidence": "high",
            "detail": f"{hit['insufficient_power']} rejection(s) for insufficient "
                      "available bus power on the missing bus(es). Too many "
                      "high-draw dongles on one root hub.",
            "evidence": "kernel: insufficient available bus power",
            "remedy": "Externally powered hub, or spread the dongles across "
                      "controllers.",
        })
    if hit.get("link_error"):
        out.append({
            "cause": "Link / enumeration errors",
            "confidence": "high",
            "detail": f"{hit['link_error']} link error(s) (-71 EPROTO / -110 "
                      "ETIMEDOUT / failed enumeration) on the missing bus(es) — "
                      "cabling, port, or a failing dongle.",
            "evidence": "kernel: descriptor/enumeration errors on the missing bus",
            "remedy": "Re-seat or replace the cable/dongle; if it repeats on the "
                      "same PORT with different dongles, the port is at fault.",
        })
    # Disconnect WITHOUT a link error. The kernel saw the device leave the bus
    # cleanly — no protocol fault, no enumeration retry, it simply stopped being
    # there. That is the electrical/PHY signature (VBUS drop, marginal cable or
    # port, a hub dropping a downstream port), NOT a software or driver fault.
    #
    # This branch was missing, and its absence was silent in the worst way:
    # `disconnect` counts landed in `hit`, which made the "no kernel evidence"
    # fallback below think evidence existed, so a host whose dongles ONLY logged
    # disconnects produced an EMPTY cause list. Three of four lab hosts showed
    # 23-140 disconnects and no probable cause at all.
    if hit.get("disconnect") and not hit.get("link_error"):
        _n = hit["disconnect"]
        out.append({
            "cause": "Clean disconnect (no link error)",
            "confidence": "high" if _n >= len(missing_buses) else "medium",
            "detail": f"{_n} USB disconnect event(s) on the missing bus(es) with "
                      "NO accompanying link error. The device left the bus "
                      "cleanly rather than failing to talk — that points at "
                      "power/PHY (VBUS drop, marginal cable or port, a hub "
                      "dropping its downstream port), not at the driver or the "
                      "USB stack.",
            "evidence": "kernel: USB disconnect on the missing bus, no -71/-110",
            "remedy": ("Power-cycle the port: uhubctl -a cycle -l <hub>"
                       if (uhubctl or {}).get("supported") else
                       "Re-seat the dongle / try a powered hub; no PPPS hub here, "
                       "so the fallback is an xHCI unbind/rebind") +
                      ". If it recurs on the SAME port with a different dongle, "
                      "the port or its power supply is at fault.",
        })
    if totals.get("controller_fault"):
        out.append({
            "cause": "USB controller fault",
            "confidence": "high" if len(missing_buses) > 2 else "medium",
            "detail": f"{totals['controller_fault']} controller-level fault(s) "
                      "(xHCI died/halted). Everything behind that controller "
                      "goes at once — this matches a whole-group disappearance.",
            "evidence": "kernel: xhci_hcd fault",
            "remedy": "Unbind/rebind the controller to re-enumerate without a "
                      "reboot: " + (", ".join(c["pci_address"] for c in
                                              (controllers or [])[:3]) or "see controllers"),
        })

    # Autosuspend: only meaningful if the dongles that are STILL here are set to
    # auto — we can't read the setting of a device that's gone, so this is
    # inference from its surviving siblings, hence never "high".
    auto = [p for p in (power or []) if p.get("autosuspend_enabled")]
    if auto:
        out.append({
            "cause": "USB autosuspend",
            "confidence": "medium",
            "detail": f"{len(auto)} of {len(power or [])} present device(s) can "
                      "actually suspend (power/control=auto AND a non-negative "
                      "autosuspend delay — control=auto with a negative delay "
                      "does NOT suspend). A "
                      "dongle whose firmware doesn't resume cleanly drops off the "
                      "bus and never returns — the classic slow multi-day decay. "
                      "Inferred from the surviving dongles (a device that's gone "
                      "can't be read).",
            "evidence": "sysfs: power/control=auto on present dongles",
            "remedy": "Set usbcore.autosuspend=-1 on the kernel cmdline, or a "
                      "udev rule pinning power/control=on for these vid:pids.",
        })

    # Nothing in the log about the buses that vanished. That absence is itself
    # the finding: a clean disappearance points at VBUS/PHY rather than a
    # protocol fault, which is exactly the power-cycle case.
    if not hit and not totals.get("controller_fault"):
        if not (kernel or {}).get("available"):
            out.append({
                "cause": "No kernel log available",
                "confidence": "unknown",
                "detail": "journalctl -k could not be read, so kernel evidence "
                          "could not be checked. This is NOT the same as a clean log.",
                "evidence": "journalctl unavailable",
                "remedy": "Check the agent can run journalctl -k on the host.",
            })
        else:
            out.append({
                "cause": "Silent disappearance (no kernel evidence)",
                "confidence": "medium",
                "detail": "The dongle(s) left with no error, over-current or "
                          "disconnect logged. That points at power/PHY rather than "
                          "a protocol fault — the device stopped answering without "
                          "the kernel noticing a failure.",
                "evidence": "clean kernel log for the missing bus(es)",
                "remedy": ("Power-cycle the ports: uhubctl -a cycle "
                           if (uhubctl or {}).get("supported") else
                           "No PPPS hub for uhubctl, so cut power by unbinding the "
                           "controller: "),
            })
    return out


async def collect(agent, present_by_bus: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Full diagnostic snapshot for the telemetry frame. Never raises."""
    try:
        from . import usb_provision
        if present_by_bus is None:
            present = await asyncio.to_thread(
                usb_provision.scan_present_dongles,
                usb_provision._dongle_vidpids(agent),
                usb_provision._certified_types(agent))
            present_by_bus = present or {}
        now = time.time()
        # Every sysfs walk + roster write below is SYNCHRONOUS file I/O. On a host
        # with many devices that is enough to stall the event loop, and the
        # systemd watchdog is fed from telemetry-tick completion — so blocking
        # here gets the agent SIGABRT'd. Offloaded to a worker thread.
        roster = await asyncio.to_thread(record_presence, present_by_bus, now)
        # Boot baseline FIRST — the authoritative "what should be here". Bus ids
        # are stable within a boot, so a diff against it is a real loss with none
        # of the roster's cross-reboot caveats (renumbering, controllers handed
        # to a VM, dongles physically moved between ports).
        baseline = await asyncio.to_thread(capture_boot_baseline, present_by_bus, now)
        controllers = await asyncio.to_thread(usb_controllers)
        # Controllers handed to a VM. Dongles behind them leave the host BY
        # DESIGN (T1/T3 PCI passthrough), so they must not be counted as losses.
        pt_addrs = {c["pci_address"] for c in controllers if c.get("passthrough")}
        missing_all = missing_dongles(roster, present_by_bus, now, pt_addrs)
        passed = [m for m in missing_all if m.get("passed_through")]
        _m_real = [m for m in missing_all if not m.get("passed_through")]
        missing = [m for m in _m_real if m.get("established")]
        missing_transient = [m for m in _m_real if not m.get("established")]
        lost_all = lost_since_boot(baseline, present_by_bus, pt_addrs, roster)
        # Split: a dongle absent because its controller was passed through to a
        # VM is exactly where it should be. Only the remainder is a real loss.
        _real = [m for m in lost_all if not m.get("passed_through")]
        lost_passthrough = [m for m in lost_all if m.get("passed_through")]
        # Only ESTABLISHED dongles (>= INVENTORY_MIN_AGE_S observed) count as
        # losses. A stick that was plugged in briefly and pulled is transient,
        # not missing inventory.
        lost = [m for m in _real if m.get("established")]
        lost_transient = [m for m in _real if not m.get("established")]
        kernel = await kernel_usb_events()
        power = await asyncio.to_thread(usb_power_settings)
        uhub = await uhubctl_support()
        return {
            "generated_at": now,
            "present_count": len(present_by_bus or {}),
            "known_count": len(roster or {}),
            "missing": missing,
            # Absent-but-EXPECTED: their controller is passed through to a VM.
            # Reported separately so the headline "missing" count means "lost"
            # and nothing else — ~4 per host live here permanently.
            "passed_through": passed,
            "passthrough_controllers": sorted(pt_addrs),
            # THE headline number: present at boot, gone now. Unambiguous.
            "lost_since_boot": lost,
            "boot_passthrough": lost_passthrough,
            # Seen too briefly to be inventory — surfaced, never counted.
            "lost_transient": lost_transient,
            "missing_transient": missing_transient,
            "inventory_min_age_s": INVENTORY_MIN_AGE_S,
            "boot_baseline": {k: v for k, v in (baseline or {}).items() if k != "buses"},
            "kernel": kernel,
            "power": power,
            "controllers": controllers,
            "uhubctl": uhub,
            # Hub tier: what hangs off each hub, whether it can cut per-port
            # power, and whether it is over the tested 7-dongle ceiling or
            # bus-powered. Empty on a host with no external hub -- which is the
            # current fleet, so this ships dark until one is fitted.
            "hubs": hub_topology((uhub or {}).get("ppps_hubs")),
            "hub_dongle_ceiling": HUB_DONGLE_CEILING,
            # Correlate against the BOOT-derived losses when the baseline is a
            # true boot snapshot — that set is exactly "what should be here and
            # is not". Fall back to the roster when the baseline was taken
            # mid-boot (agent installed/restarted late), where it is only a
            # sample and the roster is the better evidence.
            "causes": correlate(lost if baseline.get("trusted") else missing,
                                kernel, power, controllers, uhub),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("usb_diagnostics.collect failed: %s", e)
        return {}
