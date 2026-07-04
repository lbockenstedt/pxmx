"""Client-Simulation long-op implementations for the unified pxmx agent (Phase E).

The synchronous (<15s) cs commands live in ``cs_commands``. The long ops —
``delete_vm``, ``reclone_vm``, ``clone_lxc``, ``provision_unassigned``,
``backup``, ``reseed``, ``update_agent`` — would blow the pxmx spoke's 15s
``send_to_agent`` window, so they use the **accepted + streamed-progress +
terminal-result** pattern instead:

  1. ``cs_commands.handle_cs_command`` sees the action is a long op, spawns
     :func:`run_long_op` as a task on ``agent._cs_long_ops``, and immediately
     returns ``{status: ACCEPTED, cs_cmd_id}``. The LM hub's bridge leaves the
     cs queue command ``delivered`` (no ack yet).
  2. The task streams ``CS_PROGRESS`` frames (starting/running/completed/failed
     + step + pct) up to the hub, which relays them to the cs spoke.
  3. The task emits a terminal ``CS_COMMAND_RESULT`` carrying ``cs_cmd_id`` +
     ``completed|failed``; the hub relays it as ``CS_INGEST_COMMAND_RESULT``,
     and the cs spoke acks the queue command (closing the deferred loop).

Every VM-targeted op funnels through ``cs_guard.assert_sim_vm`` (execution-layer
safeguard). ``reseed`` is intentionally unguarded — it targets a template VMID
(the clone source), like ``unlock_template``.

:func:`run_long_op` never raises to its caller: a handler exception becomes a
terminal ``failed`` result so the cs queue command is always acked.
"""

import asyncio
import os
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from . import pve_cmds
from . import usb_provision
from .cs_guard import GuardError, assert_sim_vm, resolve_protected_vmids
from .pve_cmds import PveError

logger = logging.getLogger("PxmxAgent")

# Actions handled here (the accepted+progress pattern), not in cs_commands.
LONG_ACTIONS = frozenset({
    "delete_vm", "reclone_vm", "clone_lxc", "provision_unassigned",
    "backup", "reseed", "update_agent",
})


def _protected(agent) -> set:
    return resolve_protected_vmids(agent.config.get("client_simulation"))


def _usb_cfg(agent) -> Dict[str, Any]:
    return (agent.config.get("client_simulation") or {}).get("usb_config") or {}


# ── progress / terminal event helpers ──────────────────────────────────────


async def _progress(agent, cs_cmd_id: str, action: str, status: str,
                    step: str, pct: Optional[int] = None,
                    message: Optional[str] = None, **extra) -> None:
    """Emit a CS_PROGRESS frame. ``status`` ∈ starting/running/completed/failed."""
    data: Dict[str, Any] = {"cs_cmd_id": cs_cmd_id, "action": action,
                            "status": status, "step": step}
    if pct is not None:
        data["pct"] = max(0, min(100, int(pct)))
    if message:
        data["message"] = message
    data.update(extra)
    await agent.send_cs_event("CS_PROGRESS", data)


async def _terminal(agent, cs_cmd_id: str, action: str, status: str,
                     message: str, **extra) -> None:
    """Emit the terminal CS_COMMAND_RESULT. ``status`` ∈ completed/failed — the
    cs spoke maps these onto ``ack_command(completed|failed)``."""
    data: Dict[str, Any] = {"cs_cmd_id": cs_cmd_id, "action": action,
                            "status": status, "message": message}
    data.update(extra)
    await agent.send_cs_event("CS_COMMAND_RESULT", data)


# ── core destroy (shared by delete_vm / reclone / provision-loop teardown) ──


async def _expire_pending_commands(agent, vmid: int, kind: str) -> None:
    """Best-effort: purge any commands still queued for this VM's guest
    hostname BEFORE destroying it (bash ``_expire_vm_pending_commands`` +
    ``destroy_vm``, proxmox-agent.sh:2138-2157). Without this a stale command
    (e.g. a queued reboot) sits in the cs spoke's inbox and gets delivered to
    whatever guest reuses this vmid slot next, causing a surprise reboot on a
    brand-new VM. The spoke's ``CS_CLEAR_COMMANDS`` handler already supports
    a ``target``-scoped purge for exactly this (cs_spoke.py's comment there
    literally calls out "pre-teardown-expire") — it was just never invoked
    from the agent side. Relayed hub-side via the CS_* ingest map (main.py
    ``_CS_INGEST_MAP``); never raises — a failed purge must not block the
    destroy that follows."""
    try:
        cfg = await (pve_cmds.pct_config(vmid) if kind == "lxc" else pve_cmds.qm_config(vmid))
        hostname = cfg.get("hostname" if kind == "lxc" else "name")
        if hostname:
            await agent.send_cs_event("CS_EXPIRE_PENDING_COMMANDS", {"target": hostname})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"destroy_vm {vmid}: pending-command expire failed: {e}")


