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


def apply_all() -> Dict[str, Any]:
    """Every host tuning, applied once at agent start. Never raises."""
    out: Dict[str, Any] = {}
    try:
        out["ksm"] = ensure_ksm()
    except Exception as e:  # noqa: BLE001 — tuning must never block startup
        logger.warning("host_tuning: ksm failed: %s", e)
        out["ksm"] = {"error": str(e)}
    return out
