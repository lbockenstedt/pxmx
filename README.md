# pxmx — Proxmox VE Spoke (Lab Manager Module)

`pxmx` is the Lab Manager module for Proxmox VE hypervisors. It connects Proxmox VE clusters and standalone nodes to the Lab Manager (LM) unified control plane, enabling complete hypervisor telemetry, virtual machine and LXC container lifecycle operations, direct-attached drive health monitoring, and client-simulation automation without opening the native Proxmox web interface.

---

## Architecture

`pxmx` operates on a two-tier coordinator and agent topology:

```
┌─────────────────┐             WebSocket / TLS (:443)             ┌─────────────────┐
│     LM Hub      │ ◄────────────────────────────────────────────► │   pxmx Spoke    │
│  Control Plane  │                                                │  (Coordinator)  │
└─────────────────┘                                                └────────┬────────┘
                                                                            │
                                                WebSocket / TLS (:443 / :8443)
                                                                            │
                                                       ┌────────────────────┴────────────────────┐
                                                       ▼                                         ▼
                                            ┌─────────────────────┐                   ┌─────────────────────┐
                                            │  pxmx Host Agent    │                   │  pxmx Host Agent    │
                                            │   (Proxmox Node 1)  │                   │   (Proxmox Node 2)  │
                                            └──────────┬──────────┘                   └──────────┬──────────┘
                                                       │                                         │
                                            ┌──────────┴──────────┐                   ┌──────────┴──────────┐
                                            │ Direct Drive Health │                   │ Direct Drive Health │
                                            │ (smartctl / ssacli) │                   │ (smartctl / ssacli) │
                                            └─────────────────────┘                   └─────────────────────┘
```

1. **Spoke Coordinator (`src/proxmox_spoke.py`, `src/control_plane.py`):**
   - Establishes an outbound TLS WebSocket dial to the LM Hub control plane (`/ws/spoke` on port 443).
   - Serves as the agent listener (`/ws/agent`), fanning out commands and aggregating telemetry across multiple physical Proxmox VE hosts.
   - Manages canonical VM addressing using `<cluster_name>/<node>/<vmid>`.
   - Manages console sessions (VNC and interactive host shells) between browser clients and node agents.

2. **Node-Agent Communication (`agent/src/agent.py`):**
   - Runs as a systemd service (`lm-pxmx-agent.service`) directly on each Proxmox VE host as root.
   - Dials the Spoke coordinator over an authenticated WebSocket link (`/ws/agent`), using HMAC message signing (`agent/src/security_utils.py`).
   - Executes hypervisor-native CLI operations (`qm`, `pct`, `pvesh`, `vzdump`, `pvenode`).
   - Runs background watchdogs, guest agent monitors, and client-simulation auto-provisioning pipelines.

3. **Direct-Attached Drive Health Pipeline (`src/drive_health.py`, `agent/src/drive_health.py`):**
   - Automatically probes local storage devices across SATA, SAS, and NVMe interfaces using `smartctl`.
   - Detects HPE ProLiant platforms and Smart Array RAID controllers via DMI/sysfs inspection (`/sys/class/dmi/id/sys_vendor`, `/sys/bus/pci/drivers/smartpqi`, `/sys/bus/pci/drivers/hpsa`), supporting dynamic installation of `hpssacli`/`ssacli`.
   - Collects critical drive health indicators: SSD wear leveling percentage, NVMe endurance / spare block depletion, reallocated sectors, power-on hours, and operating temperatures.
   - Normalizes telemetry into structured status assessments (`healthy`, `warning`, `critical`) with alert thresholds.

---

## Features

