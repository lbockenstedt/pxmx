"""Fleet OS-package updates (apt) for spokes, agents and the hub.

DISTINCT FROM THE LM CODE UPDATE PATH. ``update_pipeline.py`` /
``messaging/self_update.py`` ship *our* code (git/tarball + SPOKE_UPDATE
fan-out). This module updates the underlying OPERATING SYSTEM packages of the
box a node runs on. The two are deliberately separate: they have different
blast radii, different approval requirements, and different failure modes, and
conflating them would let a routine code push drag a kernel upgrade along with
it.

Design decisions (operator-chosen, see the WebUI panel):
  * ``apt-get dist-upgrade`` — what Proxmox documents for PVE hosts; plain
    ``upgrade`` silently holds back kernel/PVE transitions and the host drifts.
  * NEVER auto-reboot. Kernel/PVE updates commonly need one, and these hosts are
    running the client fleet. We report ``reboot_required`` and stop; rebooting
    is a separate, explicit operator action.
  * Eligibility is DETECTED, never assumed from "the node reports in". The fleet
    is not homogeneous: TrueNAS SCALE is Debian underneath but appliance-managed
    (apt corrupts its own updater), OPNsense is FreeBSD, and `nw` nodes are
    switches/APs with no host at all. Those are reported as unmanaged WITH A
    REASON rather than hidden, so "up to date" is never confused with "not
    covered".

Selective patching: ``apply_updates(packages=[...])`` upgrades ONLY the named
packages (``apt-get install --only-upgrade``) instead of the whole node. An
empty list is refused — it must never silently widen into a full dist-upgrade.

Privilege: apt and reboot need root. Root nodes (generic agent, pxmx host
agent) run apt directly; nodes running as ``svc_lm`` (hub, cs spoke, standalone
dns/dhcp) go through ``sudo -n /usr/local/bin/lm-os-update`` — a root-owned
helper with a fixed set of actions (``scripts/lm-os-update``), installed with a
matching sudoers rule. Without it a check still works but apply is reported as
blocked WITH the reason, rather than failing mid-run.

Everything here is synchronous subprocess work; callers run it via
``asyncio.to_thread`` so a 10-minute dist-upgrade never blocks the event loop.

VENDORED: ``pxmx/agent/src/os_update.py`` is a verbatim copy. Change both.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("OsUpdate")

# apt needs an explicit non-interactive posture or a changed config file opens a
# prompt on stdin that nothing will ever answer — the process then hangs until
# the timeout, mid-upgrade, which is the worst place to be interrupted.
_APT_ENV = {
    **os.environ,
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "NEEDRESTART_MODE": "a",
}
_APT_CONF = [
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
]

CHECK_TIMEOUT_S = 180
APPLY_TIMEOUT_S = 3600      # a PVE dist-upgrade on a slow mirror genuinely takes this long

# Wait for the dpkg lock rather than dying with rc=100 when another apt user
# (a role deploy, unattended-upgrades) is mid-run. Each wait MUST stay under the
# subprocess timeout above, or we would kill apt for waiting as instructed and
# report a timeout instead of the queue it was politely sitting in.
_CHECK_LOCK_TIMEOUT_S = 120
_APPLY_LOCK_TIMEOUT_S = 600


def _lock_flags(timeout_s: int) -> List[str]:
    return ["-o", f"DPkg::Lock::Timeout={timeout_s}"]


# Capabilities this node understands beyond the original check/apply-all. The
# hub only offers package selection / reboot for nodes that report them.
FEATURES = ["packages", "reboot"]

HELPER = "/usr/local/bin/lm-os-update"
# Debian package name, optionally arch-qualified. Same rule as the helper's.
_PKG_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*(:[a-z0-9\-]+)?$")


def _is_root() -> bool:
    return getattr(os, "geteuid", lambda: 0)() == 0


def _privileged(action: str, args: Optional[List[str]] = None) -> List[str]:
    """argv for a root-only step: direct when root, else via the sudo helper."""
    args = list(args or [])
    if not _is_root():
        return ["sudo", "-n", HELPER, action, *args]
    direct = {
        "update": ["apt-get", "update", "-qq", *_lock_flags(_CHECK_LOCK_TIMEOUT_S)],
        "dist-upgrade": ["apt-get", "dist-upgrade", "-y", *_APT_CONF,
                         *_lock_flags(_APPLY_LOCK_TIMEOUT_S)],
        "upgrade-only": ["apt-get", "install", "--only-upgrade", "-y", *_APT_CONF,
                         *_lock_flags(_APPLY_LOCK_TIMEOUT_S)],
        "reboot": ["shutdown", "-r", "+1", "LM: operator-approved reboot"],
    }
    return direct[action] + args


def _privilege_blocker() -> str:
    """Why this node can't run apt/reboot itself ("" when it can)."""
    if _is_root() or os.path.exists(HELPER):
        return ""
    return ("this node runs as a non-root user and the lm-os-update helper is not "
            "installed — re-run the node's installer to enable updates")


