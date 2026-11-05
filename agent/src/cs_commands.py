"""Client-Simulation fast command dispatcher for the unified pxmx agent.

Handles the synchronous (<15s) cs commands delivered as ``CS_COMMAND``:
start/stop/reboot/snapshot_vm, the batch start_vms/stop_vms/snapshot_vms,
unlock_template, clear_provision_lock, clear_usb_quarantine.

Long ops (delete_vm, reclone_vm, clone_lxc, provision_unassigned, backup,
reseed, update_agent) would blow the pxmx spoke's 15s ``send_to_agent`` window,
so they are NOT handled synchronously here — :func:`handle_cs_command` spawns
them via :mod:`cs_sim.run_long_op` (accepted + streamed ``CS_PROGRESS`` +
terminal ``CS_COMMAND_RESULT``) and immediately returns ``ACCEPTED``.

Every VM-targeted fast action funnels through ``cs_guard`` via ``pve_cmds``.
The host-level actions (unlock_template, clear_provision_lock,
clear_usb_quarantine) carry no vmid guard by design (templates live below the
90000 floor; unlock/lock-clear are non-destructive recovery).

:func:`handle_cs_command` never raises — errors (guard refusals, pve failures)
come back as ``{status: ERROR, message: ...}`` so the hub can ACK them.
"""

# asyncio spawns long-running operations as background tasks.
import asyncio
# logging reports accepted, refused and failed commands.
import logging
# os checks sysfs paths for the live USB probe.
import os
# time.monotonic timestamps entries in the duplicate-command cache.
import time
# uuid generates a command id when the hub did not send one.
import uuid
# Type hints only; they document the shapes of values passed around.
from typing import Any, Dict, Set

# Proxmox command helpers (start/stop/snapshot VMs, unlock templates, ...).
from . import pve_cmds
# Long-op runner and the reclone-stop control.
from . import cs_sim
# USB provisioning state (reclone state, quarantine, exclusions).
from . import usb_provision
# Guard errors (refused VMIDs) and the per-host protected-VMID resolver.
from .cs_guard import GuardError, resolve_protected_vmids
# Error type raised when a Proxmox command fails.
from .pve_cmds import PveError

# Share the main agent logger so command messages appear with other agent output.
logger = logging.getLogger("PxmxAgent")

# How long a long-op's cs_cmd_id is remembered for dedup. The hub re-queues +
# re-delivers a command it hasn't seen a terminal ACK for (e.g. the ACCEPTED
# reply got stuck behind a congested WS under CPU pressure). Without dedup the
# agent spawns a SECOND destroy/reclone task for the same id — duplicate heavy
# work that deepens the overload that caused the re-queue in the first place.
# Long enough to cover several re-queue cycles; pruned so it can't grow.
_CS_CMD_DEDUP_TTL_S = 600.0


def _protected(agent) -> Set[int]:
    """Resolve this host's protected-VMID set from its client_simulation cfg."""
    # Read the host's client_simulation config (may be missing) and resolve its protected set.
    return resolve_protected_vmids(agent.config.get("client_simulation"))


def _template_ids(agent, data: Dict[str, Any]):
    """Template IDs to unlock: command arg first, then usb_config, else none."""
    # Prefer IDs passed with the command.
    ids = data.get("template_ids")
    # Otherwise fall back to the IDs configured in usb_config (either key name).
    if not ids:
        usb_cfg = (agent.config.get("client_simulation") or {}).get("usb_config") or {}
        ids = usb_cfg.get("template_ids") or usb_cfg.get("image_template_ids")
    # No IDs anywhere means nothing to unlock.
    return ids or []


def _force_unbind_present(agent) -> list:
    """Force-release driver-bound dongles from the host kernel driver so they can
    be passed through without a reboot. Skips buses currently assigned to a VM.
    Best-effort — never raises. Shared by both the Clear Quarantine and Clear
    Exclusion actions (the physical release is the same for both)."""
    # Imported lazily to avoid a circular import with usb_provision.
    from . import usb_provision
    # Any failure is logged and treated as "nothing unbound".
    try:
        # Buses already assigned to a VM must not be yanked from it.
        _st = usb_provision.load_usb_state()
        _assigned = set((_st.get("bus_to_vmid") or {}).keys())
        # Release every recognised dongle that is still held by a host driver; return the buses
        # released.
        return usb_provision.force_unbind_dongle_drivers(
            usb_provision._dongle_vidpids(agent), skip_buses=_assigned).get("unbound", [])
    # Unbind failed: log it and report nothing released.
    except Exception as _e:  # noqa: BLE001 — unbind is best-effort
        logger.warning(f"force-unbind failed: {_e}")
        return []


async def handle_cs_command(agent, action: str,
                            data: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a CS_COMMAND. Returns an AGENT_RESPONSE-shaped result dict.

    ``agent`` is the ProxmoxAgent instance (needs ``.cs_enabled`` and
    ``.config``). ``data`` is the inner command payload (``{vmid, ...}``).

    Long ops return ``{status: ACCEPTED, cs_cmd_id}`` and run in the background
    (Phase E); fast ops return their terminal ``SUCCESS``/``ERROR`` here.
    """
    # This host is not a Client-Simulation host: refuse every CS command.
    if not agent.cs_enabled:
        return {"status": "ERROR",
                "message": "client_simulation disabled on this host"}

    # Normalise action names so "start-vm" and "start_vm" are treated the same.
    action = (action or "").replace("-", "_")

    # ── Long ops: spawn + accept immediately (Phase E) ───────────────────────
    if action in cs_sim.LONG_ACTIONS:
        # Use the hub's command id (needed to match results), or make one up.
        cs_cmd_id = data.get("cs_cmd_id") or str(uuid.uuid4())
        # Idempotency + liveness: the hub re-delivers a long op it hasn't seen a
        # terminal result for (congested WS, agent restart, or the cs spoke's
        # verify-timeout requeue). Each seen entry is {ts, accept, task, terminal}:
        #   - task alive  → re-ACK ACCEPTED (don't double-run a running op).
        #   - task done + terminal recorded → return the terminal so the bridge
        #     acks completed/failed instead of dropping the re-deliver.
        #   - task done + NO terminal (task died without emitting CS_COMMAND_RESULT)
        #     → RE-SPAWN. This is what makes a requeued delete actually re-run
        #     instead of dedup-suppressing forever (the old "VM never deleted"
        #     symptom when the agent lost the long op).
        # Per-agent cache of command ids already seen, created on first use.
        seen = getattr(agent, "_cs_seen_cmds", None)
        if seen is None:
            seen = agent._cs_seen_cmds = {}
        # Drop cache entries older than the dedup window so the cache cannot grow forever.
        now = time.monotonic()
        for _k in [k for k, v in seen.items() if now - v.get("ts", 0) > _CS_CMD_DEDUP_TTL_S]:
            seen.pop(_k, None)
        # Has this exact command been delivered before?
        prior = seen.get(cs_cmd_id)
        if prior is not None:
            # The task we started for it, and its final result if one was recorded.
            task = prior.get("task")
            terminal = prior.get("terminal")
            # Still running: re-send the original ACCEPTED reply.
            if task is not None and not task.done():
                logger.info("CS_COMMAND %s DUPLICATE (cs_cmd_id=%s) — re-acking, "
                            "task still running", action, cs_cmd_id)
                return prior["accept"]
            # Already finished with a result: re-send that result.
            if terminal is not None:
                logger.info("CS_COMMAND %s DUPLICATE (cs_cmd_id=%s) — re-acking "
                            "terminal %s", action, cs_cmd_id, terminal.get("status"))
                return terminal
            # Task died with no terminal → re-spawn (real retry).
            logger.warning("CS_COMMAND %s RE-SPAWN (cs_cmd_id=%s) — prior task "
                           "ended with no terminal", action, cs_cmd_id)
        # Start the long operation in the background and track it until it finishes.
        task = asyncio.create_task(cs_sim.run_long_op(agent, action, data, cs_cmd_id))
        agent._cs_long_ops.add(task)
        task.add_done_callback(agent._cs_long_ops.discard)
        # Reply ACCEPTED now and remember the command for dedup.
        logger.info(f"CS_COMMAND {action} accepted (cs_cmd_id={cs_cmd_id})")
        resp = {"status": "ACCEPTED", "message": f"{action} accepted",
                "cs_cmd_id": cs_cmd_id, "action": action}
        seen[cs_cmd_id] = {"ts": now, "accept": resp, "task": task, "terminal": None}
        return resp

    # ── Fast op: stop a running fleet reclone. Must be handled here (not via
    # run_long_op) so it isn't blocked behind the batch holding _LONG_OP_SEM.
    if action == "proxmox_reclone_clear":
        # Fast, local: drops a FINISHED batch's failed-count + log so old errors
        # stop rendering under Reclone All. Refused while a batch is running.
        # Clear the finished batch's error state; refused (False) while a batch is running.
        cleared = usb_provision.clear_reclone_state()
        # Report whether the state was cleared, with a hint when it was refused.
        return {"cleared": bool(cleared),
                "message": ("" if cleared else
                            "a reclone batch is running — stop it before clearing")}

    # Ask any running fleet reclone to stop after its current VM.
    if action == "proxmox_reclone_stop":
        # Report whether there was a reclone to stop.
        stopped = cs_sim.request_reclone_stop()
        return {"status": "SUCCESS", "stopped": stopped,
                "message": "reclone stop requested" if stopped else "no reclone running"}

    # Protected VMIDs used by every VM-targeted command below.
    protected = _protected(agent)

    # Any guard/Proxmox/other error is converted to an ERROR reply in the handlers at the bottom.
    try:
        # Single-VM power/snapshot actions: run the guarded Proxmox command and report success.
        if action == "start_vm":
            r = await pve_cmds.start_vm(data.get("vmid"), protected)
            return {"status": "SUCCESS", "message": f"VM {r['vmid']} started", **r}

        if action == "stop_vm":
            r = await pve_cmds.stop_vm(data.get("vmid"), protected)
            return {"status": "SUCCESS", "message": f"VM {r['vmid']} stopped", **r}

        if action == "reboot_vm":
            r = await pve_cmds.reboot_vm(data.get("vmid"), protected)
            return {"status": "SUCCESS", "message": f"VM {r['vmid']} rebooted", **r}

        if action == "snapshot_vm":
            r = await pve_cmds.snapshot_vm(data.get("vmid"), protected,
                                           name=data.get("snapshot_name"))
            return {"status": "SUCCESS",
                    "message": f"VM {r['vmid']} snapshot {r['snapshot']}", **r}

        # Batch actions over every simulation VM on the host, reporting started/stopped/skipped
        # counts.
        if action == "start_vms":
            r = await pve_cmds.start_vms(protected)
            return {"status": "SUCCESS",
                    "message": f"started {len(r['started'])} VMs "
                               f"(skipped {len(r['skipped'])})", **r}

        if action == "stop_vms":
            r = await pve_cmds.stop_vms(protected)
            return {"status": "SUCCESS",
                    "message": f"stopped {len(r['stopped'])} VMs "
                               f"(skipped {len(r['skipped'])})", **r}

        if action == "snapshot_vms":
            r = await pve_cmds.snapshot_vms(protected)
            return {"status": "SUCCESS",
                    "message": f"snapshotted {len(r['snapshotted'])} VMs "
                               f"(skipped {len(r['skipped'])})", **r}

        # Unlock the configured template VMs; partial failures are still a SUCCESS with counts.
        if action == "unlock_template":
            r = await pve_cmds.unlock_template(_template_ids(agent, data))
            if r["failed"]:
                return {"status": "SUCCESS",
                        "message": f"unlocked {len(r['unlocked'])}, "
                                   f"failed {len(r['failed'])}", **r}
            return {"status": "SUCCESS",
                    "message": f"unlocked {len(r['unlocked'])} template(s)", **r}

        # Kill stuck `qm` processes and clear provisioning locks left on VMs.
        if action == "clear_provision_lock":
            r = await pve_cmds.clear_provision_lock()
            return {"status": "SUCCESS",
                    "message": f"killed {r['killed_qm_pids']} qm pids, "
                               f"unlocked {len(r['unlocked_vmids'])} VMs", **r}

        if action == "usb_probe":
            # LIVE presence probe for one bus path, for the WebUI "Replace
            # Dongle" flow. Deliberately reads sysfs on demand rather than
            # riding the telemetry frame: telemetry idles at 60s (and is now
            # capped at 15s even under load), which is far too slow to tell
            # someone "yes, that is the one you just pulled" while their hand is
            # still in the chassis. This is a handful of file reads.
            # usb_diagnostics knows how to map a bus to its controller and physical location.
            from . import usb_diagnostics as _ud
            # The bus path (e.g. "3-1.2") is required.
            bus = str(data.get("bus_path") or "").strip()
            if not bus:
                return {"status": "ERROR", "message": "bus_path required"}
            # sysfs directory for that bus; it exists only while a device is plugged in there.
            base = f"/sys/bus/usb/devices/{bus}"
            present = os.path.exists(base)

            def _rd(path):
                """Read a small sysfs file, returning "" when it does not exist or cannot be read.
                """
                # sysfs attribute files are tiny one-line values.
                try:
                    with open(path) as f:
                        return f.read().strip()
                # Missing attribute (or device unplugged mid-read).
                except OSError:
                    return ""

            def _mac(d):
                """MAC of the dongle's net interface, '' if it has none here.

                THE identity that actually distinguishes two physically
                different dongles in this fleet. vid:pid is identical (one
                model throughout) and so is the SERIAL -- these sticks ship
                with a shared/blank iSerial, so serial cannot tell a
                replacement from a reseat. A MAC is burned per device.

                Only readable while the dongle is bound HOST-side; once it is
                passed through to a VM the host has no netdev for it, so this
                is '' and the caller must not infer identity from silence.
                """
                # glob finds the net/*/address file wherever the netdev sits under the device.
                import glob as _g
                # Look for a network interface under the device or under one of its interfaces.
                for pat in (f"{d}/*/net/*/address", f"{d}/net/*/address"):
                    for hit in sorted(_g.glob(pat)):
                        # First non-empty MAC wins.
                        v = _rd(hit)
                        if v:
                            return v
                return ""

            def _ident(b):
                """Collect identifying details (vid:pid, product, serial, MAC, devnum) for a USB bus
                path.
                """
                # sysfs directory for this bus, and its USB vendor and product ids.
                d = f"/sys/bus/usb/devices/{b}"
                vid, pid = _rd(f"{d}/idVendor"), _rd(f"{d}/idProduct")
                # Build the identity record; vidpid is only filled when both ids were read.
                return {"bus_path": b,
                        "vidpid": f"{vid}:{pid}" if (vid and pid) else "",
                        "product": _rd(f"{d}/product"),
                        "serial": _rd(f"{d}/serial"),
                        "mac": _mac(d),
                        # devnum increments on EVERY re-enumeration, including a
                        # fault flap, so it proves "this re-enumerated" and
                        # never "this is a different device". Shipped for
                        # evidence, deliberately not used as identity.
                        "devnum": _rd(f"{d}/devnum")}

            # PCI address of the USB controller the bus hangs off (only if something is plugged in).
            ctrl = _ud.usb_device_controller(bus) if present else ""
            # Base reply: presence, physical location and controller.
            out = {"status": "SUCCESS", "action": "usb_probe", "bus": bus,
                   "present": present,
                   "location": _ud.location_for(bus) if present else "",
                   "pci_address": ctrl}
            # Add the device's identity details when present.
            if present:
                out.update(_ident(bus))
            # Everything else currently on the SAME controller, so the UI can
            # say "a new dongle appeared on port 3" and can show the operator
            # which neighbouring ports are still populated -- the difference
            # between pulling the right stick and pulling its neighbour.
            sibs = []
            # Only possible when we know the controller.
            if ctrl:
                # Bus ids on the same root hub share the leading "N-" prefix.
                prefix = bus.split("-", 1)[0] + "-"
                # List every USB device on that root hub, skipping interfaces (names with ':').
                try:
                    for name in sorted(os.listdir("/sys/bus/usb/devices")):
                        if not name.startswith(prefix) or ":" in name:
                            continue
                        sibs.append(_ident(name))
                # sysfs listing failed: just return no siblings.
                except OSError:
                    pass
            # Attach the sibling list and return the probe result.
            out["siblings"] = sibs
            return out

        if action == "clear_usb_quarantine":
            # Clear the dmesg quarantine store (usb_quarantine.json, incl. the
            # 5-strike permanent flag) + force-release driver-bound dongles. Leaves
            # the destroy-fail exclusion list intact. No bus_path = clear all.
            from . import usb_provision
            # Optional single bus; None clears every bus.
            bus = data.get("bus_path")
            # Clear the quarantine and physically release any driver-bound dongles.
            usb_provision.clear_quarantine(bus)
            unbound = _force_unbind_present(agent)
            # Build a human-readable summary of what was done.
            msg = (f"cleared quarantine{(' for ' + bus) if bus else ''}"
                   + (f" + force-unbound {len(unbound)} driver-bound dongle(s)"
                      if unbound else ""))
            # Report success with the details.
            return {"status": "SUCCESS", "action": "clear_usb_quarantine",
                    "bus": bus, "cleared": True, "unbound": unbound, "message": msg}

        if action == "clear_usb_history":
            # Purge the missing-dongle HISTORY: the presence roster + the boot
            # baseline. For after a deliberate hardware change (dongles moved,
            # ports rewired, a controller card pulled) where the recorded history
            # describes a machine that no longer exists and would otherwise
            # report permanent phantom losses. Distinct from clear_usb_quarantine
            # (fault state) and clear_usb_exclusions (destroy-fail bookkeeping):
            # this only forgets what USED to be attached. The next pass rebuilds
            # both files from what is actually there, and the boot baseline is
            # re-captured mid-boot — so it is marked untrusted until the next
            # reboot rather than pretending to be a boot snapshot.
            from . import usb_diagnostics
            # Delete the roster and boot baseline files; returns what was purged.
            r = usb_diagnostics.purge_history()
            # Reflect the purge in telemetry IMMEDIATELY. The WebUI missing-dongle
            # panel reads the CACHED _last_usb_diag, which _slow_jobs_loop only
            # recomputes every _USB_DIAG_INTERVAL_S (300s) — unlike the quarantine
            # / exclusion lists, which ride the fast telemetry loop. Without this
            # the purge appears to do nothing for up to 5 minutes even on a page
            # refresh (the reported "not deleting the dongles from the UI" bug).
            # Scrub the roster/baseline-derived loss fields now (they are exactly
            # what purge_history just deleted, so [] is accurate) and force a
            # fresh recompute on the next slow-jobs pass (≤15s) to re-learn from
            # what is actually attached.
            _d = getattr(agent, "_last_usb_diag", None)
            # Blank the loss lists in the cached diagnostics so the UI updates straight away.
            if isinstance(_d, dict):
                for _k in ("missing", "lost_since_boot", "passed_through",
                           "boot_passthrough", "lost_transient",
                           "missing_transient", "causes"):
                    if _k in _d:
                        _d[_k] = []
                _d["known_count"] = 0
                _d["boot_baseline"] = {}
            # Force the slow-jobs loop to recompute diagnostics on its next pass.
            agent._last_usb_diag_ts = 0.0
            # Report what was purged (or that there was nothing to purge).
            n = len(r.get("purged") or [])
            return {"status": "SUCCESS", "action": "clear_usb_history", **r,
                    "message": (f"purged dongle history ({', '.join(r['purged'])})"
                                if n else "no dongle history to purge")}

        if action == "clear_usb_exclusions":
            # Clear the destroy-fail bus EXCLUSIONS (usb_state.json) — what repeated
            # spin-up/down teardowns trip — + release STALE assignments (a bus stuck
            # 'assigned' to a VM that no longer holds the dongle: the "no eligible
            # dongles (assigned=[...])" stall) + force-release driver-bound dongles.
            # Leaves the quarantine (QT) list intact.
            # Imported lazily to avoid circular imports.
            from . import usb_provision, pve_cmds
            # Clear every bus exclusion, counting how many were cleared.
            excluded = usb_provision.clear_excluded_buses()
            # Release assignments whose VM no longer exists; failures are logged and ignored.
            stale = 0
            try:
                _existing = await pve_cmds.list_all_vmids()
                stale = await usb_provision.release_stale_assignments(_existing)
            # Stale-assignment release failed: log and carry on with the rest.
            except Exception as _e:  # noqa: BLE001 — best-effort
                logger.warning(f"clear_usb_exclusions: release_stale_assignments failed: {_e}")
            # Release any driver-bound dongles.
            unbound = _force_unbind_present(agent)
            # Build a human-readable summary of everything that was done.
            msg = (f"cleared {excluded} bus exclusion(s)"
                   + (f" + released {stale} stale assignment(s)" if stale else "")
                   + (f" + force-unbound {len(unbound)} driver-bound dongle(s)"
                      if unbound else "")
                   + " — dongles available on the next provision pass")
            # Report success with the counts.
            return {"status": "SUCCESS", "action": "clear_usb_exclusions",
                    "excluded_cleared": excluded, "stale_released": stale,
                    "unbound": unbound, "message": msg}

        # Anything else is an action this agent does not know.
        return {"status": "ERROR", "message": f"unknown CS action: {action}"}

    # The guard refused the VMID (protected or outside the simulation range).
    except GuardError as e:
        logger.warning(f"CS_COMMAND {action} refused by guard: {e}")
        return {"status": "ERROR", "message": str(e)}
    # The Proxmox command itself failed.
    except PveError as e:
        logger.warning(f"CS_COMMAND {action} failed: {e}")
        return {"status": "ERROR", "message": f"{action} failed: {e}"}
    # Any unexpected bug: log the full traceback but still reply with an ERROR.
    except Exception as e:  # noqa: BLE001 — never let a CS command kill the dispatch loop
        logger.exception(f"CS_COMMAND {action} error")
        return {"status": "ERROR", "message": f"{action} error: {e}"}