- **Node Telemetry & Metrics:** Real-time collection of CPU utilization (1m, 5m, 15m load averages), total and allocated memory, swap pressure, storage pool capacities, kernel versions, and PVE platform versions.
- **VM & Container Lifecycle:** Full lifecycle management for QEMU virtual machines and LXC containers: start, stop, reboot, snapshot create/rollback/delete, clone, destroy, and cross-tenant re-tagging.
- **Bulk Lifecycle Operations:** Concurrent execution of power and snapshot commands across multiple VMs per node with semaphore concurrency limits.
- **Storage Pool Visibility:** Real-time visibility into local and shared storage pools (`local`, `local-lvm`, `local-zfs`, NFS, Ceph), backup-capable destinations, and ISO repositories.
- **Drive Health Monitoring:** Host-level disk telemetry covering SMART data, wear-leveling percentages, NVMe percentage used, HPE Smart Array logical/physical drives, and proactive threshold alerts.
- **Remote Web Consoles:** Interactive noVNC graphical displays and web-based terminal PTY shells routed through the spoke-agent relay with ticket authentication.
- **Certificate Distribution:** Unattended distribution and application of Let's Encrypt TLS certificates issued by the LM Hub directly to `pveproxy` via `pvenode cert set`.
- **Client-Simulation Automation:** Built-in auto-provisioning brain managing USB dongle tracking, multi-tier resource gates, template unlocking, and automated VM provisioning.

---

## Spoke Commands Reference Table

Commands routed and processed by `src/proxmox_spoke.py`:

| Command | Direction / Scope | Description |
| :--- | :--- | :--- |
| `GET_VERSION` | Hub → Spoke | Returns spoke module version and local git commit SHA. |
| `UPDATE_CONFIG` | Hub → Spoke / Agents | Updates spoke configuration and broadcasts new settings to connected agents. |
| `PXMX_RETAG_TENANT` | Hub → Spoke → Agents | Cross-tenant migration: re-tags VMs with `old_tag` to `new_tag` across all nodes. |
| `SET_AGENT_CONFIG` | Hub → Spoke → Agent | Pushes and persists configuration for a designated agent. |
| `GET_AGENTS` | Hub → Spoke | Returns registry of connected and pending agents with node/VM summary counts. |
| `SPOKE_RELAY` | Hub → Spoke → Agent | Relays control commands (`APPROVAL_SUCCESS`, `REVOKE_AGENT`, generic commands) to target agent. |
| `GET_NODE_STATS` | Hub → Spoke → Agents | Aggregates CPU, memory, and storage metrics across specified or all connected nodes. |
| `PXMX_LIST_VMS` | Hub → Spoke → Agents | Aggregates full virtual machine and container inventory from all connected agents. |
| `GET_VM_LIST` / `AGENT_GET_VM_LIST` | Hub → Spoke → Agents | Aliases for `PXMX_LIST_VMS`. |
| `SEARCH_VMS` | Hub → Spoke | Searches VMs across clusters by name, VMID, IP, or MAC with tenant-scoping enforcement. |
| `GET_VM_INFO` | Hub → Spoke → Agent | Fetches detailed configuration and runtime status for a specific VM. |
| `PXMX_VM_ACTION` | Hub → Spoke → Agent | Executes single VM action (`start`, `stop`, `reboot`, `snapshot`, `backup`) via agent. |
| `PXMX_VM_ACTION_BULK` | Hub → Spoke → Agents | Groups and executes lifecycle actions across multiple VMs in parallel per agent. |
| `PXMX_CLONE_VM` | Hub → Spoke → Agent | Clones a template VM to a new VMID with name, resource pool, and tenant tags. |
| `PXMX_LIST_POOLS` | Hub → Spoke → Agents | Aggregates Proxmox resource pools across all connected agents. |
| `PXMX_LIST_ISOS` | Hub → Spoke → Agent | Lists ISO images present across storage volumes on a given node. |
| `PXMX_LIST_STORAGES` | Hub → Spoke → Agent | Queries storage volumes on a target node accepting specified content types. |
| `PXMX_DRIVE_HEALTH` | Hub → Spoke → Agent | Queries physical drive telemetry, wear-leveling, and SMART health for a node. |
| `PXMX_INSTALL_SSACLI` | Hub → Spoke → Agent | Triggers automated HPE SSACLI package installation on an HPE server node. |
| `PXMX_CREATE_VM` | Hub → Spoke → Agent | Creates and configures a new QEMU virtual machine from an ISO image. |
| `INSTALL_CERT` | Hub → Spoke → Agent | Relays TLS certificate and private key to agent for local `pveproxy` installation. |
| `VNC_START` | Hub → Spoke → Agent | Establishes authenticated noVNC console session, returning VNC ticket. |
| `VNC_FRAME_DOWN` | Hub → Spoke → Agent | Relays browser input frames to node VNC session. |
| `VNC_DISCONNECT` | Hub → Spoke → Agent | Tears down active VNC console session. |
| `SHELL_START` | Hub → Spoke → Agent | Spawns interactive administrative PTY shell session on target node. |
| `SHELL_IN` / `SHELL_RESIZE` | Hub → Spoke → Agent | Streams terminal input and window resize events to the active PTY shell. |
| `SHELL_DISCONNECT` | Hub → Spoke → Agent | Closes active host shell session. |

