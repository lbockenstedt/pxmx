"""Client-Simulation watchdogs for the unified pxmx agent.

Ports the two watchdogs from ``cs/proxmox/proxmox-agent.sh``:

1. **Hardware watchdog** (bash ``hw_watchdog_loop``/``hw_watchdog_check``/
   ``hard_reset``, lines 3658-3852) — scans the kernel journal via a saved
   ``journalctl`` cursor for Tier-1 (fatal) and Tier-2 (accumulating) hardware
   fault patterns, records them, and triggers a host hard reset (IPMI → sysrq
   → ``reboot -f``) on a match, with a cooldown. Emits ``CS_HW_RESET_EVENT``
   up to the spoke immediately before the reset.

2. **Guest-agent watchdog** (bash ``_run_vm_agent_watchdog``, lines 2279-2381)
   — per sim-VM: pings the QEMU guest agent, and escalates an unresponsive VM
   warn → soft reboot (``qm reboot``) → reclone. Emits ``CS_WATCHDOG_EVENT`` on
   each transition. The reclone step destroys the VM, which belongs to Phase E
   (``destroy_vm``); until then the reclone threshold is reached is emitted +
   logged and the actual destroy is deferred (no destroy_vm exists yet).

All thresholds default to the bash defaults and are overridable from the hub
via ``client_simulation.watchdog`` (the AGNT-row equivalent) and env vars.
"""

# Standard-library helpers: async loops, JSON state files, logging, paths and timestamps.
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

# pve_cmds wraps Proxmox CLI calls (qm, journalctl) with timeouts.
from . import pve_cmds

# Share the main agent logger so watchdog messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# ── Paths (unified agent uses /var/lib/pxmx; the bash agent used
#    /var/lib/client-sim — Phase G migrates) ────────────────────────────────
# State files for the journal cursor, the fault history, the last reset, and per-VM watchdog state.
PXMLIB = "/var/lib/pxmx"
HW_CURSOR = f"{PXMLIB}/hw-watchdog-cursor"
HW_FAULT_LOG = f"{PXMLIB}/hw-faults.json"
HW_RESET_RECORD = f"{PXMLIB}/hw-last-reset.json"
VM_WD_STATE = f"{PXMLIB}/vm_agent_watchdog.json"

# ── Defaults (match cs bash lines 58-77) ───────────────────────────────────
# Hardware watchdog defaults: scan interval, on/off, Tier-2 hit threshold, reset cooldown.
DEF_HW_INTERVAL = 60
DEF_HW_ENABLED = True
DEF_HW_TIER2_THRESHOLD = 3
DEF_HW_REBOOT_COOLDOWN = 300
# Whether the watchdogs are allowed to reboot anything (otherwise they only report).
DEF_WATCHDOG_REBOOT_ENABLED = True

# Guest-agent watchdog defaults, in minutes.
DEF_GA_ENABLED = True
DEF_GA_GRACE_MIN = 20
DEF_GA_CHECK_INTERVAL_MIN = 10
DEF_GA_REBOOT_AFTER_MIN = 10
DEF_GA_RECLONE_AFTER_MIN = 30

# Command timeouts (seconds) and how many hardware faults to keep in the history file.
PING_TIMEOUT = 5          # `timeout 5 qm agent <vmid> ping`
QM_REBOOT_TIMEOUT = 30    # `qm reboot --timeout 30`
FAULT_RING = 100          # keep last 100 hw faults

# ── Kernel-journal fault patterns (verbatim from bash lines 3716-3766) ──────
# Tier-1 patterns: a single match means the host is badly broken and should be reset.
TIER1_PATTERNS: List[str] = [
    r"Kernel panic", r"kernel BUG at", r"BUG: unable to handle kernel",
    r"Oops: general protection", r"RIP:.*Oops", r"double fault",
    r"machine check exception", r"nvme.*controller is down", r"nvme.*failed state",
    r"nvme.*Abort status.*DNR", r"nvme.*reset: controller failed",
    r"ata.*SRST failed.*error=-19", r"ata.*hard reset failed",
    r"ata.*failed to recover some devices", r"EXT4-fs error.*aborting journal",
    r"EXT4-fs.*remounting filesystem read-only",
    r"XFS.*log I/O error.*shutting down filesystem",
    r"XFS.*metadata I/O error.*shutting down",
    r"BTRFS.*error.*transaction abort", r"pcieport.*PCIe Bus Error.*severity=Fatal",
    r"AER.*Uncorrected.*Fatal", r"EDAC.*UE.*uncorrected error",
    r"Hardware Error.*severity.*Fatal", r"MCE.*Hardware Error.*fatal",
]

