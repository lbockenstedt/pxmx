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

# How far back the diagnostic kernel scan looks. The quarantine scanner uses
# 180s (it must react fast); a dongle count that decays over days needs a window
# wide enough to hold the event that removed one. journalctl reads a compressed
# on-disk journal, so this stays cheap.
DIAG_KERNEL_WINDOW_S = 86400

# A roster entry not seen for this long is dropped — a dongle genuinely removed
# from the host shouldn't be reported "missing" forever.
ROSTER_TTL_S = 30 * 86400

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


# ── presence roster ──────────────────────────────────────────────────────────

def _load_roster() -> Dict[str, Any]:
    try:
        if os.path.exists(USB_PRESENCE_FILE) and os.path.getsize(USB_PRESENCE_FILE) > 0:
            with open(USB_PRESENCE_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("usb_diagnostics: roster read failed: %s", e)
    return {}


def _save_roster(d: Dict[str, Any]) -> None:
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
    for bus, info in (present_by_bus or {}).items():
        e = roster.get(bus) or {}
        e["vidpid"] = (info or {}).get("vidpid") or e.get("vidpid") or ""
        e["product"] = (info or {}).get("product") or e.get("product") or ""
        e["type"] = (info or {}).get("type") or e.get("type") or ""
        e.setdefault("first_seen", now)
        e["last_seen"] = now
        e["missing_since"] = None
        roster[bus] = e
    # Stamp the moment each known-but-absent bus went away, and expire entries
    # whose dongle has been gone long enough to count as removed, not lost.
    for bus in list(roster):
        e = roster.get(bus) or {}
        if bus in (present_by_bus or {}):
            continue
        if e.get("missing_since") is None:
            e["missing_since"] = now
        if now - float(e.get("last_seen") or now) > ROSTER_TTL_S:
            roster.pop(bus, None)
            continue
        roster[bus] = e
    _save_roster(roster)
    return roster


def missing_dongles(roster: Dict[str, Any], present_by_bus: Dict[str, Any],
                    now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Roster entries whose bus is no longer on the host, newest loss first."""
    now = time.time() if now is None else now
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


def usb_controllers() -> List[Dict[str, Any]]:
    """xHCI/EHCI controllers with the count of USB devices currently under each.

    The PCI address is what an operator would unbind/rebind to force a full
    re-enumeration (the closest no-reboot equivalent of a power cycle), so it is
    surfaced verbatim. The device count is what makes a controller-wide loss
    visible: all dongles vanishing off ONE controller reads very differently
    from losses scattered across several.
    """
    out: List[Dict[str, Any]] = []
    for drv in ("xhci_hcd", "ehci-pci", "ehci_hcd"):
        for path in sorted(glob.glob(f"/sys/bus/pci/drivers/{drv}/0000:*")):
            addr = os.path.basename(path)
            buses, devices = [], 0
            for usbdir in sorted(glob.glob(os.path.join(path, "usb*"))):
                buses.append(os.path.basename(usbdir))
                # Every non-interface child under this root hub is a device.
                devices += len([d for d in glob.glob(os.path.join(usbdir, "*-*"))
                                if ":" not in os.path.basename(d)])
            out.append({"driver": drv, "pci_address": addr,
                        "root_hubs": buses, "device_count": devices})
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


async def kernel_usb_events(window_s: int = DIAG_KERNEL_WINDOW_S) -> Dict[str, Any]:
    """Categorised kernel USB events over *window_s*, totals + per-bus.

    Returns ``{"window_s", "totals": {cat: n}, "by_bus": {bus: {cat: n}},
    "samples": [recent raw lines], "available": bool}``. ``available`` false
    means the journal could not be read at all — the UI must then say "no data"
    rather than "no errors", which are very different diagnoses.
    """
    from . import pve_cmds
    out: Dict[str, Any] = {"window_s": int(window_s), "totals": {},
                           "by_bus": {}, "samples": [], "available": False}
    try:
        rc, _log, _ = await pve_cmds._run(
            ["journalctl", "-k", "--no-pager", "-o", "cat",
             "--since", f"-{int(window_s)}s"], check=False, timeout=30)
        log = _text(_log)
    except Exception as e:  # noqa: BLE001
        logger.debug("usb_diagnostics: journalctl failed: %s", e)
        return out
    if rc != 0 or not log:
        return out
    out["available"] = True
    for line in log.splitlines():
        # "hub" matters as much as "usb"/"hcd": the kernel logs over-current
        # against the HUB, e.g. "hub 2-0:1.0: over-current condition on port 3",
        # which contains neither of the other two — so the single most
        # actionable cause (a port shutting itself off) was being filtered out
        # before it ever reached the pattern list. Caught by a test feeding a
        # real over-current line.
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
            present = usb_provision.scan_present_dongles(
                usb_provision._dongle_vidpids(agent),
                usb_provision._certified_types(agent))
            present_by_bus = present or {}
        now = time.time()
        roster = record_presence(present_by_bus, now)
        missing = missing_dongles(roster, present_by_bus, now)
        kernel = await kernel_usb_events()
        power = usb_power_settings()
        controllers = usb_controllers()
        uhub = await uhubctl_support()
        return {
            "generated_at": now,
            "present_count": len(present_by_bus or {}),
            "known_count": len(roster or {}),
            "missing": missing,
            "kernel": kernel,
            "power": power,
            "controllers": controllers,
            "uhubctl": uhub,
            "causes": correlate(missing, kernel, power, controllers, uhub),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("usb_diagnostics.collect failed: %s", e)
        return {}