def _run(argv: List[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout, env=_APT_ENV)


def detect_capability() -> Dict[str, Any]:
    """Can this node be OS-updated by us, and how?

    Returns ``{eligible, manager, flavor, reason}``. ``reason`` is always set
    when ``eligible`` is False so the UI can say WHY a node is unmanaged instead
    of silently omitting it.
    """
    # Appliance checks come FIRST: TrueNAS SCALE ships apt, so an apt-first probe
    # would happily classify it as eligible and then break its updater.
    if os.path.exists("/usr/bin/midclt") or os.path.exists("/etc/truenas"):
        return {"eligible": False, "manager": None, "flavor": "truenas",
                "reason": "TrueNAS is appliance-managed — update it from its own UI "
                          "(apt here corrupts the appliance updater)"}
    if os.path.exists("/usr/local/sbin/opnsense-version"):
        return {"eligible": False, "manager": None, "flavor": "opnsense",
                "reason": "OPNsense is FreeBSD and appliance-managed — update it "
                          "from its own UI"}
    if not shutil.which("apt-get"):
        return {"eligible": False, "manager": None, "flavor": "unknown",
                "reason": "no apt-get on this node — not a Debian-family host"}
    flavor = "proxmox" if shutil.which("pveversion") else "debian"
    return {"eligible": True, "manager": "apt", "flavor": flavor, "reason": ""}


def _reboot_required() -> bool:
    """Debian marks this file; Proxmox kernel updates set it too."""
    return os.path.exists("/var/run/reboot-required") or os.path.exists("/run/reboot-required")


def _parse_upgradable(text: str) -> List[Dict[str, str]]:
    """Parse ``apt list --upgradable`` into ``[{package, candidate, current}]``.

    Line shape: ``pkg/suite 1.2.3 amd64 [upgradable from: 1.2.2]``. Anything that
    doesn't match is skipped rather than guessed at — a half-parsed package name
    in an approval UI is worse than a missing row.

    The suite is kept and used to flag SECURITY updates (``*-security``, and
    Proxmox's own ``pve-*`` repos carry security fixes too). dist-upgrade applies
    everything regardless; the flag exists so the approval panel can show what a
    batch actually contains rather than just a count.
    """
    out: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("Listing") or "/" not in line:
            continue
        m = re.match(r"^([^/\s]+)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from:\s*([^\]]+)\]", line)
        if not m:
            continue
        suite = m.group(2)
        out.append({"package": m.group(1), "suite": suite,
                    "candidate": m.group(3), "current": m.group(4).strip(),
                    "security": bool(re.search(r"security", suite, re.I))})
    return out


def check_updates(refresh: bool = True) -> Dict[str, Any]:
    """List pending OS package updates. Read-only — never installs anything."""
    cap = detect_capability()
    if not cap["eligible"]:
        return {"status": "SUCCESS", "eligible": False, "reason": cap["reason"],
                "flavor": cap["flavor"], "packages": [], "count": 0,
                "reboot_required": False}
    warnings: List[str] = []
    blocker = _privilege_blocker()
    if refresh and blocker:
        warnings.append("package lists not refreshed: " + blocker)
    elif refresh:
        try:
            r = _run(_privileged("update"), CHECK_TIMEOUT_S)
            if r.returncode != 0:
                # A failing metadata refresh is reported, not fatal: the cached
                # list is still useful, and a dead mirror shouldn't blank the panel.
                warnings.append(f"apt-get update exited {r.returncode}: "
                                f"{(r.stderr or '').strip()[:300]}")
        except subprocess.TimeoutExpired:
            warnings.append(f"apt-get update timed out after {CHECK_TIMEOUT_S}s")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"apt-get update failed: {exc}")
    try:
        r = _run(["apt", "list", "--upgradable"], CHECK_TIMEOUT_S)
        pkgs = _parse_upgradable(r.stdout)
    except Exception as exc:  # noqa: BLE001
        return {"status": "ERROR", "eligible": True, "flavor": cap["flavor"],
                "message": f"listing upgradable packages failed: {exc}",
                "packages": [], "count": 0, "reboot_required": _reboot_required(),
                "warnings": warnings}
    sec = sum(1 for p in pkgs if p.get("security"))
    return {"status": "SUCCESS", "eligible": True, "flavor": cap["flavor"],
            "packages": pkgs, "count": len(pkgs),
            "security_count": sec, "other_count": len(pkgs) - sec,
            "reboot_required": _reboot_required(), "warnings": warnings,
            "features": FEATURES, "apply_blocker": blocker}