# Tier-2 patterns: warning signs that only trigger a reset once enough hits pile up.
TIER2_PATTERNS: List[str] = [
    r"nvme.*I/O.*timeout", r"nvme.*Abort command", r"ata.*exception Emask",
    r"ata.*timeout waiting for", r"blk_update_request.*I/O error",
    r"I/O error.*dev.*sector", r"scsi.*timing out command",
    r"sd.*Result: hostbyte=DID_TIMEOUT", r"SCSI error.*sense key.*HARDWARE ERROR",
    r"SCSI error.*sense key.*MEDIUM ERROR", r"ata.*soft resetting link",
    r"ata.*hard resetting link", r"task.*blocked for more than.*seconds",
    r"hung_task.*blocked", r"EDAC.*CE.*memory error", r"MCE.*corrected error",
    r"xhci_hcd.*died", r"ehci_hcd.*died", r"usb.*hub.*unable to enumerate",
    r"pcieport.*PCIe Bus Error.*severity=Corrected", r"Out of memory.*Kill process",
    r"oom.*killed process",
]


def _cfg(agent) -> Dict[str, Any]:
    """Return the ``client_simulation.watchdog`` config section (empty dict if missing)."""
    # Missing sections become empty dicts so callers can call .get() safely.
    return (agent.config.get("client_simulation") or {}).get("watchdog") or {}


def _usb_cfg(agent) -> Dict[str, Any]:
    """Return the hub-managed ``client_simulation.usb_config`` blob (empty dict if missing)."""
    # The cs spoke is the source of truth for the hub-managed watchdog knobs
    # (guest_agent_* / watchdog_reboot_enabled): it emits them in the
    # ``usb_config`` blob the cs_bridge relays into client_simulation.usb_config.
    # The watchdog section (_cfg) + env vars remain as fallbacks/overrides.
    return (agent.config.get("client_simulation") or {}).get("usb_config") or {}


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable; anything except 0/off/false/no counts as true."""
    # Unset means use the default.
    v = os.environ.get(name)
    if v is None:
        return default
    # Only explicit "off" spellings disable the setting.
    return v.strip().lower() not in ("0", "off", "false", "no")


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable, returning ``default`` when unset or invalid."""
    # Unset means use the default.
    v = os.environ.get(name)
    if v is None:
        return default
    # A value that isn't a number also falls back to the default.
    try:
        return int(v)
    except ValueError:
        return default


def _usb_toggle(val: Any, fallback: bool) -> bool:
    """Resolve a cs-speak (usb_config) on/off value, falling back if absent.

    The cs speak emits these via ``_normalize_toggle`` (``"on"``/``"off"``); a
    bool or ``"1"/"true"`` is also accepted. ``None``/missing → ``fallback``.
    """
    # Missing value: use the fallback.
    if val is None:
        return fallback
    # Real booleans pass straight through.
    if isinstance(val, bool):
        return val
    # Strings: only the usual "on" spellings count as true.
    return str(val).strip().lower() in ("1", "on", "true", "yes", "enabled")


def _usb_int(val: Any, fallback: int) -> int:
    """Resolve a cs-speak (usb_config) integer, falling back if absent/bad."""
    # Missing value: use the fallback.
    if val is None:
        return fallback
    # Parse the value, falling back when it isn't a valid number.
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return fallback