async def destroy_vm(agent, vmid: Any, *, bus: Optional[str] = None,
                     protected: Optional[set] = None,
                     exclude_bus_after: bool = False) -> Dict[str, Any]:
    """Stop + destroy a sim VM (bash ``_destroy_guest_only`` 1740-1816 +
    ``destroy_vm`` 2147-2183). Returns ``{ok, orphaned, bus, kind, fails?}``.

    On success the bus assignment + destroy-fail counter + orphan entry are
    cleared. On failure the destroy-fail count increments; at ``DESTROY_MAX_FAILS``
    the VM is declared an orphan (bus released for re-provisioning). Used by the
    ``delete_vm`` long-op, the reclone flow, and the USB provision loop's
    missing-dongle teardown (which passes ``bus`` from state).

    ``exclude_bus_after``: mirrors bash's ``destroy_vm "$vmid" "" "1" "1"``
    (the ``exclude_bus=1`` arg, proxmox-agent.sh:4066-4073) — an explicit
    admin delete (not a reclone, not the provision loop's own missing-dongle
    teardown) marks the bus excluded so the NEXT provision tick doesn't just
    reclone a fresh VM for the same still-plugged-in dongle. Without this the
    delete_vm long-op cleared the assignment and nothing else, so a dongle
    that was never unplugged got silently reprovisioned within one tick.
    The exclusion self-clears once the dongle is actually unplugged
    (usb_provision's reconcile step, "Clear exclusions ... for buses no
    longer present") — it is not a permanent ban.
    """
    prot = protected if protected is not None else _protected(agent)
    vid = assert_sim_vm(vmid, prot)  # GuardError if invalid → caller handles
    kind = await pve_cmds.detect_guest_type(vid)
    bus = bus or usb_provision.bus_for_vmid(vid)
    await _expire_pending_commands(agent, vid, kind)
    if kind == "lxc":
        await pve_cmds.pct_stop(vid, prot)
        ok = await pve_cmds.pct_destroy(vid, prot)
    else:
        await pve_cmds.qm_stop_force(vid, prot)
        ok = await pve_cmds.qm_destroy(vid, prot)
    if ok:
        usb_provision.clear_assignment(vid, bus)
        usb_provision.clear_destroy_fails(vid)
        usb_provision.remove_orphan_vm(vid)
        if exclude_bus_after and bus:
            usb_provision.exclude_bus(bus)
        return {"ok": True, "orphaned": False, "bus": bus, "kind": kind}
    res = usb_provision.record_destroy_fail(vid, bus or "")
    if res["orphaned"]:
        usb_provision.clear_assignment(vid, bus)
        if exclude_bus_after and bus:
            usb_provision.exclude_bus(bus)
    return {"ok": False, "orphaned": res["orphaned"], "bus": bus,
            "kind": kind, "fails": res["count"]}


# ── per-action handlers ────────────────────────────────────────────────────


async def _delete_vm(agent, data, cs_cmd_id) -> None:
    vmid = data.get("vmid") or data.get("vm_id")
    prot = _protected(agent)
    vid = assert_sim_vm(vmid, prot)  # raises → run_long_op emits terminal failed
    await _progress(agent, cs_cmd_id, "delete_vm", "running", "stopping", 10, vmid=vid)
    r = await destroy_vm(agent, vid, protected=prot, exclude_bus_after=True)
    if r["ok"]:
        await _terminal(agent, cs_cmd_id, "delete_vm", "completed",
                        f"VM {vid} destroyed", vmid=vid, kind=r["kind"])
    else:
        msg = (f"VM {vid} destroy failed — declared orphan (bus released)"
               if r["orphaned"] else
               f"VM {vid} destroy failed (attempt {r.get('fails')}/3)")
        await _terminal(agent, cs_cmd_id, "delete_vm", "failed", msg,
                        vmid=vid, orphaned=r["orphaned"])


