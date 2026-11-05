"""Host-level tuning the agent owns and keeps true.

Settings that belong to the HOST rather than to any VM, applied by the installer
and then re-asserted by the agent on every start. The agent half matters because
the fleet is already deployed: an installer-only change reaches new hosts and
silently skips every existing one, which is how a setting ends up true on some
boxes and not others with nothing reporting the difference.

Everything here is idempotent and best-effort — a tuning failure must never stop
the agent from coming up.
"""

# logging reports what each tuning step found and changed.
import logging
# os reads env overrides, checks files, and does atomic renames.
import os
# re matches the target key in conf files (including commented-out forms).
import re
# shutil copies the net-watchdog unit files into place.
import shutil
# subprocess runs systemctl.
import subprocess
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, Optional

# Share the main agent logger so tuning messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# ksmtuned's config file and systemd service name.
KSMTUNED_CONF = "/etc/ksmtuned.conf"
KSMTUNED_SERVICE = "ksmtuned"

# ksmtuned starts KSM once free memory falls below KSM_THRES_COEF PERCENT of
# total. The stock 20 means KSM only engages when the host is nearly full, which
# is far too late here: these hosts run a dozen-plus near-identical sim VMs off
# the same template, so their guest memory is highly dedupable and there is no
# reason to wait for pressure before reclaiming it. 80 keeps KSM working
# essentially all the time, trading a little CPU for a lot of RAM.
KSM_THRES_COEF = int(os.environ.get("LM_PXMX_KSM_THRES_COEF", "80") or 80)

# ── gateway-loss net-watchdog units ─────────────────────────────────────────
# The net-watchdog (lm-pxmx-net-watchdog.timer) reboots the host after the
# default gateway has been unreachable for NET_DOWN_REBOOT_SECS. It is installed
# by install_agent.sh Phase G — but the agent SELF-UPDATE path only swaps the
# code tree + restarts; it never (re)installs these units or enables the timer.
# So a host that received the net-watchdog release via self-update alone (never a
# fresh reinstall) silently has NO gateway-loss reboot. Re-assert it here, on
# every agent start, exactly like ensure_ksm — the source unit files ship inside
# the agent tree (copied into the install dir by both the installer and the
# self-update), so the install dir is the authoritative source.
# systemd timer that drives the net-watchdog check.
NET_WATCHDOG_TIMER = "lm-pxmx-net-watchdog.timer"
# {source basename in the install dir: (destination, mode)}
NET_WATCHDOG_UNITS = {
    "lm-pxmx-net-watchdog.sh": ("/usr/local/bin/lm-pxmx-net-watchdog", 0o755),
    "lm-pxmx-net-watchdog.service": ("/etc/systemd/system/lm-pxmx-net-watchdog.service", 0o644),
    "lm-pxmx-net-watchdog.timer": ("/etc/systemd/system/lm-pxmx-net-watchdog.timer", 0o644),
}