def _hw_settings(agent) -> Dict[str, Any]:
    """Resolve the hardware watchdog settings (environment beats hub config, which beats defaults).
    """
    # Hardware section of the watchdog config.
    c = _cfg(agent).get("hardware") or {}
    # watchdog_reboot_enabled is hub-owned (cs speak → usb_config); the other
    # hardware knobs (enabled/interval/tier2/cooldown) are env/watchdog-section
    # only — the original never hub-managed them.
    usb = _usb_cfg(agent)
    reboot_enabled = _usb_toggle(usb.get("watchdog_reboot_enabled"),
                                 bool(c.get("reboot_enabled", DEF_WATCHDOG_REBOOT_ENABLED)))
    # Each setting: an environment variable wins, then the config value, then the built-in default.
    return {
        "enabled": _env_bool("CLIENT_SIM_HW_WATCHDOG_ENABLED",
                              bool(c.get("enabled", DEF_HW_ENABLED))),
        "interval": _env_int("CLIENT_SIM_HW_WATCHDOG_INTERVAL",
                             int(c.get("interval", DEF_HW_INTERVAL))),
        "tier2_threshold": _env_int("CLIENT_SIM_HW_TIER2_THRESHOLD",
                                    int(c.get("tier2_threshold", DEF_HW_TIER2_THRESHOLD))),
        "cooldown": _env_int("CLIENT_SIM_HW_REBOOT_COOLDOWN",
                             int(c.get("reboot_cooldown", DEF_HW_REBOOT_COOLDOWN))),
        "reboot_enabled": _env_bool("CLIENT_SIM_WATCHDOG_REBOOT_ENABLED",
                                    reboot_enabled),
    }


def _ga_settings(agent) -> Dict[str, Any]:
    """Resolve the guest-agent watchdog settings; minute values are converted to seconds."""
    # All guest-agent watchdog knobs are hub-owned (cs speak → usb_config), with
    # the watchdog section + env as fallbacks/overrides — mirroring the legacy
    # spoke where the settings store was the source. Env still wins so ops can
    # force a value; when env is unset the hub-managed value takes effect.
    usb = _usb_cfg(agent)
    c = _cfg(agent).get("guest_agent") or {}
    # Hub value first, then the watchdog section, then the built-in default.
    enabled = _usb_toggle(usb.get("guest_agent_watchdog_enabled"),
                          bool(c.get("enabled", DEF_GA_ENABLED)))
    check = _usb_int(usb.get("guest_agent_check_interval_minutes"),
                     int(c.get("check_interval_min", DEF_GA_CHECK_INTERVAL_MIN)))
    grace = _usb_int(usb.get("guest_agent_grace_minutes"),
                     int(c.get("grace_min", DEF_GA_GRACE_MIN)))
    # Escalation thresholds: when to soft-reboot and when to reclone.
    reboot_after = _usb_int(usb.get("guest_agent_reboot_after_minutes"),
                            int(c.get("reboot_after_min", DEF_GA_REBOOT_AFTER_MIN)))
    reclone_after = _usb_int(usb.get("guest_agent_reclone_after_minutes"),
                             int(c.get("reclone_after_min", DEF_GA_RECLONE_AFTER_MIN)))
    # Shared reboot kill-switch.
    reboot_enabled = _usb_toggle(usb.get("watchdog_reboot_enabled"),
                                bool(c.get("reboot_enabled", DEF_WATCHDOG_REBOOT_ENABLED)))
    # Environment overrides win; interval settings are converted from minutes to seconds.
    return {
        "enabled": _env_bool("CLIENT_SIM_GUEST_AGENT_WATCHDOG_ENABLED", enabled),
        "check_interval": _env_int("CLIENT_SIM_GUEST_AGENT_CHECK_INTERVAL_MINUTES", check) * 60,
        "grace": _env_int("CLIENT_SIM_GUEST_AGENT_GRACE_MINUTES", grace) * 60,
        "reboot_after": _env_int("CLIENT_SIM_GUEST_AGENT_REBOOT_AFTER_MINUTES", reboot_after) * 60,
        "reclone_after": _env_int("CLIENT_SIM_GUEST_AGENT_RECLONE_AFTER_MINUTES", reclone_after) * 60,
        "reboot_enabled": _env_bool("CLIENT_SIM_WATCHDOG_REBOOT_ENABLED", reboot_enabled),
    }


# ── Small JSON helpers ─────────────────────────────────────────────────────

def _ensure_libdir() -> None:
    """Create the state directory if needed (logs a warning instead of raising)."""
    try:
        os.makedirs(PXMLIB, exist_ok=True)
    except OSError as e:
        logger.warning(f"watchdog: cannot create {PXMLIB}: {e}")


def _read_json(path: str, default: Any) -> Any:
    """Load JSON from ``path``, returning ``default`` if it's missing, empty or corrupt."""
    # Only read files that exist and have content.
    try:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path) as f:
                return json.load(f)
    # Unreadable or corrupt files are treated as missing.
    except (OSError, json.JSONDecodeError):
        pass
    return default


