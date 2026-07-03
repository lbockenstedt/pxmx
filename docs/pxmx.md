# pxmx — Proxmox (hypervisor)

Proxmox bridge spoke + per-host agent. Repo: `pxmx`. `module_type = "hypervisor"`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

A **bridge spoke**: connects the LM hub to one or more **pxmx host agents** running on Proxmox nodes. The spoke never touches Proxmox — all `qm`/`pct` work happens in the agent. The agent also hosts the Client-Sim auto-provisioning **brain** (`agent/src/usb_provision.py`). Documented in `pxmx/ARCHITECTURE.md`.

## Entrypoints

- **Spoke:** `python3 -m src.control_plane` (`PxmxControlPlane`), systemd `lm-pxmx.service`, `User=svc_lm`. Installer `install_pxmx.sh` (clones to `/opt/lm/pxmx`, `.env`, `lm-pxmx.service`, self-update rollback watchdog + sudoers, generates `agent_secret` at `/etc/lm-agent/config.json`).
- **Agent:** `python3 -m src.agent` (`ProxmoxAgent`), systemd `lm-pxmx-agent.service`, `User=root`, `WatchdogSec=60` + `NotifyAccess=main`. Installer `agent/install_agent.sh` (clones to `/opt/lm/pxmx/agent`, net-watchdog timer, kernel hang/panic sysctls, softdog, kdump-tools).
- Other: `agent/retire_bash_agent.sh`, `agent/uninstall_agent.sh`, `agent/lm-pxmx-net-watchdog.{service,timer,sh}`.

## Ports

- Spoke dials hub on **443** (`/ws/spoke`, wss).
- Agent listener: `LM_PXMX_AGENT_PORT` — default **8443** loopback all-in-one (`LM_PXMX_AGENT_LOOPBACK=1`; the hub `/ws/agent` byte-proxy dials it — NOT advertised externally); **443** wss standalone pxmx spoke; legacy **8766** plaintext. mDNS TXT `agent_port` advertises the **external** dial port (**443** on both deployments) → agents auto-discover `wss://<hub>:443/ws/agent`.
- mDNS browses `_lm-hub._tcp.local.` (TXT `tls_port` + `agent_port`); DNS `lm-hub.<search>`.

## Environment variables

- Spoke `.env`: `HUB_URL`, `SPOKE_ID`, `SPOKE_SECRET`, `HUB_SECRET`, `LM_TLS_CERT`, `LM_TLS_KEY`, `LM_PXMX_AGENT_PORT`, `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`.
- Spoke process: `LM_DEP_GUARD_DISABLE`, `LM_PXMX_STATE_DIR` (`/var/lib/pxmx/update-state`), `LM_SD_NOTIFY_INTERVAL_S` (20), `USB_PROVISION_INTERVAL_S` (60), `NOTIFY_SOCKET`.
- Agent `.env`: `SPOKE_URL`, `AGENT_ID`, `AGENT_SECRET`; process `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`, `LM_PXMX_STATE_DIR`, `LM_SD_NOTIFY_INTERVAL_S`, `USB_PROVISION_INTERVAL_S`.

## Install flags

- `install_pxmx.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--tls-verify` (+ `--tls-ca-cert`; **required** on standalone), `--all-prereqs` (no-op). IDs default `<hostname>-spoke`.
- `agent/install_agent.sh`: `--spoke-url`, `--id`, `--secret` (all optional; auto-discovers when `--spoke-url` absent).

## Key commands / handlers

