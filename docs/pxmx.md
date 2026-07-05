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
- Agent listener (`run_agent_server`): **standalone (DEFAULT)** serves `wss://0.0.0.0:443` so a remote Proxmox agent dials `wss://<this-spoke>:443/ws/agent` directly; **loopback** (`--loopback`, co-located all-in-one only) binds `127.0.0.1:8443` plaintext and the hub `/ws/agent` route byte-proxies to it; legacy no-cert fallback `ws://0.0.0.0:8766`. mDNS TXT `agent_port` advertises `443` (the hub's external surface on the all-in-one path).
- mDNS browses `_lm-hub._tcp.local.` (TXT `tls_port` + `agent_port`); DNS `lm-hub.<search>`.

## Agent listener modes — standalone vs loopback (important caveat)

The pxmx spoke's agent listener has two modes. **Which mode is deployed determines the whole agent path** — get this right or the agent gets `Connection refused`:

- **Standalone (DEFAULT — agent → spoke → hub).** The pxmx spoke lives on its **own box**, separate from the hub. It serves `wss://0.0.0.0:443` (self-signed cert) and a Proxmox agent dials `wss://<spoke>:443/ws/agent` **directly**; the spoke then talks to the hub outbound. This is the design across the board. `install_pxmx.sh` (run directly on the spoke box) defaults to this — **no `--loopback`**. Because a standalone spoke does **not** broadcast `_lm-hub` mDNS (only the hub does), the agent **cannot auto-discover it** — the agent install must be **pinned**: `agent/install_agent.sh --spoke-ip <spoke-host>`. The installer prints this pinned command.
- **Loopback (opt-in — agent → hub → spoke).** The pxmx spoke is **co-located with the hub on the same box** (all-in-one). The hub already owns `:443`, so the pxmx agent listener binds `127.0.0.1:8443` **plaintext**; a Proxmox agent dials `wss://<hub>:443/ws/agent` (auto-discovered via `_lm-hub` mDNS / `lm-hub` DNS) and the hub `/ws/agent` route byte-proxies to the loopback listener. `--loopback` is passed **only by `install_all.sh`** (the rare co-located all-in-one path); a standalone install never sets it.

> **If a remote agent reports `[Errno 111] Connect call failed (<spoke>, 443)`**, the spoke is almost certainly in loopback mode (bound `127.0.0.1:8443`, refuses remote) when it should be standalone — i.e. `install_all.sh` was used on a box that is actually a standalone spoke, or `LM_PXMX_AGENT_LOOPBACK=1` is set in `/opt/lm/pxmx/.env`. Fix: reinstall with the standalone `install_pxmx.sh` (no `--loopback`) and pin the agent to `wss://<spoke>:443/ws/agent`. Check with `journalctl -u lm-pxmx | grep 'Agent listener on'` — standalone shows `wss://0.0.0.0:443`, loopback shows `ws://127.0.0.1:8443`.

## Environment variables

- Spoke `.env`: `HUB_URL`, `SPOKE_ID`, `SPOKE_SECRET`, `HUB_SECRET`, `LM_TLS_CERT`, `LM_TLS_KEY`, `LM_PXMX_AGENT_PORT`, `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`.
- Spoke process: `LM_DEP_GUARD_DISABLE`, `LM_PXMX_STATE_DIR` (`/var/lib/pxmx/update-state`), `LM_SD_NOTIFY_INTERVAL_S` (20), `USB_PROVISION_INTERVAL_S` (60), `NOTIFY_SOCKET`.
- Agent `.env`: `SPOKE_URL`, `AGENT_ID`, `AGENT_SECRET`; process `LM_HUB_TLS_VERIFY`, `LM_HUB_CA_CERT`, `LM_PXMX_STATE_DIR`, `LM_SD_NOTIFY_INTERVAL_S`, `USB_PROVISION_INTERVAL_S`.

## Install flags

- `install_pxmx.sh`: `--hub`, `--id`/`--name`, `--secret`, `--hub-secret`, `--tls-verify` (+ `--tls-ca-cert`; **required** on standalone), `--loopback` (opt-in co-located/all-in-one mode — passed only by `install_all.sh`; default is standalone `agent → spoke → hub`), `--all-prereqs` (no-op). IDs default `<hostname>-spoke`.
- `agent/install_agent.sh`: `--spoke-ip` (preferred; just the spoke's IP, the agent auto-determines the scheme/port/`/ws/agent` path by probing), `--spoke-url` (advanced full-URL pin), `--id`, `--secret`.

## Key commands / handlers

- **`ProxmoxSpoke.handle_command`** (`src/proxmox_spoke.py`): `GET_VERSION`, `UPDATE_CONFIG`, `SET_AGENT_CONFIG`, `GET_AGENTS`, `SPOKE_RELAY` (`APPROVAL_SUCCESS`/`REVOKE_AGENT`/forward), `GET_NODE_STATS`, `PXMX_LIST_VMS`/`GET_VM_LIST`/`AGENT_GET_VM_LIST`, `SEARCH_VMS`, `GET_VM_INFO`, `CREATE_VM`/`AGENT_CREATE_VM`, `DELETE_VM`/`AGENT_DELETE_VM`, `PXMX_VM_ACTION`, `PXMX_CLONE_VM`, `PXMX_LIST_POOLS`, `PXMX_LIST_ISOS`, `PXMX_LIST_STORAGES`, `PXMX_CREATE_VM`, `VNC_PROXY`, `VNC_START`, `VNC_FRAME_DOWN`, `VNC_DISCONNECT`. VM identity key: `<cluster_name>/<node>/<vmid>`.
- **`PxmxControlPlane._agent_handler`** relayed frame types (wrapped in `AGENT_RELAY_UP`): `AGENT_HEARTBEAT`, `AGENT_TELEMETRY`, `AGENT_RESPONSE`, `AGENT_LOG`, `CS_*` (`CS_TELEMETRY`/`CS_LOG`/`CS_WATCHDOG_EVENT`/`CS_HW_RESET_EVENT`/`CS_PROGRESS`/`CS_COMMAND_RESULT`/`CS_TOKEN_RESULT`), `VNC_*` (`VNC_FRAME_UP`/`VNC_READY`/`VNC_ERROR`/`VNC_DISCONNECT`). `SET_LOG_LEVEL`/`SPOKE_SET_LOG_LEVEL` broadcast down to all agents.
- **Agent dispatch** (`agent/src/agent.py`): `UPDATE_CONFIG`, `GET_VM_LIST`, `GET_NODE_STATS`, `GET_SYSTEM_STATS`, `SET_LOG_LEVEL`, `SHELLEXEC`, `CS_COMMAND` (→ `cs_commands.handle_cs_command`), `CS_CREATE_PROXMOX_TOKEN`, `PXMX_VM_ACTION`, `PXMX_CLONE_VM`, `PXMX_LIST_POOLS/ISOS/STORAGES`, `PXMX_CREATE_VM`, `VNC_PROXY/START/FRAME_DOWN/DISCONNECT`.
- **`cs_commands.handle_cs_command`** fast actions: `start_vm`, `stop_vm`, `reboot_vm`, `snapshot_vm`, `start_vms`, `stop_vms`, `snapshot_vms`, `unlock_template`, `clear_provision_lock`, `clear_usb_quarantine`. Long ops (accepted + `CS_PROGRESS` + terminal `CS_COMMAND_RESULT`) in `agent/src/cs_sim.py::LONG_ACTIONS`: `delete_vm`, `reclone_vm`, `clone_lxc`, `provision_unassigned`, `backup`, `reseed`, `update_agent`.

## Key files

- Spoke: `src/control_plane.py` (`PxmxControlPlane`, `run_agent_server` — self-healing agent listener; **standalone DEFAULT** `wss://0.0.0.0:443` (agent → spoke → hub); **loopback** `127.0.0.1:8443` plaintext via `LM_PXMX_AGENT_LOOPBACK=1` reached through the hub `/ws/agent` byte-proxy (agent → hub → spoke, `--loopback`/install_all only); legacy no-cert `ws://0.0.0.0:8766`), `src/proxmox_spoke.py` (`ProxmoxSpoke`), `src/discovery.py` (vendored).
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