def _write_json(path: str, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` (logs a warning instead of raising)."""
    # Make sure the directory exists, then overwrite the file.
    try:
        _ensure_libdir()
        with open(path, "w") as f:
            json.dump(data, f)
    # Disk errors are logged so the watchdog keeps running.
    except OSError as e:
        logger.warning(f"watchdog: cannot write {path}: {e}")


# ── Hardware watchdog ──────────────────────────────────────────────────────

async def _journalctl(*args: str, timeout: int = 15) -> str:
    """Run journalctl with ``args`` and return its stdout as text."""
    rc, out, err = await pve_cmds._run(["journalctl", *args], check=False, timeout=timeout)
    return out.decode(errors="replace")


def _init_cursor() -> Optional[str]:
    """Initialize the journal cursor to the current end-of-log (bash 3842-3847)."""
    # Synchronous subprocess call; this runs once at startup.
    import subprocess
    try:
        # Ask journalctl for the cursor at the current end of the kernel log.
        r = subprocess.run(["journalctl", "-k", "-n", "0", "--show-cursor"],
                           capture_output=True, text=True, timeout=15)
        # Save the cursor so the first scan starts from now instead of replaying old history.
        for line in r.stdout.splitlines():
            if line.startswith("-- cursor:"):
                cur = line.split(":", 1)[1].strip()
                _write_json(HW_CURSOR, {"cursor": cur})
                return cur
    # Without a cursor, the next check simply starts fresh.
    except Exception as e:
        logger.warning(f"hw_watchdog: cursor init failed: {e}")
    return None


def _record_hw_fault(tier: str, pattern: str, detail: str) -> None:
    """Append a hardware fault to the history file, keeping only the newest ``FAULT_RING`` entries.
    """
    # Load the existing fault list, tolerating a missing or malformed file.
    data = _read_json(HW_FAULT_LOG, {"faults": []})
    faults = data.get("faults") if isinstance(data, dict) else []
    if not isinstance(faults, list):
        faults = []
    # Add this fault, trim to the ring size, and save.
    faults.append({"ts": time.time(), "tier": tier, "pattern": pattern, "detail": detail})
    data = {"faults": faults[-FAULT_RING:], "last_updated": time.time()}
    _write_json(HW_FAULT_LOG, data)


def _reboot_cooled_down(cooldown: int) -> bool:
    """Return True if enough time has passed since the last hardware reset to allow another."""
    # No reset on record: a reset is allowed.
    rec = _read_json(HW_RESET_RECORD, None)
    if not rec:
        return True
    # Compare the age of the last reset with the cooldown; a bad record allows a reset.
    try:
        return (time.time() - float(rec.get("ts", 0))) >= cooldown
    except (TypeError, ValueError):
        return True


async def hard_reset(agent, reason: str) -> None:
    """Write the reset record, emit CS_HW_RESET_EVENT up, then reset the host.

    Order: IPMI chassis power reset → sysrq trigger → reboot -f (bash 3658-3711).
    """
    # Record the reset first so the cooldown applies after the reboot.
    from .agent import get_version
    _write_json(HW_RESET_RECORD,
                {"ts": time.time(), "reason": reason, "agent_version": get_version()})
    # Tell the spoke/hub why the host is about to go down.
    payload = {"hostname": agent.hostname, "reason": reason, "tier": "watchdog",
               "ts": time.time(), "agent_version": get_version()}
    try:
        await agent.send_cs_event("CS_HW_RESET_EVENT", payload)
    except Exception:
        pass  # best-effort — the reset must proceed even if the event can't be sent

    # Log, then try each reset method in turn until one works.
    logger.error(f"hard_reset: {reason}")
    # Method 1 — IPMI
    if os.path.isfile("/usr/bin/ipmitool") or os.path.isfile("/usr/sbin/ipmitool"):
        # Power-cycle via the BMC, then wait for it to take effect.
        try:
            await pve_cmds._run(["ipmitool", "chassis", "power", "reset"], check=False, timeout=15)
            await asyncio.sleep(30)
            return
        except Exception as e:
            logger.warning(f"hard_reset: ipmitool failed: {e}")
    # Method 2 — sysrq
    # Enable sysrq, flush disks, then trigger an immediate reboot.
    try:
        with open("/proc/sys/kernel/sysrq", "w") as f:
            f.write("1")
        os.sync()
        with open("/proc/sysrq-trigger", "w") as f:
            f.write("b")
        await asyncio.sleep(5)
    except Exception as e:
        logger.warning(f"hard_reset: sysrq failed: {e}")
    # Method 3 — last resort
    try:
        await pve_cmds._run(["reboot", "-f"], check=False, timeout=10)
    except Exception:
        pass


async def _hw_check(agent, settings: Dict[str, Any]) -> None:
    """One hardware-watchdog pass: read new kernel log lines and act on fault patterns."""
    # subprocess reads the cursor synchronously.
    import subprocess
    # No journalctl means nothing to scan.
    if not shutil_which("journalctl"):
        return
    # Build the NEW cursor first (captures current end-of-log before reading delta).
    try:
        r = subprocess.run(["journalctl", "-k", "-n", "0", "--show-cursor"],
                           capture_output=True, text=True, timeout=15)
        new_cursor = None
        for line in r.stdout.splitlines():
            if line.startswith("-- cursor:"):
                new_cursor = line.split(":", 1)[1].strip()
                break
    except Exception as e:
        logger.warning(f"hw_watchdog: cursor read failed: {e}")
        return

    # Load the cursor saved by the previous pass.
    saved = None
    cur_blob = _read_json(HW_CURSOR, {})
    if isinstance(cur_blob, dict):
        saved = cur_blob.get("cursor")
    # Save the new cursor so the next pass only reads lines added after this one.
    if new_cursor:
        _write_json(HW_CURSOR, {"cursor": new_cursor})
    if not saved:
        return  # first run — nothing to scan yet

    # Read kernel messages since the saved cursor.
    cursor_arg = f"--cursor={saved}"
    try:
        msgs = await _journalctl("-k", "--no-pager", "-o", "short-monotonic", cursor_arg)
    # A read failure skips this pass.
    except Exception:
        return
    # No new messages: nothing to do.
    if not msgs.strip():
        return

    # Tier-1: first hit wins.
    t1_matched = None
    for pat in TIER1_PATTERNS:
        hit = _first_match(msgs, pat)
        if hit:
            t1_matched = pat
            logger.error(f"hw_watchdog: Tier-1 fault '{pat}': {hit[:200]}")
            _record_hw_fault("tier1", pat, hit[:300])
            break
    # Tier-1 hit: reset if the cooldown has passed and resets are enabled, otherwise just log.
    if t1_matched:
        if _reboot_cooled_down(settings["cooldown"]):
            if settings["reboot_enabled"]:
                await hard_reset(agent, f"Tier-1 hardware fault: {t1_matched}")
            else:
                logger.warning("hw_watchdog: Tier-1 hit — reporting only (reboot disabled)")
        else:
            logger.warning("hw_watchdog: Tier-1 hit — reset skipped (cooldown active)")
        return

    # Tier-2: accumulate counts across patterns.
    t2_count = 0
    t2_reasons: List[str] = []
    for pat in TIER2_PATTERNS:
        count = _count_matches(msgs, pat)
        if count:
            t2_count += count
            t2_reasons.append(f"{pat}({count})")
            _record_hw_fault("tier2", pat, f"count={count}")
    # Enough Tier-2 hits in this window: reset under the same cooldown and kill-switch rules.
    if t2_count >= settings["tier2_threshold"]:
        reason = f"Tier-2 hardware faults ({t2_count} hits): {', '.join(t2_reasons)}"
        logger.error(f"hw_watchdog: {reason}")
        if _reboot_cooled_down(settings["cooldown"]):
            if settings["reboot_enabled"]:
                await hard_reset(agent, reason)
            else:
                logger.warning("hw_watchdog: Tier-2 threshold — reporting only (reboot disabled)")
        else:
            logger.warning("hw_watchdog: Tier-2 threshold — reset skipped (cooldown active)")


def _first_match(text: str, pattern: str) -> Optional[str]:
    """Return the first case-insensitive match of ``pattern`` in ``text``, or None."""
    # Search case-insensitively and return the matched text.
    import re
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(0) if m else None


def _count_matches(text: str, pattern: str) -> int:
    """Count case-insensitive matches of ``pattern`` in ``text``."""
    # findall returns every non-overlapping match.
    import re
    return len(re.findall(pattern, text, re.IGNORECASE))


def shutil_which(name: str) -> Optional[str]:
    """Find an executable on PATH, like ``shutil.which`` (returns None if not found)."""
    # Check each PATH directory for an executable file with this name.
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


async def hw_watchdog_loop(agent) -> None:
    """Background task: scan the kernel journal every `interval` seconds."""
    # Load settings once; a disabled watchdog exits immediately.
    settings = _hw_settings(agent)
    if not settings["enabled"]:
        logger.info("hw_watchdog: disabled")
        return
    # Without journalctl there is nothing to watch.
    if not shutil_which("journalctl"):
        logger.warning("hw_watchdog: journalctl not found — loop idle")
        return
    # Start from the current end of the log and log the settings in use.
    _init_cursor()
    logger.info(f"hw_watchdog: started (interval={settings['interval']}s, "
                f"tier2>={settings['tier2_threshold']}, cooldown={settings['cooldown']}s)")
    # Check forever; shutdown (cancellation) propagates, other errors are logged and retried.
    while True:
        try:
            await _hw_check(agent, settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"hw_watchdog: check error: {e}")
        # Wait before the next scan.
        await asyncio.sleep(settings["interval"])


# ── Guest-agent watchdog ───────────────────────────────────────────────────

async def _qm_status_running(vmid: int) -> bool:
    """Return True if the VM is currently running (False on any error)."""
    # Ask Proxmox for the VM status; errors are treated as not running.
    try:
        st = await pve_cmds.vm_status(vmid)
        return bool(st.get("running"))
    except Exception:
        return False


async def _qm_agent_ping(vmid: int) -> bool:
    """`timeout 5 qm agent <vmid> ping` → True if rc 0."""
    # Ping the guest agent; a timeout or error counts as no response.
    try:
        rc, out, err = await pve_cmds._run(
            ["qm", "agent", str(vmid), "ping"], check=False, timeout=PING_TIMEOUT)
        return rc == 0
    except Exception:
        return False


async def vm_agent_watchdog_loop(agent) -> None:
    """Background task: every `check_interval`, ping each sim VM's guest agent
    and escalate unresponsive VMs warn → soft reboot → (reclone, Phase E)."""
    # Load settings once; a disabled watchdog exits immediately.
    settings = _ga_settings(agent)
    if not settings["enabled"]:
        logger.info("vm_agent_watchdog: disabled")
        return
    # Log the thresholds in use.
    logger.info(f"vm_agent_watchdog: started (check={settings['check_interval']}s, "
                f"grace={settings['grace']}s, reboot_after={settings['reboot_after']}s, "
                f"reclone_after={settings['reclone_after']}s)")
    # Scan forever; shutdown (cancellation) propagates, other errors are logged and retried.
    while True:
        try:
            await _ga_scan(agent, settings)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"vm_agent_watchdog: scan error: {e}")
        # Wait before the next scan.
        await asyncio.sleep(settings["check_interval"])


async def _ga_scan(agent, settings: Dict[str, Any]) -> None:
    """One guest-agent pass: ping every sim VM and step through warn → reboot → reclone."""
    # Lazy imports avoid a circular import at module load time.
    from .cs_guard import SIM_VMIN
    from . import usb_provision
    # Per-VM watchdog state from previous passes.
    state = _read_json(VM_WD_STATE, {})
    if not isinstance(state, dict):
        state = {}
    # Shared timestamp for this pass, plus a flag for whether the state needs saving.
    now = time.time()
    mutated = False
    # Protected VMs are never touched by the watchdog.
    try:
        protected = usb_provision._protected_vmids(agent)
    except Exception:
        protected = set()
    # VMs that are being deleted, recloned or provisioned are expected to be unresponsive; skip
    # them.
    in_flight = set()
    try:
        in_flight |= {int(v) for v in (usb_provision.current_deleting_vmids() or [])}
        in_flight |= {int(v) for v in (usb_provision.current_reclone_vmids() or [])}
        # Include every VM in a running provisioning batch.
        run = usb_provision.current_prov_run() or {}
        if run.get("running"):
            for it in (run.get("items") or []):
                try:
                    in_flight.add(int((it or {}).get("vmid")))
                except (TypeError, ValueError):
                    continue
    # If the lookup fails, carry on without the in-flight list.
    except Exception as e:
        logger.debug("vm_agent_watchdog: in-flight lookup failed: %s", e)

    # Every sim VM on this host (VMID at or above the sim range start).
    vmids = [v for v in await pve_cmds.list_qemu_vmids() if v >= SIM_VMIN]
    for vmid in vmids:
        # Skip protected and busy VMs.
        if vmid in protected or vmid in in_flight:
            continue
        # Templates have no running guest agent; skip them.
        try:
            if await pve_cmds.is_template(vmid):
                continue
        except Exception:
            pass
        # Earlier state for this VM: when it first failed and when we rebooted it.
        entry = state.get(str(vmid)) or {}
        first_fail = entry.get("first_fail", 0)
        rebooted_at = entry.get("rebooted_at", 0)

        # Post-reboot reclone deadline (runs regardless of running state).
        if rebooted_at and (now - rebooted_at) >= settings["reclone_after"]:
            logger.warning(f"vm_agent_watchdog: VM {vmid} unresponsive "
                           f">{settings['reclone_after']}s since reboot — reclone threshold reached")
            await agent.send_cs_event("CS_WATCHDOG_EVENT", {
                "hostname": agent.hostname, "vmid": vmid,
                "action": "reclone_threshold_reached", "ts": now})
            # The actual destroy+reclone needs Phase E's destroy_vm; clear the
            # state so it doesn't re-fire every cycle and defer the reclone.
            state.pop(str(vmid), None)
            mutated = True
            continue

        # Only ping running VMs.
        if not await _qm_status_running(vmid):
            continue  # only ping running VMs

        # Responding: clear any previous failure state and move on.
        if await _qm_agent_ping(vmid):
            if entry:  # recovered
                logger.info(f"vm_agent_watchdog: VM {vmid} guest agent recovered")
                state.pop(str(vmid), None)
                mutated = True
            continue

        # Unresponsive.
        # First failure: start the clock and tell the hub.
        if not first_fail:
            state[str(vmid)] = {"first_fail": now, "last_check": now, "rebooted_at": 0}
            mutated = True
            logger.warning(f"vm_agent_watchdog: VM {vmid} agent not responding — "
                           f"monitoring started (reboot_after={settings['reboot_after']}s, "
                           f"reclone_after={settings['reclone_after']}s)")
            await agent.send_cs_event("CS_WATCHDOG_EVENT", {
                "hostname": agent.hostname, "vmid": vmid,
                "action": "unresponsive", "ts": now})
            continue

        # Still failing: update the state with how long it has been unresponsive.
        unresponsive_s = now - first_fail
        state[str(vmid)] = {"first_fail": first_fail, "last_check": now, "rebooted_at": rebooted_at}
        mutated = True
        # Unresponsive too long and not yet rebooted: soft-reboot it (if allowed).
        if rebooted_at == 0 and unresponsive_s >= settings["reboot_after"]:
            if settings["reboot_enabled"]:
                logger.warning(f"vm_agent_watchdog: VM {vmid} unresponsive "
                               f">{settings['reboot_after']}s — issuing soft reboot")
                try:
                    # Reboot through qm; a failure is logged and still counts as an attempt.
                    await pve_cmds._run(["qm", "reboot", str(vmid), "--timeout", str(QM_REBOOT_TIMEOUT)],
                                        check=False, timeout=QM_REBOOT_TIMEOUT + 5)
                except Exception as e:
                    logger.warning(f"vm_agent_watchdog: qm reboot {vmid} failed: {e}")
                # Record the reboot time (starts the reclone countdown) and tell the hub.
                state[str(vmid)] = {"first_fail": first_fail, "last_check": now, "rebooted_at": now}
                await agent.send_cs_event("CS_WATCHDOG_EVENT", {
                    "hostname": agent.hostname, "vmid": vmid,
                    "action": "soft_reboot", "ts": now})
            # Reboots disabled: just log.
            else:
                logger.warning(f"vm_agent_watchdog: VM {vmid} unresponsive — "
                               f"auto-reboot disabled, reporting only")

    # Save the state only if something changed.
    if mutated:
        _write_json(VM_WD_STATE, state)