- **`ProxmoxSpoke.handle_command`** (`src/proxmox_spoke.py`): `GET_VERSION`, `UPDATE_CONFIG`, `SET_AGENT_CONFIG`, `GET_AGENTS`, `SPOKE_RELAY` (`APPROVAL_SUCCESS`/`REVOKE_AGENT`/forward), `GET_NODE_STATS`, `PXMX_LIST_VMS`/`GET_VM_LIST`/`AGENT_GET_VM_LIST`, `SEARCH_VMS`, `GET_VM_INFO`, `CREATE_VM`/`AGENT_CREATE_VM`, `DELETE_VM`/`AGENT_DELETE_VM`, `PXMX_VM_ACTION`, `PXMX_CLONE_VM`, `PXMX_LIST_POOLS`, `PXMX_LIST_ISOS`, `PXMX_LIST_STORAGES`, `PXMX_CREATE_VM`, `VNC_PROXY`, `VNC_START`, `VNC_FRAME_DOWN`, `VNC_DISCONNECT`. VM identity key: `<cluster_name>/<node>/<vmid>`.
- **`PxmxControlPlane._agent_handler`** relayed frame types (wrapped in `AGENT_RELAY_UP`): `AGENT_HEARTBEAT`, `AGENT_TELEMETRY`, `AGENT_RESPONSE`, `AGENT_LOG`, `CS_*` (`CS_TELEMETRY`/`CS_LOG`/`CS_WATCHDOG_EVENT`/`CS_HW_RESET_EVENT`/`CS_PROGRESS`/`CS_COMMAND_RESULT`/`CS_TOKEN_RESULT`), `VNC_*` (`VNC_FRAME_UP`/`VNC_READY`/`VNC_ERROR`/`VNC_DISCONNECT`). `SET_LOG_LEVEL`/`SPOKE_SET_LOG_LEVEL` broadcast down to all agents.
- **Agent dispatch** (`agent/src/agent.py`): `UPDATE_CONFIG`, `GET_VM_LIST`, `GET_NODE_STATS`, `GET_SYSTEM_STATS`, `SET_LOG_LEVEL`, `SHELLEXEC`, `CS_COMMAND` (→ `cs_commands.handle_cs_command`), `CS_CREATE_PROXMOX_TOKEN`, `PXMX_VM_ACTION`, `PXMX_CLONE_VM`, `PXMX_LIST_POOLS/ISOS/STORAGES`, `PXMX_CREATE_VM`, `VNC_PROXY/START/FRAME_DOWN/DISCONNECT`.
- **`cs_commands.handle_cs_command`** fast actions: `start_vm`, `stop_vm`, `reboot_vm`, `snapshot_vm`, `start_vms`, `stop_vms`, `snapshot_vms`, `unlock_template`, `clear_provision_lock`, `clear_usb_quarantine`. Long ops (accepted + `CS_PROGRESS` + terminal `CS_COMMAND_RESULT`) in `agent/src/cs_sim.py::LONG_ACTIONS`: `delete_vm`, `reclone_vm`, `clone_lxc`, `provision_unassigned`, `backup`, `reseed`, `update_agent`.

## Key files

- Spoke: `src/control_plane.py` (`PxmxControlPlane`, `run_agent_server` — self-healing agent listener, loopback `:8443` all-in-one via `LM_PXMX_AGENT_LOOPBACK` reached through the hub `/ws/agent` byte-proxy, `:443` wss standalone), `src/proxmox_spoke.py` (`ProxmoxSpoke`), `src/discovery.py` (vendored).
- Agent: `agent/src/agent.py` (`ProxmoxAgent`, telemetry `_cs_telemetry_body`, VNC, self-update, sd_notify watchdog), `agent/src/usb_provision.py` (7-layer auto-provision brain), `agent/src/cs_commands.py`, `agent/src/cs_sim.py`, `agent/src/cs_guard.py` (90000 VMID floor + `PROTECTED_VMIDS`), `agent/src/pve_cmds.py` (async `qm`/`pct`), `agent/src/watchdogs.py`, `agent/src/security_utils.py` (HMAC), `agent/src/update_recovery.py`, `agent/src/vm_names.json`, `agent/src/dep_guard.py`.

## Notable behaviors & gotchas

- **Auto-provisioning brain = the agent**, not the hub or cs spoke. `usb_provision.run_provision_loop` is a 7-layer gate: reconcile → toggle gate → 1h resource thresholds → `provision_halt` → delete gate + 300s cooldown → missing-dongle teardown → VMID-gap audit → clone. Every silent gate logs a `reason`/`_provision_reason` so the UI can show *why* nothing provisions.
- **Resource gate sources Proxmox, not psutil** — `sample_resources` reads Proxmox node stats (`get_node_stats` cpu/mem), matching the UI tiles; psutil's virtual_memory counts VM RAM+cache and false-fired. Cold-start: no-data provisions freely (`cpu_avg is None` → gate passes).
- **Per-host VMID ranges** — `_host_vmid_range` derives a per-host batch (hostname-suffix → VMID range, stride 24) instead of a flat shared 90000–99999. Clone-template VMIDs are excluded from the pool. `batch_id` in telemetry.
- **VNC ticket = RFB password** — `VNC_START` must use `request_response` end-to-end so the Proxmox ticket reaches the browser (noVNC `credentials.password`); `send_to_spoke_command` is fire-and-forget and drops it.
- **Toggle key-name duality** — `usb_auto_provision`/`auto_provision`, `usb_missing_timeout`/`missing_timeout`, `usb_max_slots`/`max_slots` (webui-spoke 6-key blob vs lm-spoke 27-key payload); readers take the union.
- **Kernel crash-hardening** files are `lm-pxmx`-prefixed so `retire_bash_agent.sh` won't clobber them; the softdog `rmmod` is guarded.

## Related pages

[architecture-topology.md](architecture-topology.md), [cs.md](cs.md), [lm-hub.md](lm-hub.md), [environment-variables.md](environment-variables.md), [install-flags.md](install-flags.md).