---

## Agent Commands Reference Table

Commands handled by `agent/src/agent.py` on the Proxmox host:

| Command | Handler / Subsystem | Description |
| :--- | :--- | :--- |
| `UPDATE_CONFIG` | `agent.py` / `managed_crontab` | Updates agent runtime settings, reconciles crontabs, and toggles CS loops. |
| `GET_VM_LIST` | `agent.py:get_vm_list` | Queries local `qm` and `pct` for all configured guest domains and containers. |
| `GET_NODE_STATS` | `agent.py:get_node_stats` | Queries local PVE node stats (CPU, RAM, swap, storage). |
| `GET_SYSTEM_STATS` | `agent.py:collect_metrics` | Collects system hardware and performance telemetry. |
| `SET_LOG_LEVEL` | `agent.py:set_log_level` | Adjusts runtime logging verbosity dynamically. |
| `RUN_COMMAND` | `command_runner.py` | Runs signed local system commands with timeouts and buffer truncation limits. |
| `CS_COMMAND` | `cs_commands.py` / `cs_sim.py` | Dispatches Client-Sim commands (fast sync actions or async background jobs). |
| `CS_CREATE_PROXMOX_TOKEN` | `agent.py:_provision_proxmox_token` | Provisions an administrative API token via local `pvesh` for hub integration. |
| `PXMX_VM_ACTION` | `pve_cmds.py:vm_action_any` | Executes start, stop, reboot, snapshot, or vzdump backup on a guest. |
| `PXMX_VM_ACTION_BULK` | `pve_cmds.py` (concurrency sem) | Executes lifecycle actions across a batch of VMs on this host. |
| `PXMX_LIST_STORAGE` | `pve_cmds.py:list_backup_storages` | Identifies storage destinations supporting backup archives. |
| `PXMX_RETAG_TENANT` | `pve_cmds.py:retag_tenant` | Replaces tenant tags on all local guest configs matching the target tag. |
| `OS_UPDATE_CHECK` | `os_update.py:check_updates` | Checks for pending Debian/PVE package updates via apt. |
| `OS_UPDATE_APPLY` | `os_update.py:apply_updates` | Performs unattended dist-upgrade (guarded against active provisioning). |
| `PXMX_GET_IDENTITY` | `agent.py` | Reports agent ID, hostname, and active spoke connection coordinates. |
| `PXMX_POOL_ADD_VMS` | `pve_cmds.py:pool_add_vms` | Adds designated simulation VMs into a Proxmox resource pool. |
| `PXMX_APPLY_SIM_TAGS` | `pve_cmds.py:apply_sim_tags` | Asynchronously writes client-simulation metadata tags to local guests. |
| `PXMX_CLONE_VM` | `pve_cmds.py:clone_vm_any` | Clones template guest, sets network and tenant labels, assigns resource pool. |
| `PXMX_LIST_POOLS` | `agent.py:list_pools` | Queries cluster resource pools configured on this host. |
| `PXMX_LIST_ISOS` | `agent.py:list_node_isos` | Inspects node storages for ISO installation images. |
| `PXMX_LIST_STORAGES` | `agent.py:list_node_storages` | Queries node storage configurations for image/disk allocations. |
| `PXMX_DRIVE_HEALTH` | `drive_health.py` | Executes `smartctl` and `ssacli` drive audits and returns wear/alerting telemetry. |
| `PXMX_INSTALL_SSACLI` | `drive_health.py:install_ssacli_if_needed` | Installs HPE Smart Storage Administrator tools on HPE hardware. |
| `PXMX_CREATE_VM` | `agent.py` (`pvesh create`) | Creates a new QEMU VM from ISO with specified disk, memory, CPU, and network. |
| `VNC_START` | `agent.py:_start_vnc_session` | Spawns `vncproxy` via `pvesh`, connects local WebSocket, and returns ticket. |
| `VNC_FRAME_DOWN` | `agent.py` (Queue drain) | Relays incoming RFB frames from browser to the Proxmox VNC socket. |
| `VNC_DISCONNECT` | `agent.py:_vnc_teardown` | Closes VNC socket connection and releases session structures. |
| `SHELL_START` | `agent.py:_start_shell_session` | Forks local PTY with interactive bash shell for web terminal access. |
| `SHELL_IN` | `agent.py:_shell_write` | Forwards user keystrokes into the active PTY master file descriptor. |
| `SHELL_RESIZE` | `agent.py:_shell_resize` | Sets terminal window size via `TIOCSWINSZ` ioctl call. |
| `SHELL_DISCONNECT` | `agent.py:_shell_teardown` | Terminates shell child process and releases master PTY file descriptor. |
| `INSTALL_CERT` | `agent.py:install_cert` | Deploys TLS certificates to `pveproxy` using `pvenode cert set`. |
| `START_BACKUP` | `template_ops.py` | Triggers background vzdump backup and streams archive to repository. |
| `REFRESH_TEMPLATE` | `template_ops.py` | Restores base template from archive after purging sim VMs. |

---

## Installation & Environment Configuration

Installers are idempotent — re-running updates code while preserving configuration and credentials.

### 1. Spoke Coordinator Installation (`install_pxmx.sh`)

Run in the dedicated spoke container or VM:

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/pxmx/main/install_pxmx.sh \
  | sudo bash -s -- --hub wss://lm-hub.example.com:443
```

| Flag | Description |
| :--- | :--- |
| `--hub URL` | LM Hub WebSocket URL (`wss://<host>:443`). Bare host is automatically normalized. |
| `--id`, `--name` | Spoke identifier (defaults to `<hostname>-spoke`). |
| `--secret` | Pre-shared key for authenticating with the hub. |
| `--hub-secret` | Hub PSK for automated auto-approval. |
| `--tls-verify` | Enables strict TLS certificate validation. |
| `--tls-ca-cert PATH` | Path to custom CA certificate for TLS verification. |
| `--loopback` | Enables loopback mode for co-located (all-in-one) hub/spoke installations. |
| `--infra-only` | Installs host-level prerequisites without starting spoke daemon. |

**Spoke Environment Variables (`/opt/lm/pxmx/.env`):**
- `HUB_URL`: Target LM Hub WebSocket endpoint.
- `SPOKE_ID`: Unique identifier for this spoke coordinator.
- `SPOKE_SECRET`: Pre-shared authentication secret.
- `HUB_SECRET`: Hub auto-approval secret.
- `LM_PXMX_AGENT_PORT`: Agent listener port (default `443` standalone, `8443` loopback).
- `LM_TLS_CERT`, `LM_TLS_KEY`: Paths to TLS certificates for the agent listener.
- `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`: Hub certificate validation settings.

### 2. Host Agent Installation (`agent/install_agent.sh`)

Run directly **on each Proxmox VE host** as root:

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/pxmx/main/agent/install_agent.sh \
  | sudo bash -s -- --spoke-ip <spoke-ip>
```

| Flag | Description |
| :--- | :--- |
| `--spoke-ip IP` | IP address of the pxmx spoke. Automatically negotiates scheme, port, and `/ws/agent` path. |
| `--spoke-url URL` | Explicit full WebSocket URL to the spoke agent listener. |
| `--id` | Unique agent identifier (defaults to host hostname). |
| `--secret` | Agent pre-shared key matching spoke configuration. |

**Agent Environment Variables (`/etc/lm-agent/config.json` or systemd unit):**
- `SPOKE_URL`: Pinned WebSocket endpoint for the parent spoke coordinator.
- `AGENT_ID`: Unique identity for this host agent.
- `AGENT_SECRET`: HMAC authentication secret.
- `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`: Certificate verification configuration.

---

## Testing & Verification

Execute the test suites from the repository root:

```bash
# Run spoke tests
pytest tests

# Run agent tests
pytest agent/tests
```