async def _reclone_vm(agent, data, cs_cmd_id) -> None:
    vmid = data.get("vmid") or data.get("vm_id")
    source_vmid = data.get("source_vmid") or data.get("template_id")
    prot = _protected(agent)
    vid = assert_sim_vm(vmid, prot)

    # Recover the USB bus path: state first, then `qm config` usb0 host= (bash 2436-2451).
    bus = usb_provision.bus_for_vmid(vid)
    if not bus:
        cfg = await pve_cmds.qm_config(vid)
        for k, v in cfg.items():
            if k.startswith("usb") and "host=" in v:
                bus = v.split("host=", 1)[1].split(",", 1)[0].strip()
                break
    if not bus or not os.path.isdir(f"/sys/bus/usb/devices/{bus}"):
        await _terminal(agent, cs_cmd_id, "reclone_vm", "failed",
                        f"no present USB bus for VM {vid}", vmid=vid)
        return

    state = usb_provision.load_usb_state()
    image_num = int(state["vmid_to_image"].get(str(vid), 1) or 1)
    usb_cfg = _usb_cfg(agent)
    template = source_vmid or (usb_cfg.get("image1_template_id") if image_num == 1
                               else usb_cfg.get("image2_template_id")) \
        or usb_cfg.get("image1_template_id")
    if not template:
        await _terminal(agent, cs_cmd_id, "reclone_vm", "failed",
                        "no template id configured", vmid=vid)
        return

    await _progress(agent, cs_cmd_id, "reclone_vm", "running", "destroying", 20, vmid=vid)
    dr = await destroy_vm(agent, vid, bus=bus, protected=prot)
    if not dr["ok"] and not dr["orphaned"]:
        await _terminal(agent, cs_cmd_id, "reclone_vm", "failed",
                        f"destroy before reclone failed (attempt {dr.get('fails')}/3)",
                        vmid=vid)
        return

    name = f"sim-{vid}"
    await _progress(agent, cs_cmd_id, "reclone_vm", "running", "cloning", 40, vmid=vid)
    await pve_cmds.qm_clone(template, vid, name, protected=prot, timeout=600)
    await pve_cmds.qm_set(vid, "--onboot", "1", "--startup", "order=2,up=60", protected=prot)
    await pve_cmds.qm_set(vid, "-usb0", f"host={bus}", protected=prot)
    vlan = usb_cfg.get("vlan_nic")
    if vlan:
        await pve_cmds.qm_set(vid, "-net0", f"virtio,bridge={vlan}", protected=prot)

    await _progress(agent, cs_cmd_id, "reclone_vm", "running", "starting", 60, vmid=vid)
    await pve_cmds.qm_start(vid, prot)

    await _progress(agent, cs_cmd_id, "reclone_vm", "running", "waiting_guest_agent", 75, vmid=vid)
    # Bash cadence: `timeout 10` ping + `sleep 5` ≈ 15s/iter, 40× ≈ 10 min
    # (proxmox-agent.sh 1999-2009). Use sleep(5), not 15, to match.
    for _ in range(40):
        if await pve_cmds.qm_agent_ping(vid, protected=prot):
            break
        await asyncio.sleep(5)
    # Set the hostname inside the guest. Write /etc/hostname + /etc/hosts +
    # cloud-init preserve_hostname via `qm guest exec --timeout 60 -- bash -c`
    # (bash 2025-2036) — hostnamectl is deliberately avoided: it talks D-Bus
    # which may be unready right after boot and can hang the whole task.
    host_script = (
        f"echo '{name}' > /etc/hostname; "
        f"sed -i 's/^127\\.0\\.1\\.1.*/127.0.1.1\\t{name}/' /etc/hosts 2>/dev/null || true; "
        "mkdir -p /etc/cloud/cloud.cfg.d; "
        "echo 'preserve_hostname: true' > /etc/cloud/cloud.cfg.d/99_preserve_hostname.cfg; "
        "rm -f /var/lib/cloud/sem/config_set_hostname 2>/dev/null || true"
    )
    await pve_cmds.qm_guest_exec_shell(vid, host_script, exec_timeout=60,
                                        outer_timeout=90, protected=prot)

    usb_provision.set_assignment(vid, bus, image_num)
    usb_provision.remove_orphan_vm(vid)
    await _terminal(agent, cs_cmd_id, "reclone_vm", "completed",
                    f"VM {vid} recloned on {bus}", vmid=vid,
                    result={"bus": bus, "image": image_num})


