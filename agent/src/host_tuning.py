"""Host-level tuning the agent owns and keeps true.

Settings that belong to the HOST rather than to any VM, applied by the installer
and then re-asserted by the agent on every start. The agent half matters because
the fleet is already deployed: an installer-only change reaches new hosts and
silently skips every existing one, which is how a setting ends up true on some
boxes and not others with nothing reporting the difference.

Everything here is idempotent and best-effort — a tuning failure must never stop
the agent from coming up.
"""

import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger("PxmxAgent")

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
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _run(argv, timeout: int = 20):
    """Best-effort subprocess; returns (rc, out). Never raises."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace").strip()
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
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        logger.debug("host_tuning: cannot read %s: %s", path, e)
        return None

    want = f"{key}={value}"
    rx = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    out, seen, changed = [], False, False
    for ln in lines:
        if rx.match(ln):
            if seen:
                continue                      # drop duplicate definitions
            seen = True
            if ln.strip() != want:
                out.append(want)
                changed = True
            else:
                out.append(ln)
        else:
            out.append(ln)
    if not seen:
        out.append(want)
        changed = True
    if not changed:
        return False
    try:
        tmp = f"{path}.lmtmp"
        with open(tmp, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)                 # atomic: never leave a torn conf
        return True
    except OSError as e:
        logger.debug("host_tuning: cannot write %s: %s", path, e)
        return None


def ensure_ksm(thres_coef: int = KSM_THRES_COEF) -> Dict[str, Any]:
    """Ensure ksmtuned is configured, enabled and running.

    Reports what it found and what it changed rather than just succeeding
    quietly, so a host where this silently does nothing is visible in the log.
    """
    res: Dict[str, Any] = {"conf": None, "enabled": None, "active": None,
                           "restarted": False, "thres_coef": thres_coef}
    if not os.path.exists(KSMTUNED_CONF):
        # ksm-control-daemon not installed. Not fatal, and not something the
        # agent should apt-install behind the operator's back.
        res["conf"] = "missing"
        logger.info("host_tuning: %s absent — ksmtuned not installed, skipping",
                    KSMTUNED_CONF)
        return res

    changed = set_conf_value(KSMTUNED_CONF, "KSM_THRES_COEF", str(thres_coef))
    res["conf"] = {True: "updated", False: "already-set", None: "unwritable"}[changed]

    rc, _ = _run(["systemctl", "is-enabled", "--quiet", KSMTUNED_SERVICE])
    if rc != 0:
        _run(["systemctl", "enable", KSMTUNED_SERVICE])
    rc, _ = _run(["systemctl", "is-enabled", "--quiet", KSMTUNED_SERVICE])
    res["enabled"] = (rc == 0)

    rc, _ = _run(["systemctl", "is-active", "--quiet", KSMTUNED_SERVICE])
    active = rc == 0
    # Restart only when the config actually changed, or when it is not running.
    # An unconditional restart on every agent start would bounce KSM tuning
    # needlessly and lose the daemon's accumulated state.
    if changed is True or not active:
        _run(["systemctl", "restart", KSMTUNED_SERVICE])
        res["restarted"] = True
        rc, _ = _run(["systemctl", "is-active", "--quiet", KSMTUNED_SERVICE])
        active = rc == 0
    res["active"] = active

    logger.info("host_tuning: ksmtuned conf=%s enabled=%s active=%s restarted=%s "
                "(KSM_THRES_COEF=%s)", res["conf"], res["enabled"], res["active"],
                res["restarted"], thres_coef)
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
    res: Dict[str, Any] = {"copied": [], "enabled": None, "active": None}
    src_dir = _install_dir()
    changed = False
    for name, (dest, mode) in NET_WATCHDOG_UNITS.items():
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            # Source not shipped (older tree) — don't clobber an installer copy.
            continue
        if os.path.isfile(dest) and _same_contents(src, dest):
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest)
            os.chmod(dest, mode)
            res["copied"].append(dest)
            changed = True
        except OSError as e:
            logger.debug("host_tuning: net-watchdog copy %s→%s failed: %s",
                         src, dest, e)

    if changed:
        _run(["systemctl", "daemon-reload"])

    rc, _ = _run(["systemctl", "is-enabled", "--quiet", NET_WATCHDOG_TIMER])
    if rc != 0:
        _run(["systemctl", "enable", "--now", NET_WATCHDOG_TIMER])
    else:
        rc, _ = _run(["systemctl", "is-active", "--quiet", NET_WATCHDOG_TIMER])
        if rc != 0:
            _run(["systemctl", "start", NET_WATCHDOG_TIMER])

    rc, _ = _run(["systemctl", "is-enabled", "--quiet", NET_WATCHDOG_TIMER])
    res["enabled"] = (rc == 0)
    rc, _ = _run(["systemctl", "is-active", "--quiet", NET_WATCHDOG_TIMER])
    res["active"] = (rc == 0)

    logger.info("host_tuning: net-watchdog copied=%s enabled=%s active=%s",
                res["copied"], res["enabled"], res["active"])
    if not res["active"]:
        logger.warning("host_tuning: lm-pxmx-net-watchdog.timer is NOT active — "
                       "gateway-loss reboot will not run on this host")
    return res


def apply_all() -> Dict[str, Any]:
    """Every host tuning, applied once at agent start. Never raises."""
    out: Dict[str, Any] = {}
    try:
        out["ksm"] = ensure_ksm()
    except Exception as e:  # noqa: BLE001 — tuning must never block startup
        logger.warning("host_tuning: ksm failed: %s", e)
        out["ksm"] = {"error": str(e)}
    try:
        out["net_watchdog"] = ensure_net_watchdog()
    except Exception as e:  # noqa: BLE001 — tuning must never block startup
        logger.warning("host_tuning: net_watchdog failed: %s", e)
        out["net_watchdog"] = {"error": str(e)}
    return out
