"""QEMU guest-agent watchdog — port of the legacy ``proxmox/check_guest.sh``.

Recovers sim VMs whose guest OS has hung. The dongle health ladder in
``usb_provision`` deliberately will NOT do this: ``_guest_health_probe`` returns
``None`` when QGA is unreachable and ``_classify_guest_health`` maps that to
``unknown`` with an explicit "never escalate blind" rule. A VM whose OS wedged —
QGA silent, dongle perfectly fine — therefore had nothing watching it once the
legacy bash agent stopped being deployed. That is the hole this fills; the two
mechanisms are complementary, not competing.

Escalation ladder, per VM, unchanged from the bash original:

  ==========================  =======================================
  guest agent silent for      action
  ==========================  =======================================
  < GUEST_GRACE_S (10m)       log only — it may simply be rebooting
  GUEST_GRACE_S..HARD (10-20m) ``qm reset``            (soft)
  > GUEST_HARD_S (20m)        ``qm unlock`` + ``stop`` + wait + ``start``
  no heartbeat on record      ``qm start``             (see below)
  ==========================  =======================================

**The no-heartbeat case.** The bash keyed its heartbeat off ``/tmp/<vmid>.lastup``
and read a missing file as "the host rebooted and cleared /tmp", so it started
the VM. That behaviour is preserved verbatim here at the operator's explicit
request. Note the trigger is no longer equivalent: this agent's state lives in
``/var/lib/pxmx`` and SURVIVES a reboot, so a missing record now means "never
observed healthy" — which on a first run after deployment is EVERY VM in range.
The first pass on a host will therefore start every stopped sim VM it finds.

**Interlock with the provisioning loop (not in the original).** The bash predates
the provisioning loop and had nothing to coordinate with. Resetting or stopping a
VM while ``qm clone`` is mid-write corrupts it, so VMs the loop is actively
cloning, deleting or recloning are skipped for this pass — and so is one inside
its post-clone settle window, which is expected to be QGA-silent and to reboot
itself twice by design. This is a correctness interlock, not a softening of the
policy above: those VMs are skipped, never given a weaker action.
"""

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("PxmxAgent")

PXMLIB = "/var/lib/pxmx"
GUEST_WATCHDOG_FILE = f"{PXMLIB}/guest_watchdog.json"

# Cadence + thresholds. Defaults reproduce the cron line the bash shipped with
# (*/5) and its 10/20-minute ladder.
WATCHDOG_INTERVAL_S = float(os.environ.get("LM_PXMX_GUEST_WD_INTERVAL_S", "300") or 300)
GUEST_GRACE_S = float(os.environ.get("LM_PXMX_GUEST_WD_GRACE_S", "600") or 600)
GUEST_HARD_S = float(os.environ.get("LM_PXMX_GUEST_WD_HARD_S", "1200") or 1200)
# How long to wait for a VM to reach `stopped` before issuing start (bash: 30x1s).
STOP_WAIT_S = int(os.environ.get("LM_PXMX_GUEST_WD_STOP_WAIT_S", "30") or 30)
# Don't re-act on the same VM inside this window — one reset needs time to take
# effect before the next pass decides it "still isn't answering" and escalates.
# The bash had no such guard and could reset the same VM every 5 minutes.
ACTION_COOLDOWN_S = float(os.environ.get("LM_PXMX_GUEST_WD_COOLDOWN_S", "600") or 600)
# A freshly cloned VM is expected to be QGA-silent while it settles (and reboots
# itself twice by design — see the post-clone settle/reboot contract).
POST_CLONE_GRACE_S = float(os.environ.get("LM_PXMX_GUEST_WD_CLONE_GRACE_S", "1800") or 1800)
# Faithful to the bash guard: only VMs above the sim floor are ever touched.
VMID_FLOOR = int(os.environ.get("LM_PXMX_GUEST_WD_VMID_FLOOR", "90000") or 90000)
PING_TIMEOUT_S = int(os.environ.get("LM_PXMX_GUEST_WD_PING_TIMEOUT_S", "5") or 5)

# Last pass's per-VM outcome, for telemetry (see current_guest_watchdog).
_last_pass: Dict[str, Any] = {}


# ── state ────────────────────────────────────────────────────────────────────

def _load() -> Dict[str, Any]:
    try:
        if os.path.exists(GUEST_WATCHDOG_FILE) and os.path.getsize(GUEST_WATCHDOG_FILE) > 0:
            with open(GUEST_WATCHDOG_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("guest_watchdog: state read failed: %s", e)
    return {}


def _save(d: Dict[str, Any]) -> None:
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        tmp = f"{GUEST_WATCHDOG_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, GUEST_WATCHDOG_FILE)   # atomic — a torn write loses heartbeats
    except OSError as e:
        logger.debug("guest_watchdog: state write failed: %s", e)


def current_guest_watchdog() -> Dict[str, Any]:
    """Last pass's outcome for the telemetry body / WebUI."""
    return dict(_last_pass)


# ── scope ────────────────────────────────────────────────────────────────────

def _in_flight_vmids() -> Set[int]:
    """VMIDs the provisioning loop is actively mutating this moment.

    Resetting or stopping a VM mid-clone corrupts it. The legacy bash script had
    no loop to coordinate with; this agent does, so these are skipped for the
    pass rather than acted on.
    """
    out: Set[int] = set()
    try:
        from . import usb_provision
        out |= {int(v) for v in (usb_provision.current_deleting_vmids() or [])}
        out |= {int(v) for v in (usb_provision.current_reclone_vmids() or [])}
        run = usb_provision.current_prov_run() or {}
        if run.get("running"):
            for it in (run.get("items") or []):
                try:
                    out.add(int((it or {}).get("vmid")))
                except (TypeError, ValueError):
                    continue
    except Exception as e:  # noqa: BLE001 — never let scoping sink the pass
        logger.debug("guest_watchdog: in-flight lookup failed: %s", e)
    return out


def _recently_cloned_vmids(now: float) -> Set[int]:
    """VMIDs still inside their post-clone settle window (QGA-silent by design)."""
    out: Set[int] = set()
    try:
        from . import usb_provision
        st = usb_provision.load_usb_state() or {}
        for vid, rec in (st.get("post_prov_reboot") or {}).items():
            ts = (rec or {}).get("cloned_at")
            if isinstance(ts, (int, float)) and (now - float(ts)) < POST_CLONE_GRACE_S:
                try:
                    out.add(int(vid))
                except (TypeError, ValueError):
                    continue
    except Exception as e:  # noqa: BLE001
        logger.debug("guest_watchdog: clone-window lookup failed: %s", e)
    return out


# ── the pass ─────────────────────────────────────────────────────────────────

async def run_pass(agent, now: Optional[float] = None) -> Dict[str, Any]:
    """One watchdog sweep over the host's sim VMs. Never raises."""
    from . import pve_cmds, usb_provision
    now = time.time() if now is None else now
    result: Dict[str, Any] = {
        "ran_at": now, "checked": 0, "responding": 0, "skipped": 0,
        "reset": [], "power_cycled": [], "started": [], "waiting": [],
        "errors": [],
    }
    try:
        protected = usb_provision._protected_vmids(agent)
    except Exception:  # noqa: BLE001
        protected = set()
    try:
        vmids = await pve_cmds.list_qemu_vmids()
    except Exception as e:  # noqa: BLE001
        logger.warning("guest_watchdog: could not list VMs: %s", e)
        result["errors"].append(f"list_qemu_vmids: {e}")
        _last_pass.clear(); _last_pass.update(result)
        return result

    in_flight = _in_flight_vmids()
    settling = _recently_cloned_vmids(now)
    state = _load()

    for vid in sorted(int(v) for v in (vmids or [])):
        # Faithful to the bash guard, plus the agent's protected set.
        if vid <= VMID_FLOOR or vid in protected:
            continue
        if vid in in_flight or vid in settling:
            result["skipped"] += 1
            continue
        try:
            if await pve_cmds.is_template(vid):
                continue
        except Exception:  # noqa: BLE001
            pass

        key = str(vid)
        rec = state.get(key) or {}
        result["checked"] += 1
        try:
            alive = await pve_cmds.qm_agent_ping(vid, protected=protected,
                                                 timeout=PING_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            logger.debug("guest_watchdog: ping %s failed: %s", vid, e)
            result["errors"].append(f"{vid}: ping {e}")
            continue

        if alive:
            rec["last_ok"] = now
            rec.pop("silent_since", None)
            state[key] = rec
            result["responding"] += 1
            continue

        # Silent. Respect the action cooldown before escalating again — one
        # reset must be given time to take effect, or the next pass reads the
        # still-booting VM as "unresponsive" and escalates to a power cycle.
        last_act = rec.get("last_action_ts")
        if isinstance(last_act, (int, float)) and (now - float(last_act)) < ACTION_COOLDOWN_S:
            result["waiting"].append(vid)
            state[key] = rec
            continue

        last_ok = rec.get("last_ok")
        if not isinstance(last_ok, (int, float)):
            # No heartbeat on record. Bash read this as "host rebooted, /tmp
            # cleared" and started the VM; preserved verbatim by request. See the
            # module docstring — here it means "never observed healthy", so the
            # first pass on a host starts every stopped sim VM in range.
            try:
                await pve_cmds.start_vm(vid, protected)
                rec["last_action"] = "start"
                rec["last_action_ts"] = now
                rec["actions"] = int(rec.get("actions", 0)) + 1
                result["started"].append(vid)
                logger.info("guest-watchdog: VM %s has no heartbeat on record "
                            "(never observed healthy) — starting", vid)
            except Exception as e:  # noqa: BLE001
                logger.info("guest-watchdog: VM %s start skipped (%s)", vid, e)
                result["errors"].append(f"{vid}: start {e}")
            state[key] = rec
            continue

        silent_for = now - float(last_ok)
        if silent_for > GUEST_HARD_S:
            try:
                await pve_cmds._run(["qm", "unlock", str(vid)], check=False, timeout=30)
                await pve_cmds.stop_vm(vid, protected)
                for _ in range(STOP_WAIT_S):
                    await asyncio.sleep(1)
                    try:
                        st = await pve_cmds.vm_status(vid)
                    except Exception:  # noqa: BLE001
                        break
                    # vm_status returns {vmid, kind, running, raw} — there is no
                    # "status" key; `running` is the parsed signal.
                    if not (st or {}).get("running", True):
                        break
                await pve_cmds.start_vm(vid, protected)
                rec["last_action"] = "power_cycle"
                rec["last_action_ts"] = now
                rec["actions"] = int(rec.get("actions", 0)) + 1
                result["power_cycled"].append(vid)
                logger.warning("guest-watchdog: VM %s guest agent silent %.0fs "
                               "(> %.0fs) — power cycling", vid, silent_for, GUEST_HARD_S)
            except Exception as e:  # noqa: BLE001
                logger.warning("guest-watchdog: VM %s power cycle failed: %s", vid, e)
                result["errors"].append(f"{vid}: cycle {e}")
        elif silent_for > GUEST_GRACE_S:
            try:
                await pve_cmds._run(["qm", "reset", str(vid)], check=False, timeout=30)
                rec["last_action"] = "reset"
                rec["last_action_ts"] = now
                rec["actions"] = int(rec.get("actions", 0)) + 1
                result["reset"].append(vid)
                logger.warning("guest-watchdog: VM %s guest agent silent %.0fs "
                               "(> %.0fs) — resetting", vid, silent_for, GUEST_GRACE_S)
            except Exception as e:  # noqa: BLE001
                logger.warning("guest-watchdog: VM %s reset failed: %s", vid, e)
                result["errors"].append(f"{vid}: reset {e}")
        else:
            # Under the grace window — may just be rebooting. Log and leave it.
            result["waiting"].append(vid)
            logger.info("guest-watchdog: VM %s guest agent silent %.0fs "
                        "(< %.0fs) — leaving alone", vid, silent_for, GUEST_GRACE_S)
        state[key] = rec

    # Forget VMs that no longer exist so a destroyed+recreated vmid starts clean
    # rather than inheriting the dead VM's heartbeat.
    live = {str(int(v)) for v in (vmids or [])}
    for gone in [k for k in state if k not in live]:
        state.pop(gone, None)
    _save(state)

    _last_pass.clear()
    _last_pass.update(result)
    if result["reset"] or result["power_cycled"] or result["started"]:
        logger.warning("guest-watchdog pass: checked=%d responding=%d reset=%s "
                       "cycled=%s started=%s skipped=%d",
                       result["checked"], result["responding"], result["reset"],
                       result["power_cycled"], result["started"], result["skipped"])
    return result