async def _clone_lxc(agent, data, cs_cmd_id) -> None:
    vmid = data.get("vmid") or data.get("vm_id")
    source_vmid = data.get("source_vmid") or data.get("template_id")
    prot = _protected(agent)
    vid = assert_sim_vm(vmid, prot)
    if not source_vmid:
        await _terminal(agent, cs_cmd_id, "clone_lxc", "failed",
                        "no source_vmid provided", vmid=vid)
        return

    # Snapshot the config keys to reapply after clone (bash 1822-1845).
    cfg = await pve_cmds.pct_config(vid)
    reapply = []
    for key in ("onboot", "startup", "cores", "memory", "swap", "features",
                "protection", "tags", "description", "nameserver", "searchdomain",
                "unprivileged"):
        if cfg.get(key):
            reapply += [f"--{key}", cfg[key]]
    for k, v in cfg.items():
        if k.startswith("net") and v:
            reapply += [f"--{k}", v]
    ct_name = cfg.get("hostname") or usb_provision._vm_name(vid) or f"ct-{vid}"

    await _progress(agent, cs_cmd_id, "clone_lxc", "running", "destroying", 20, vmid=vid)
    await pve_cmds.pct_stop(vid, prot)
    if not await pve_cmds.pct_destroy(vid, prot):
        await _terminal(agent, cs_cmd_id, "clone_lxc", "failed",
                        f"destroy of CT {vid} before clone failed", vmid=vid)
        return

    await _progress(agent, cs_cmd_id, "clone_lxc", "running", "cloning", 50, vmid=vid)
    await pve_cmds.pct_clone(source_vmid, vid, ct_name, protected=prot, timeout=600)
    if reapply:
        await pve_cmds.pct_set(vid, *reapply, protected=prot)

    await _progress(agent, cs_cmd_id, "clone_lxc", "running", "starting", 80, vmid=vid)
    await pve_cmds.pct_start(vid, prot)
    await _terminal(agent, cs_cmd_id, "clone_lxc", "completed",
                    f"CT {vid} recloned from {source_vmid}", vmid=vid)


async def _provision_unassigned(agent, data, cs_cmd_id) -> None:
    # Unconditionally clear all bus exclusions, then run one provision pass
    # (bash ``provision_unassigned`` dispatch 4078-4084 + ``usb_provision_loop``).
    cleared = usb_provision.clear_excluded_buses()
    await _progress(agent, cs_cmd_id, "provision_unassigned", "running", "provisioning", 30,
                    cleared_exclusions=cleared)
    try:
        r = await usb_provision.run_provision_loop(agent)
    except Exception as e:  # noqa: BLE001
        await _terminal(agent, cs_cmd_id, "provision_unassigned", "failed",
                        f"provision loop error: {e}")
        return
    await _terminal(agent, cs_cmd_id, "provision_unassigned", "completed",
                    f"provisioned {r.get('provisioned', 0)} VM(s), "
                    f"tore down {r.get('torn_down', 0)}", result=r)


