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

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional, Set

from . import pve_cmds
from . import cs_sim
from .cs_guard import GuardError, resolve_protected_vmids
from .pve_cmds import PveError

logger = logging.getLogger("PxmxAgent")


def _protected(agent) -> Set[int]:
    """Resolve this host's protected-VMID set from its client_simulation cfg."""
    return resolve_protected_vmids(agent.config.get("client_simulation"))


def _template_ids(agent, data: Dict[str, Any]):
    """Template IDs to unlock: command arg first, then usb_config, else none."""
    ids = data.get("template_ids")
    if not ids:
        usb_cfg = (agent.config.get("client_simulation") or {}).get("usb_config") or {}
        ids = usb_cfg.get("template_ids") or usb_cfg.get("image_template_ids")
    return ids or []


async def handle_cs_command(agent, action: str,
                            data: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a CS_COMMAND. Returns an AGENT_RESPONSE-shaped result dict.

    ``agent`` is the ProxmoxAgent instance (needs ``.cs_enabled`` and
    ``.config``). ``data`` is the inner command payload (``{vmid, ...}``).

    Long ops return ``{status: ACCEPTED, cs_cmd_id}`` and run in the background
    (Phase E); fast ops return their terminal ``SUCCESS``/``ERROR`` here.
    """
    if not agent.cs_enabled:
        return {"status": "ERROR",
                "message": "client_simulation disabled on this host"}

    action = (action or "").replace("-", "_")

    # ── Long ops: spawn + accept immediately (Phase E) ───────────────────────
    if action in cs_sim.LONG_ACTIONS:
        cs_cmd_id = data.get("cs_cmd_id") or str(uuid.uuid4())
        task = asyncio.create_task(cs_sim.run_long_op(agent, action, data, cs_cmd_id))
        agent._cs_long_ops.add(task)
        task.add_done_callback(agent._cs_long_ops.discard)
        logger.info(f"CS_COMMAND {action} accepted (cs_cmd_id={cs_cmd_id})")
        return {"status": "ACCEPTED", "message": f"{action} accepted",
                "cs_cmd_id": cs_cmd_id, "action": action}

    protected = _protected(agent)

    try:
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

        if action == "unlock_template":
            r = await pve_cmds.unlock_template(_template_ids(agent, data))
            if r["failed"]:
                return {"status": "SUCCESS",
                        "message": f"unlocked {len(r['unlocked'])}, "
                                   f"failed {len(r['failed'])}", **r}
            return {"status": "SUCCESS",
                    "message": f"unlocked {len(r['unlocked'])} template(s)", **r}

        if action == "clear_provision_lock":
            r = await pve_cmds.clear_provision_lock()
            return {"status": "SUCCESS",
                    "message": f"killed {r['killed_qm_pids']} qm pids, "
                               f"unlocked {len(r['unlocked_vmids'])} VMs", **r}

        if action == "clear_usb_quarantine":
            r = await pve_cmds.clear_usb_quarantine(data.get("bus_path"))
            return {"status": "SUCCESS", **r}

        return {"status": "ERROR", "message": f"unknown CS action: {action}"}

    except GuardError as e:
        logger.warning(f"CS_COMMAND {action} refused by guard: {e}")
        return {"status": "ERROR", "message": str(e)}
    except PveError as e:
        logger.warning(f"CS_COMMAND {action} failed: {e}")
        return {"status": "ERROR", "message": f"{action} failed: {e}"}
    except Exception as e:  # noqa: BLE001 — never let a CS command kill the dispatch loop
        logger.exception(f"CS_COMMAND {action} error")
        return {"status": "ERROR", "message": f"{action} error: {e}"}