def _install_dir() -> str:
    """The agent install dir — parent of this package's ``src`` dir. Both the
    installer and the self-update drop the net-watchdog unit files at its top
    level (alongside ``src/``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _same_contents(a: str, b: str) -> bool:
    """Return True when two files have identical bytes (False if either cannot be read)."""
    # Compare whole file contents; these unit files are tiny.
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    # Missing or unreadable file counts as "different" so the caller re-copies it.
    except OSError:
        return False


def _run(argv, timeout: int = 20):
    """Best-effort subprocess; returns (rc, out). Never raises."""
    # Run the command with captured output and a timeout; decode stdout leniently.
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace").strip()
    # Command missing or timed out: log quietly and report a generic failure code.
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("host_tuning: %s failed: %s", argv[0], e)
        return 1, ""


def set_conf_value(path: str, key: str, value: str) -> Optional[bool]:
    """Set ``key=value`` in a shell-style conf file, idempotently.

    Returns True when the file was changed, False when it already matched, and
    None when it could not be read/written.

    Handles the three states these files are actually found in: the key set to
    something else, the key present but COMMENTED OUT (how ksmtuned.conf ships),
    and the key absent entirely. A commented default that is merely appended to
    leaves two plausible-looking lines in the file, so the commented form is
    rewritten in place rather than duplicated.
    """
    # Read the conf file; a missing/unreadable file is reported as None.
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        logger.debug("host_tuning: cannot read %s: %s", path, e)
        return None

    # The exact line we want, and a pattern that matches the key whether set or commented out.
    want = f"{key}={value}"
    rx = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    # Rebuilt lines, whether we have already seen the key, and whether anything changed.
    out, seen, changed = [], False, False
    # Copy each line, replacing the first key line with the wanted value.
    for ln in lines:
        # A line defining (or commenting out) our key.
        if rx.match(ln):
            if seen:
                continue                      # drop duplicate definitions
            # First occurrence: rewrite it if it differs from the wanted line.
            seen = True
            if ln.strip() != want:
                out.append(want)
                changed = True
            else:
                out.append(ln)
        # Unrelated line: keep it unchanged.
        else:
            out.append(ln)
    # Key absent entirely: append it.
    if not seen:
        out.append(want)
        changed = True
    # Nothing changed: leave the file untouched.
    if not changed:
        return False
    # Write to a temp file and rename it over the original.
    try:
        tmp = f"{path}.lmtmp"
        with open(tmp, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)                 # atomic: never leave a torn conf
        return True
    # Could not write (read-only fs, permissions): report as None.
    except OSError as e:
        logger.debug("host_tuning: cannot write %s: %s", path, e)
        return None


def ensure_ksm(thres_coef: int = KSM_THRES_COEF) -> Dict[str, Any]:
    """Ensure ksmtuned is configured, enabled and running.

    Reports what it found and what it changed rather than just succeeding
    quietly, so a host where this silently does nothing is visible in the log.
    """
    # Result report: conf state, enabled/active state, whether we restarted, and the value used.
    res: Dict[str, Any] = {"conf": None, "enabled": None, "active": None,
                           "restarted": False, "thres_coef": thres_coef}
    # No ksmtuned config file means the package is not installed.
    if not os.path.exists(KSMTUNED_CONF):
        # ksm-control-daemon not installed. Not fatal, and not something the
        # agent should apt-install behind the operator's back.
        res["conf"] = "missing"
        logger.info("host_tuning: %s absent — ksmtuned not installed, skipping",
                    KSMTUNED_CONF)
        return res

    # Make sure the threshold is set, and record whether that changed the file.
    changed = set_conf_value(KSMTUNED_CONF, "KSM_THRES_COEF", str(thres_coef))
    res["conf"] = {True: "updated", False: "already-set", None: "unwritable"}[changed]

    # Enable the service at boot if it is not already, then record the final enabled state.
    rc, _ = _run(["systemctl", "is-enabled", "--quiet", KSMTUNED_SERVICE])
    if rc != 0:
        _run(["systemctl", "enable", KSMTUNED_SERVICE])
    rc, _ = _run(["systemctl", "is-enabled", "--quiet", KSMTUNED_SERVICE])
    res["enabled"] = (rc == 0)

    # Check whether the daemon is running right now.
    rc, _ = _run(["systemctl", "is-active", "--quiet", KSMTUNED_SERVICE])
    active = rc == 0
    # Restart only when the config actually changed, or when it is not running.
    # An unconditional restart on every agent start would bounce KSM tuning
    # needlessly and lose the daemon's accumulated state.
    if changed is True or not active:
        # Restart it and re-check that it came up.
        _run(["systemctl", "restart", KSMTUNED_SERVICE])
        res["restarted"] = True
        rc, _ = _run(["systemctl", "is-active", "--quiet", KSMTUNED_SERVICE])
        active = rc == 0
    # Record the final running state.
    res["active"] = active

    # Log the full outcome so a host where KSM is off is visible.
    logger.info("host_tuning: ksmtuned conf=%s enabled=%s active=%s restarted=%s "
                "(KSM_THRES_COEF=%s)", res["conf"], res["enabled"], res["active"],
                res["restarted"], thres_coef)
    # Warn loudly when KSM is still not running.
    if not active:
        logger.warning("host_tuning: ksmtuned is NOT running — KSM page merging "
                       "is off, so identical guest memory is not being reclaimed")
    return res


def ensure_net_watchdog() -> Dict[str, Any]:
    """Ensure the gateway-loss net-watchdog units are installed and its timer is
    enabled + active. Installer Phase G does this for fresh installs; this
    re-asserts it for hosts that only ever self-updated (code swap + restart,
    which never touches these units). Idempotent, best-effort — never raises.
    """
    # Result report: which files were copied and the timer's final enabled/active state.
    res: Dict[str, Any] = {"copied": [], "enabled": None, "active": None}
    # Unit files ship in the install dir; track whether we installed anything new.
    src_dir = _install_dir()
    changed = False
    # Copy each unit file to its systemd/bin destination when missing or out of date.
    for name, (dest, mode) in NET_WATCHDOG_UNITS.items():
        # Path of the shipped copy of this unit.
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            # Source not shipped (older tree) — don't clobber an installer copy.
            continue
        # Destination already identical: nothing to do.
        if os.path.isfile(dest) and _same_contents(src, dest):
            continue
        # Install the file with the right permissions and record it.
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest)
            os.chmod(dest, mode)
            res["copied"].append(dest)
            changed = True
        # Copy failed (permissions, read-only fs): log and try the remaining files.
        except OSError as e:
            logger.debug("host_tuning: net-watchdog copy %s→%s failed: %s",
                         src, dest, e)

    # systemd only notices new or changed unit files after a daemon-reload.
    if changed:
        _run(["systemctl", "daemon-reload"])

    # Timer not enabled: enable it and start it now.
    rc, _ = _run(["systemctl", "is-enabled", "--quiet", NET_WATCHDOG_TIMER])
    if rc != 0:
        _run(["systemctl", "enable", "--now", NET_WATCHDOG_TIMER])
    # Already enabled: just make sure it is running.
    else:
        rc, _ = _run(["systemctl", "is-active", "--quiet", NET_WATCHDOG_TIMER])
        if rc != 0:
            _run(["systemctl", "start", NET_WATCHDOG_TIMER])

    # Record the timer's final enabled and active state.
    rc, _ = _run(["systemctl", "is-enabled", "--quiet", NET_WATCHDOG_TIMER])
    res["enabled"] = (rc == 0)
    rc, _ = _run(["systemctl", "is-active", "--quiet", NET_WATCHDOG_TIMER])
    res["active"] = (rc == 0)

    # Log the outcome so hosts without the gateway-loss reboot are visible.
    logger.info("host_tuning: net-watchdog copied=%s enabled=%s active=%s",
                res["copied"], res["enabled"], res["active"])
    # Warn loudly when the timer is still not active.
    if not res["active"]:
        logger.warning("host_tuning: lm-pxmx-net-watchdog.timer is NOT active — "
                       "gateway-loss reboot will not run on this host")
    return res


def apply_all() -> Dict[str, Any]:
    """Every host tuning, applied once at agent start. Never raises."""
    # Collected per-tuning results.
    out: Dict[str, Any] = {}
    # Apply KSM tuning; any unexpected error is recorded instead of raised.
    try:
        out["ksm"] = ensure_ksm()
    except Exception as e:  # noqa: BLE001 — tuning must never block startup
        logger.warning("host_tuning: ksm failed: %s", e)
        out["ksm"] = {"error": str(e)}
    # Apply the net-watchdog units the same way.
    try:
        out["net_watchdog"] = ensure_net_watchdog()
    except Exception as e:  # noqa: BLE001 — tuning must never block startup
        logger.warning("host_tuning: net_watchdog failed: %s", e)
        out["net_watchdog"] = {"error": str(e)}
    # Report every tuning's outcome to the caller.
    return out