def apply_updates(packages: Optional[List[str]] = None) -> Dict[str, Any]:
    """Upgrade the node. NEVER reboots, whatever the outcome.

    ``packages=None`` → ``apt-get dist-upgrade -y`` (everything).
    ``packages=[...]`` → ``apt-get install --only-upgrade`` of just those; names
    no longer pending are skipped and reported. An empty list is refused.

    Returns the package count applied plus ``reboot_required`` so the caller can
    surface a badge. Output is tail-capped: a dist-upgrade log is enormous and
    the useful part is the end.
    """
    cap = detect_capability()
    if not cap["eligible"]:
        # Refuse rather than fall back to something clever. An "update" path that
        # improvises on an appliance is exactly how you break one.
        return {"status": "ERROR", "eligible": False, "reason": cap["reason"],
                "message": f"refusing to apply updates: {cap['reason']}"}
    blocker = _privilege_blocker()
    if blocker:
        return {"status": "ERROR", "eligible": True, "message": blocker,
                "reboot_required": _reboot_required()}
    selected: List[str] = []
    skipped: List[str] = []
    if packages is not None:
        names = [str(p).strip() for p in packages if str(p).strip()]
        if not names:
            return {"status": "ERROR", "eligible": True,
                    "message": "no packages selected"}
        bad = [p for p in names if not _PKG_RE.match(p)]
        if bad:
            return {"status": "ERROR", "eligible": True,
                    "message": f"invalid package name(s): {', '.join(bad[:10])}"}
    before = check_updates(refresh=True)
    if packages is not None:
        pending = {p.get("package") for p in before.get("packages") or []}
        selected = [p for p in dict.fromkeys(names) if p in pending]
        skipped = [p for p in dict.fromkeys(names) if p not in pending]
        if not selected:
            return {"status": "SUCCESS", "eligible": True, "flavor": cap["flavor"],
                    "mode": "packages", "applied": 0, "packages_applied": [],
                    "skipped": skipped, "remaining": int(before.get("count") or 0),
                    "reboot_required": _reboot_required(),
                    "message": "none of the selected packages are still pending"}
        argv = _privileged("upgrade-only", selected)
    else:
        argv = _privileged("dist-upgrade")
    try:
        r = _run(argv, APPLY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"status": "ERROR", "eligible": True,
                "message": f"upgrade exceeded {APPLY_TIMEOUT_S}s — the host may be "
                           "mid-upgrade; check it before retrying",
                "reboot_required": _reboot_required()}
    except Exception as exc:  # noqa: BLE001
        return {"status": "ERROR", "eligible": True, "message": str(exc),
                "reboot_required": _reboot_required()}
    after = check_updates(refresh=False)
    ok = r.returncode == 0
    step = "dist-upgrade" if packages is None else "package upgrade"
    return {
        "status": "SUCCESS" if ok else "ERROR",
        "eligible": True,
        "flavor": cap["flavor"],
        "mode": "all" if packages is None else "packages",
        "packages_applied": selected,
        "skipped": skipped,
        "returncode": r.returncode,
        "applied": max(0, int(before.get("count") or 0) - int(after.get("count") or 0)),
        "remaining": int(after.get("count") or 0),
        "reboot_required": _reboot_required(),
        "output": ((r.stdout or "") + (r.stderr or ""))[-4000:],
        "message": "" if ok else f"{step} exited {r.returncode}: "
                                 f"{(r.stderr or '').strip()[-300:]}",
    }


def reboot_node() -> Dict[str, Any]:
    """Schedule a reboot in one minute (``shutdown -r +1``).

    Only ever called on an explicit operator action. The delay lets this node
    answer the hub before it goes down; ``shutdown -c`` on the host cancels it.
    """
    cap = detect_capability()
    if not cap["eligible"]:
        return {"status": "ERROR", "eligible": False,
                "message": f"refusing to reboot: {cap['reason']}"}
    blocker = _privilege_blocker()
    if blocker:
        return {"status": "ERROR", "eligible": True, "message": blocker}
    try:
        r = _run(_privileged("reboot"), 30)
    except Exception as exc:  # noqa: BLE001
        return {"status": "ERROR", "eligible": True, "message": str(exc)}
    if r.returncode != 0:
        return {"status": "ERROR", "eligible": True,
                "message": f"shutdown exited {r.returncode}: "
                           f"{(r.stderr or '').strip()[:300]}"}
    return {"status": "SUCCESS", "eligible": True,
            "message": "reboot scheduled in 1 minute"}