async def _backup(agent, data, cs_cmd_id) -> None:
    import shutil
    vm_ids = data.get("vm_ids") or data.get("vmids") or []
    if isinstance(vm_ids, (str, int)):
        vm_ids = [vm_ids]
    job_id = data.get("job_id")
    azure_account = data.get("azure_account")
    azure_container = data.get("azure_container")
    azure_key = data.get("azure_key")
    retention = data.get("retention")
    spoke_id = data.get("spoke_id") or agent.agent_id
    prot = _protected(agent)

    if not vm_ids or not azure_account or not azure_container or not azure_key:
        await _terminal(agent, cs_cmd_id, "backup", "failed",
                        "missing vm_ids / azure_account / azure_container / azure_key")
        return
    if not shutil.which("azcopy"):
        await _terminal(agent, cs_cmd_id, "backup", "failed",
                        "azcopy not installed on this host")
        return

    overall = True
    # azcopy reads the Azure creds from the env (bash 3877-3878 exports
    # AZCOPY_ACCOUNT_NAME/AZCOPY_ACCOUNT_KEY). Forward them so the upload
    # authenticates — without them azcopy fails with an auth error.
    az_env = dict(os.environ)
    az_env["AZCOPY_ACCOUNT_NAME"] = str(azure_account)
    az_env["AZCOPY_ACCOUNT_KEY"] = str(azure_key)
    for vmid in vm_ids:
        try:
            vid = assert_sim_vm(vmid, prot)
        except GuardError as e:
            await _progress(agent, cs_cmd_id, "backup", "failed", "validate", 0,
                            vmid=vmid, error=str(e))
            overall = False
            continue
        dump_dir = f"/tmp/cs-backup/{vid}"
        # Clean any stale artifact from a crashed prior run before writing
        # (bash 3882: rm -rf then mkdir), so sorted(...)[-1] picks up only this run.
        _cleanup(dump_dir)
        os.makedirs(dump_dir, exist_ok=True)
        await _progress(agent, cs_cmd_id, "backup", "starting", "starting", 0,
                        vmid=vid, job_id=job_id)
        await _progress(agent, cs_cmd_id, "backup", "running", "vzdump", 15,
                        vmid=vid, job_id=job_id)
        try:
            await pve_cmds.vzdump(vid, dump_dir, protected=prot)
        except PveError as e:
            await _progress(agent, cs_cmd_id, "backup", "failed", "vzdump", 100,
                            vmid=vid, job_id=job_id, error=str(e))
            overall = False
            _cleanup(dump_dir)
            continue
        files = sorted(f for f in os.listdir(dump_dir)
                       if os.path.isfile(os.path.join(dump_dir, f)))
        if not files:
            await _progress(agent, cs_cmd_id, "backup", "failed", "locate_backup", 100,
                            vmid=vid, job_id=job_id, error="no backup artifact produced")
            overall = False
            _cleanup(dump_dir)
            continue
        backup_file = os.path.join(dump_dir, files[-1])
        dest = (f"https://{azure_account}.blob.core.windows.net/{azure_container}"
                f"/{spoke_id}/{vid}/{os.path.basename(backup_file)}")
        await _progress(agent, cs_cmd_id, "backup", "running", "uploading", 80,
                        vmid=vid, job_id=job_id, spoke_id=spoke_id)
        rc, _, _ = await pve_cmds._run(
            ["azcopy", "copy", backup_file, dest, "--overwrite=true"],
            check=False, timeout=3600, env=az_env)
        if rc != 0:
            await _progress(agent, cs_cmd_id, "backup", "failed", "uploading", 100,
                            vmid=vid, job_id=job_id, error="azcopy upload failed")
            overall = False
            _cleanup(dump_dir)
            continue
        await _progress(agent, cs_cmd_id, "backup", "completed", "completed", 100,
                        vmid=vid, job_id=job_id)
        _cleanup(dump_dir)

    status = "completed" if overall else "failed"
    await _terminal(agent, cs_cmd_id, "backup", status,
                    "backup job complete" if overall else "backup job finished with errors",
                    job_id=job_id, vm_ids=list(vm_ids))


