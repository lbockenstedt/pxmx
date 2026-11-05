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

# asyncio provides the one-second sleeps while waiting for a VM to stop.
import asyncio
# json reads and writes the persisted per-VM heartbeat file.
import json
# logging reports every watchdog action.
import logging
# os handles env overrides and the state file.
import os
# time stamps heartbeats and actions.
import time
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, List, Optional, Set

# Share the main agent logger so watchdog messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# Persistent state directory and this watchdog's heartbeat/action file (survives reboots).
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
# Seconds to wait for `qm agent ping` before treating the guest as unresponsive.
PING_TIMEOUT_S = int(os.environ.get("LM_PXMX_GUEST_WD_PING_TIMEOUT_S", "5") or 5)

# Last pass's per-VM outcome, for telemetry (see current_guest_watchdog).
_last_pass: Dict[str, Any] = {}


# ── state ────────────────────────────────────────────────────────────────────

def _load() -> Dict[str, Any]:
    """Load the per-VM watchdog state, returning {} if the file is missing, empty or corrupt."""
    # Read only a non-empty file; anything other than a dict is ignored.
    try:
        if os.path.exists(GUEST_WATCHDOG_FILE) and os.path.getsize(GUEST_WATCHDOG_FILE) > 0:
            with open(GUEST_WATCHDOG_FILE) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    # Unreadable/corrupt file: start fresh (debug log only).
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("guest_watchdog: state read failed: %s", e)
    # Fallback: empty state.
    return {}


def _save(d: Dict[str, Any]) -> None:
    """Atomically write the per-VM watchdog state (errors are logged at debug, not raised)."""
    # Create the directory, write a temp file, then rename it over the real file.
    try:
        os.makedirs(PXMLIB, exist_ok=True)
        tmp = f"{GUEST_WATCHDOG_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, GUEST_WATCHDOG_FILE)   # atomic — a torn write loses heartbeats
    # Disk/permission problem: skip this save; the next pass will retry.
    except OSError as e:
        logger.debug("guest_watchdog: state write failed: %s", e)


def current_guest_watchdog() -> Dict[str, Any]:
    """Last pass's outcome for the telemetry body / WebUI."""
    # Return a copy so callers cannot mutate the module's record.
    return dict(_last_pass)


def current_guest_watchdog_state() -> Dict[str, Any]:
    """Per-VM persisted watchdog record (last_ok / last_action* /
    last_ping_error* / ping_error_streak), for the telemetry body / WebUI —
    durable across passes/restarts, unlike current_guest_watchdog()'s
    single-pass-only ``errors`` list. Added specifically so a ping-timeout
    streak on one VM (the "OS is genuinely wedged" signal — see the ping
    exception handling in run_pass) has something to inspect after the fact
    instead of a debug log line that already scrolled away. Bounded to VMs
    currently in the fleet (run_pass prunes anything no longer present)."""
    # Read straight from disk so the data survives agent restarts.
    return dict(_load())


# ── scope ────────────────────────────────────────────────────────────────────

def _in_flight_vmids() -> Set[int]:
    """VMIDs the provisioning loop is actively mutating this moment.

    Resetting or stopping a VM mid-clone corrupts it. The legacy bash script had
    no loop to coordinate with; this agent does, so these are skipped for the
    pass rather than acted on.
    """
    # Collected VMIDs to skip.
    out: Set[int] = set()
    # Any lookup failure must not stop the watchdog pass.
    try:
        # Imported lazily to avoid a circular import with usb_provision.
        from . import usb_provision
        # VMs currently being deleted or recloned.
        out |= {int(v) for v in (usb_provision.current_deleting_vmids() or [])}
        out |= {int(v) for v in (usb_provision.current_reclone_vmids() or [])}
        # VMs in the currently running provisioning batch, if one is running.
        run = usb_provision.current_prov_run() or {}
        if run.get("running"):
            # Add each batch item's VMID, skipping malformed items.
            for it in (run.get("items") or []):
                try:
                    out.add(int((it or {}).get("vmid")))
                except (TypeError, ValueError):
                    continue
    # Lookup failed: skip nothing extra rather than abort.
    except Exception as e:  # noqa: BLE001 — never let scoping sink the pass
        logger.debug("guest_watchdog: in-flight lookup failed: %s", e)
    # VMIDs to leave alone this pass.
    return out


def _recently_cloned_vmids(now: float) -> Set[int]:
    """VMIDs still inside their post-clone settle window (QGA-silent by design)."""
    # Collected VMIDs still settling after a clone.
    out: Set[int] = set()
    # Any lookup failure must not stop the watchdog pass.
    try:
        # Imported lazily to avoid a circular import with usb_provision.
        from . import usb_provision
        # The post-clone reboot queue records when each VM was cloned.
        st = usb_provision.load_usb_state() or {}
        # A VM cloned less than POST_CLONE_GRACE_S ago is still settling.
        for vid, rec in (st.get("post_prov_reboot") or {}).items():
            ts = (rec or {}).get("cloned_at")
            if isinstance(ts, (int, float)) and (now - float(ts)) < POST_CLONE_GRACE_S:
                # Add the VMID, skipping keys that are not numbers.
                try:
                    out.add(int(vid))
                except (TypeError, ValueError):
                    continue
    # Lookup failed: skip nothing extra rather than abort.
    except Exception as e:  # noqa: BLE001
        logger.debug("guest_watchdog: clone-window lookup failed: %s", e)
    # VMIDs to leave alone this pass.
    return out


# ── the pass ─────────────────────────────────────────────────────────────────

async def run_pass(agent, now: Optional[float] = None) -> Dict[str, Any]:
    """One watchdog sweep over the host's sim VMs. Never raises."""
    # Imported lazily to avoid circular imports with these sibling modules.
    from . import pve_cmds, usb_provision
    # Allow tests to inject a fixed clock; otherwise use the current time.
    now = time.time() if now is None else now
    # Summary of what this pass did, returned and kept for telemetry.
    result: Dict[str, Any] = {
        "ran_at": now, "checked": 0, "responding": 0, "skipped": 0,
        "reset": [], "power_cycled": [], "started": [], "waiting": [],
        "errors": [],
    }
    # Protected VMIDs (e.g. the hub container) are never touched; default to none on error.
    try:
        protected = usb_provision._protected_vmids(agent)
    except Exception:  # noqa: BLE001
        protected = set()
    # List the host's QEMU VMs; without that list there is nothing safe to do.
    try:
        vmids = await pve_cmds.list_qemu_vmids()
    # Could not list VMs: record the error, publish it as this pass's result, and stop.
    except Exception as e:  # noqa: BLE001
        logger.warning("guest_watchdog: could not list VMs: %s", e)
        result["errors"].append(f"list_qemu_vmids: {e}")
        _last_pass.clear(); _last_pass.update(result)
        return result

    # VMs the provisioning loop is busy with, and VMs still settling after a clone.
    in_flight = _in_flight_vmids()
    settling = _recently_cloned_vmids(now)
    # VMs the DONGLE-HEALTH ladder just acted on. It reboots a VM as one of its
    # rungs, and a rebooting VM is QGA-silent by design -- indistinguishable
    # from hung. Each path honours only its own cooldown, so without this the
    # two recovery systems act on the same VM in turn, each one's action
    # tripping the other's trigger.
    # Merge in VMs the dongle-health ladder just acted on (see the comment above).
    try:
        settling = settling | usb_provision.recently_actioned_vmids(now)
    # Lookup failed: continue with the sets we already have.
    except Exception as e:  # noqa: BLE001
        logger.debug("guest_watchdog: ladder-action lookup failed: %s", e)
    # Load the persisted per-VM heartbeat records.
    state = _load()

    # Check every VM on the host in VMID order.
    for vid in sorted(int(v) for v in (vmids or [])):
        # Faithful to the bash guard, plus the agent's protected set.
        if vid <= VMID_FLOOR or vid in protected:
            continue
        # Busy or settling VMs are counted as skipped and left alone this pass.
        if vid in in_flight or vid in settling:
            result["skipped"] += 1
            continue
        # Templates are never running guests; skip them (a failed check just carries on).
        try:
            if await pve_cmds.is_template(vid):
                continue
        except Exception:  # noqa: BLE001
            pass

        # This VM's persisted record (empty if first seen) and count it as checked.
        key = str(vid)
        rec = state.get(key) or {}
        result["checked"] += 1
        # Ask the guest agent inside the VM whether it is alive.
        try:
            alive = await pve_cmds.qm_agent_ping(vid, protected=protected,
                                                 timeout=PING_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            # qm_agent_ping passes check=False, so _run only ever raises here
            # on the TIMEOUT path (PveError, unconditional regardless of
            # check) — meaning `qm agent ping` ITSELF hung waiting on the
            # guest's virtio-serial socket. That is a STRONGER signal of a
            # wedged guest than a clean non-zero exit (QGA not running but
            # the helper still returns promptly), yet this used to `continue`
            # past the VM entirely — skipping last_ok/silent_since bookkeeping
            # and the whole escalation ladder below. The single worst case
            # (ping hangs) got ZERO reset/power-cycle attempts, forever, while
            # a guest that cleanly refused the ping escalated normally. Fold
            # it into the SAME "not alive" path instead of skipping it.
            logger.warning("guest_watchdog: VM %s ping errored (%s) — "
                           "treating as not responding", vid, e)
            result["errors"].append(f"{vid}: ping {e}")
            # Persisted (not just this pass's transient `result`) so an
            # operator has something to look at after the fact — the prior
            # debug-level log line scrolled away with nothing durable behind
            # it, which is exactly why this went unnoticed for hours.
            rec["last_ping_error"] = str(e)[:300]
            rec["last_ping_error_ts"] = now
            rec["ping_error_streak"] = int(rec.get("ping_error_streak", 0)) + 1
            alive = False

        # Guest answered: record the heartbeat, clear any silence/error streak, and move on.
        if alive:
            rec["last_ok"] = now
            rec.pop("silent_since", None)
            rec.pop("ping_error_streak", None)
            state[key] = rec
            result["responding"] += 1
            continue

        # Silent. Respect the action cooldown before escalating again — one
        # reset must be given time to take effect, or the next pass reads the
        # still-booting VM as "unresponsive" and escalates to a power cycle.
        # When the watchdog last acted on this VM.
        last_act = rec.get("last_action_ts")
        # Still within the cooldown: note it as waiting and do nothing more this pass.
        if isinstance(last_act, (int, float)) and (now - float(last_act)) < ACTION_COOLDOWN_S:
            result["waiting"].append(vid)
            state[key] = rec
            continue

        # Last time the guest was seen healthy.
        last_ok = rec.get("last_ok")
        # Never seen healthy (see the "no-heartbeat case" in the module docstring).
        if not isinstance(last_ok, (int, float)):
            # No heartbeat on record. Bash read this as "host rebooted, /tmp
            # cleared" and started the VM; preserved verbatim by request. See the
            # module docstring — here it means "never observed healthy", so the
            # first pass on a host starts every stopped sim VM in range.
            # Start the VM and record the action; protected/failed starts are logged and listed.
            try:
                await pve_cmds.start_vm(vid, protected)
                rec["last_action"] = "start"
                rec["last_action_ts"] = now
                rec["actions"] = int(rec.get("actions", 0)) + 1
                result["started"].append(vid)
                logger.info("guest-watchdog: VM %s has no heartbeat on record "
                            "(never observed healthy) — starting", vid)
            # Start refused or failed: note it and move on.
            except Exception as e:  # noqa: BLE001
                logger.info("guest-watchdog: VM %s start skipped (%s)", vid, e)
                result["errors"].append(f"{vid}: start {e}")
            # Save the record and go to the next VM.
            state[key] = rec
            continue

        # How long the guest has been silent decides which rung of the ladder applies.
        silent_for = now - float(last_ok)
        # Silent past the hard limit: force a full power cycle.
        if silent_for > GUEST_HARD_S:
            # Clear any stale lock, stop the VM, then wait for it to actually stop.
            try:
                await pve_cmds._run(["qm", "unlock", str(vid)], check=False, timeout=30)
                await pve_cmds.stop_vm(vid, protected)
                # Poll once a second, up to STOP_WAIT_S seconds.
                for _ in range(STOP_WAIT_S):
                    await asyncio.sleep(1)
                    # If the status call fails, stop waiting and just try the start.
                    try:
                        st = await pve_cmds.vm_status(vid)
                    except Exception:  # noqa: BLE001
                        break
                    # vm_status returns {vmid, kind, running, raw} — there is no
                    # "status" key; `running` is the parsed signal.
                    if not (st or {}).get("running", True):
                        break
                # Start it again and record the power cycle.
                await pve_cmds.start_vm(vid, protected)
                rec["last_action"] = "power_cycle"
                rec["last_action_ts"] = now
                rec["actions"] = int(rec.get("actions", 0)) + 1
                result["power_cycled"].append(vid)
                # Power cycles are logged loudly so the operator can see them.
                logger.warning("guest-watchdog: VM %s guest agent silent %.0fs "
                               "(> %.0fs) — power cycling", vid, silent_for, GUEST_HARD_S)
            # Any step failed: log and list the error for this VM.
            except Exception as e:  # noqa: BLE001
                logger.warning("guest-watchdog: VM %s power cycle failed: %s", vid, e)
                result["errors"].append(f"{vid}: cycle {e}")
        # Silent past the grace window but under the hard limit: soft reset.
        elif silent_for > GUEST_GRACE_S:
            # `qm reset` is like pressing the reset button; record the action.
            try:
                await pve_cmds._run(["qm", "reset", str(vid)], check=False, timeout=30)
                rec["last_action"] = "reset"
                rec["last_action_ts"] = now
                rec["actions"] = int(rec.get("actions", 0)) + 1
                result["reset"].append(vid)
                # Resets are logged loudly so the operator can see them.
                logger.warning("guest-watchdog: VM %s guest agent silent %.0fs "
                               "(> %.0fs) — resetting", vid, silent_for, GUEST_GRACE_S)
            # Reset command failed: log and list the error for this VM.
            except Exception as e:  # noqa: BLE001
                logger.warning("guest-watchdog: VM %s reset failed: %s", vid, e)
                result["errors"].append(f"{vid}: reset {e}")
        else:
            # Under the grace window — may just be rebooting. Log and leave it.
            result["waiting"].append(vid)
            logger.info("guest-watchdog: VM %s guest agent silent %.0fs "
                        "(< %.0fs) — leaving alone", vid, silent_for, GUEST_GRACE_S)
        # Save this VM's updated record.
        state[key] = rec

    # Forget VMs that no longer exist so a destroyed+recreated vmid starts clean
    # rather than inheriting the dead VM's heartbeat.
    # Set of VMIDs that currently exist; drop records for any others, then save the state.
    live = {str(int(v)) for v in (vmids or [])}
    for gone in [k for k in state if k not in live]:
        state.pop(gone, None)
    _save(state)

    # Publish this pass's summary for telemetry.
    _last_pass.clear()
    _last_pass.update(result)
    # Only log the pass summary when the watchdog actually took an action.
    if result["reset"] or result["power_cycled"] or result["started"]:
        logger.warning("guest-watchdog pass: checked=%d responding=%d reset=%s "
                       "cycled=%s started=%s skipped=%d",
                       result["checked"], result["responding"], result["reset"],
                       result["power_cycled"], result["started"], result["skipped"])
    # Return the summary to the caller.
    return result