async def _reseed(agent, data, cs_cmd_id) -> None:
    """Reseed a template: download a .vma.zst → qmrestore → qm template (bash
    ``run_reseed_command`` 3931-3978). The target VMID is a *template* (the
    clone source, below the 90000 floor), so this is intentionally unguarded —
    like ``unlock_template``. The legacy ``clone.sh`` step is retired in the
    unified agent (clone logic now lives in ``usb_provision._clone_and_provision``)."""
    import shutil
    blob_url = data.get("blob_url")
    vm_id = int(data.get("vm_id") or data.get("vmid") or 100)
    job_id = data.get("job_id")
    download = f"/tmp/reseed-vm-{vm_id}.vma.zst"
    # Concurrency lock (bash RESEED_LOCK_FILE): a reseed is a heavy single-host
    # op (download + restore + template); refuse a second concurrent one so
    # they don't collide on the same download path / VMID.
    lock = "/tmp/.proxmox_reseed_lock"

    if not blob_url:
        await _terminal(agent, cs_cmd_id, "reseed", "failed",
                        "no blob_url supplied (download+restore required)")
        return
    if os.path.exists(lock):
        await _terminal(agent, cs_cmd_id, "reseed", "failed",
                        "a reseed is already in progress", vmid=vm_id, job_id=job_id)
        return
    try:
        Path(lock).touch()
    except OSError:
        pass  # proceed best-effort; the lock is advisory

    try:
        await _progress(agent, cs_cmd_id, "reseed", "starting", "starting", 0,
                        vmid=vm_id, job_id=job_id)
        await _progress(agent, cs_cmd_id, "reseed", "running", "downloading", 15,
                        vmid=vm_id, job_id=job_id)
        if not shutil.which("curl"):
            await _terminal(agent, cs_cmd_id, "reseed", "failed",
                            "curl not installed", vmid=vm_id)
            return
        rc, _, _ = await pve_cmds._run(["curl", "-L", "-o", download, blob_url],
                                        check=False, timeout=600)
        if rc != 0:
            await _terminal(agent, cs_cmd_id, "reseed", "failed",
                            "download failed", vmid=vm_id, job_id=job_id)
            _cleanup(download)
            return

        await _progress(agent, cs_cmd_id, "reseed", "running", "restoring", 45,
                        vmid=vm_id, job_id=job_id)
        rc, _, err = await pve_cmds._run(["qmrestore", download, str(vm_id), "--force"],
                                         check=False, timeout=600)
        if rc != 0:
            await _terminal(agent, cs_cmd_id, "reseed", "failed",
                            f"qmrestore failed: {err.decode().strip()[:200]}",
                            vmid=vm_id, job_id=job_id)
            _cleanup(download)
            return

        await _progress(agent, cs_cmd_id, "reseed", "running", "templating", 75,
                        vmid=vm_id, job_id=job_id)
        rc, _, err = await pve_cmds._run(["qm", "template", str(vm_id)],
                                         check=False, timeout=120)
        _cleanup(download)
        if rc != 0:
            await _terminal(agent, cs_cmd_id, "reseed", "failed",
                            f"qm template failed: {err.decode().strip()[:200]}",
                            vmid=vm_id, job_id=job_id)
            return
        await _terminal(agent, cs_cmd_id, "reseed", "completed",
                        f"reseed complete for template {vm_id}", vmid=vm_id, job_id=job_id)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


async def _update_agent(agent, data, cs_cmd_id) -> None:
    """Trigger an immediate self-update (pull + sync + restart). The agent's
    ``_apply_update`` os._exit(0)s after the restart, so the terminal result is
    only reached when there was no new version to apply."""
    await _progress(agent, cs_cmd_id, "update_agent", "running", "applying_update", 50)
    try:
        await agent.trigger_update()
    except Exception as e:  # noqa: BLE001
        await _terminal(agent, cs_cmd_id, "update_agent", "failed",
                        f"update failed: {e}")
        return
    await _terminal(agent, cs_cmd_id, "update_agent", "completed",
                    "update applied; agent restarting (or already current)")


_HANDLERS = {
    "delete_vm": _delete_vm,
    "reclone_vm": _reclone_vm,
    "clone_lxc": _clone_lxc,
    "provision_unassigned": _provision_unassigned,
    "backup": _backup,
    "reseed": _reseed,
    "update_agent": _update_agent,
}


async def run_long_op(agent, action: str, data: Dict[str, Any],
                      cs_cmd_id: str) -> None:
    """Dispatch a long op as a background task. Emits a terminal
    CS_COMMAND_RESULT for every outcome (success, logical failure, or an
    unexpected exception) so the cs queue command is always acked."""
    handler = _HANDLERS.get(action)
    if not handler:
        await _terminal(agent, cs_cmd_id, action, "failed",
                        f"unknown long op: {action}")
        return
    try:
        await handler(agent, data, cs_cmd_id)
    except asyncio.CancelledError:
        # Best-effort: tell the cs spoke the op was abandoned so the queue
        # command doesn't sit delivered forever.
        await _terminal(agent, cs_cmd_id, action, "failed",
                        f"{action} cancelled")
        raise
    except GuardError as e:
        logger.warning(f"long op {action} refused by guard: {e}")
        await _terminal(agent, cs_cmd_id, action, "failed", str(e))
    except Exception as e:  # noqa: BLE001 — never let a long op vanish silently
        logger.exception(f"long op {action} error")
        await _terminal(agent, cs_cmd_id, action, "failed", f"{action} error: {e}")


def _cleanup(path: str) -> None:
    """Remove a tmp file/dir, ignoring errors (bash 3925/3978 cleanup)."""
    try:
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        os.remove(os.path.join(root, f))
                    except OSError:
                        pass
            os.rmdir(path)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass