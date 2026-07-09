# pxmx — Proxmox (hypervisor)

Proxmox bridge spoke + per-host agent. Repo: `pxmx`. `module_type = "hypervisor"`. See [architecture-topology.md](architecture-topology.md).

## Role & module_type

A **bridge spoke**: connects the LM hub to one or more **pxmx host agents** running on Proxmox nodes. The spoke never touches Proxmox — all `qm`/`pct` work happens in the agent. The agent also hosts the Client-Sim auto-provisioning **brain** (`agent/src/usb_provision.py`). Documented in `pxmx/ARCHITECTURE.md`.

## What it does

pxmx is what lets the LM WebUI see and control your Proxmox VE hosts: node stats, VM inventory, console access, and VM create/clone — all without opening the Proxmox web UI directly. Find it in the WebUI under the **Hypervisor** tab (spoke type label "Hypervisor (Proxmox / pxmx)"), where nodes are listed and clicking a node drills into its VMs.

Under the hood it also runs the Client-Simulation (cs) **auto-provisioning brain** — the logic that clones/tears down sim VMs to keep USB dongles matched to running VMs — and it keeps NetBox's VM inventory in sync for IPAM.

## Entrypoints

- **Spoke:** `python3 -m src.control_plane` (`PxmxControlPlane`), systemd `lm-pxmx.service`, `User=svc_lm`. Installer `install_pxmx.sh` (clones to `/opt/lm/pxmx`, `.env`, `lm-pxmx.service`, self-update rollback watchdog + sudoers, generates `agent_secret` at `/etc/lm-agent/config.json`).
- **Agent:** `python3 -m src.agent` (`ProxmoxAgent`), systemd `lm-pxmx-agent.service`, `User=root`, `WatchdogSec=60` + `NotifyAccess=main`. Installer `agent/install_agent.sh` (clones to `/opt/lm/pxmx/agent`, net-watchdog timer, kernel hang/panic sysctls, softdog, kdump-tools).
- Other: `agent/retire_bash_agent.sh`, `agent/uninstall_agent.sh`, `agent/lm-pxmx-net-watchdog.{service,timer,sh}`.

> **The bridge spoke is primarily a role now.** The pxmx **bridge spoke** runs mainly as the **`proxmox`** role hosted by the generic agent (`agent-<hostname>`, unit `lm-agent`): the agent opens a sub-spoke `{agent}-proxmox` (module_type `hypervisor`, parent-auto-approved) and self-installs it via `agent/src/agent_spoke.py::_install_role` — cloning `lbockenstedt/pxmx.git` + deps and running `install_pxmx.sh --infra-only` for the idempotent host prep (agent-host listener). The dedicated `lm-pxmx.service` / `install_pxmx.sh` `<hostname>-spoke` path is the **legacy/standalone** alternative. (Note: this is separate from the pxmx **per-host agent** `lm-pxmx-agent.service` described below, which still runs on each Proxmox node and does the actual `qm`/`pct` work regardless of how the bridge spoke is deployed.)

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

## How it works

**Topology.** `LM Hub ↔ pxmx bridge spoke ↔ one-or-more pxmx per-host agents`. The hub never talks to Proxmox directly — it sends commands to the bridge spoke, which routes them to the specific agent that owns the target node/VM (by `unique_id`), and only the agent runs `qm`/`pct`. See `pxmx/ARCHITECTURE.md` for the diagram.

**Bridge spoke deployment — current vs legacy.** The bridge spoke itself now runs mainly as the **`proxmox` role** hosted by the generic unified agent (`agent-<hostname>`, unit `lm-agent`): the agent opens a sub-spoke `{agent}-proxmox` (module_type `hypervisor`, parent-auto-approved) and self-installs it by cloning `lbockenstedt/pxmx.git` and running `install_pxmx.sh --infra-only` for idempotent host prep. This is the **current** path. The dedicated `lm-pxmx.service` (`install_pxmx.sh` run standalone, `<hostname>-spoke`) is the **legacy/standalone** alternative and still works, but new installs go through the agent role. Either way this is a *different component* from the **per-host pxmx agent** (`lm-pxmx-agent.service`) described below — that one runs on every Proxmox node and does the actual VM work regardless of how the bridge spoke got deployed.

**Relay wrapping.** Everything the per-host agent pushes up unsolicited — heartbeats, telemetry, logs, cs events, VNC frames — arrives at the hub wrapped in an `AGENT_RELAY_UP` envelope (`PxmxControlPlane._agent_handler`), which is how one bridge spoke fans in traffic from many agents without losing track of which agent said what. `AGENT_TELEMETRY`, `CS_TELEMETRY`/`CS_LOG`/`CS_WATCHDOG_EVENT`/`CS_PROGRESS`/`CS_COMMAND_RESULT`, and `VNC_FRAME_UP`/`VNC_READY`/`VNC_ERROR` all ride this path.

**VM identity.** Every VM is addressed by a single canonical key: **`<cluster_name>/<node>/<vmid>`**. This is how the spoke disambiguates VMID 105 on cluster A from VMID 105 on cluster B, and it's the identity carried into NetBox sync and VNC session tracking.

**VNC console flow.** Opening a console is a two-part relay: `VNC_START` is a **request/response** call (the hub blocks on `send_to_agent` with a real timeout) because the agent has to synchronously open the Proxmox `vncwebsocket` and hand back a ticket — and that ticket **doubles as the RFB/VNC password**, so it must arrive intact or noVNC authenticates with an empty password and Proxmox drops the session. Once the session is up, the high-volume pieces (`VNC_FRAME_DOWN`, `VNC_DISCONNECT`, and the agent's `VNC_FRAME_UP`) switch to fire-and-forget (`send_raw_to_agent`) so streaming frames never block the dispatch loop.

**VM → NetBox sync.** The spoke pulls the full VM list from every connected agent (`PXMX_LIST_VMS`) and pushes one `NETBOX_SYNC_VMS` (replace-all, cluster-wide) so NetBox's virtualization inventory matches Proxmox. This runs on a schedule (interval, default hourly, or a fixed daily time — configurable in the WebUI's System → Sync → Hypervisor panel) and also fires immediately after you create, clone, or otherwise edit a VM from the WebUI, so a manual change shows up in NetBox within a few seconds rather than waiting for the next scheduled pass.

**The auto-provisioning brain.** This is the most misunderstood part of pxmx, so read this section fully if dongles aren't turning into VMs. The brain is **not** in the hub and **not** in the cs spoke — the cs spoke is relay-only. It lives entirely in the per-host agent: `agent/src/usb_provision.py:run_provision_loop`, ticking roughly every 60s (`_usb_provision_loop`). Each tick runs a strict, ordered gate pipeline — if an earlier layer stops the pass, later layers don't run, and the agent logs *why* (surfaced back up through `CS_TELEMETRY` so the WebUI's Auto-Provisioning card can show the exact reason instead of just "nothing happened"):

1. **Reconcile** — release any USB bus↔VM assignment whose VM no longer exists, and follow a dongle that moved to a different bus after being unplugged/replugged.
2. **Toggle gate** — the tenant-level `usb_auto_provision` (or `auto_provision`) flag. Off → telemetry-only pass, no cloning/teardown/deletion at all.
3. **Resource thresholds** — CPU/mem 1-hour rolling averages, sourced from the **same Proxmox node stats the WebUI tiles show** (`get_node_stats`), *not* `psutil`. This matters: `psutil` counts VM RAM and page cache as "used" and reads high even when the host is actually idle, which used to fire the gate for no real reason. On a fresh agent with no samples yet (cold start), the gate treats "no data" as "not overloaded" and provisions freely rather than blocking forever.
4. **Delete gate** — if the delete threshold (default 90%) is exceeded, the newest (highest-VMID) sim VM is destroyed and a **300s cooldown** starts to stop repeated thrash.
5. **Missing-dongle teardown** — a sim VM whose dongle has been physically absent longer than the configured missing-timeout gets destroyed and its bus freed.
6. **VMID-gap audit** — every 300s, checks for holes in the assigned VMID range and deletes the highest VMID above a gap so the next pass can refill it cleanly.
7. **Clone** — only reached if resources are under the provision threshold (default 80%), not in delete cooldown, not at the CPU ceiling, no provisioning run already active, and under the configured slot cap (`usb_max_slots`/`max_slots`, default 24). Eligible present dongles are matched to a template (image1/image2, split by `image1_pct`) and cloned into a free VMID.

**The classic trap: two toggles, not one.** Auto-provisioning requires **both** the tenant-level `usb_auto_provision` toggle **and** the per-agent `client_simulation.enabled` flag to be on. It is extremely common to flip the tenant toggle in the WebUI, see nothing provision, and not realize the per-agent flag is still off (or vice versa) — check both before assuming something is broken.

**Per-host VMID ranges.** Sim VMIDs are never a flat shared pool. Each Proxmox host derives its own batch from its hostname's trailing number, stride 24 from a **90000 floor**: `svr-01` → 90001–90024, `svr-02` → 90025–90048, `svr-003` → 90049–90072, and so on. An explicit non-default `vmid_start`/`vmid_end` from the tenant config overrides the derived range (e.g. for a host needing more than 24 slots). On top of the range, `agent/src/cs_guard.py` enforces an **execution-layer** guard on every mutating command: nothing below VMID 90000 can ever be touched as a "sim VM", and a configurable `PROTECTED_VMIDS` set (default `{1001}`, the hub's own LXC container) can never be mutated regardless of any flag or command sent down.

## How to use it

**Install a per-host agent on a Proxmox node.**
- If your pxmx bridge spoke is co-located with the hub (all-in-one/loopback setup), the WebUI's Hypervisor install modal calls `/api/pxmx/agent-install-cmd` and gives you a ready-to-paste command (`curl ... install_agent.sh | sudo bash -s -- --spoke-ip <host>`) to run on the Proxmox node.
- If the bridge spoke is standalone (its own box, the default), the agent **cannot auto-discover it** — you must pin it explicitly: `agent/install_agent.sh --spoke-ip <spoke-host>` run on the Proxmox node. `install_pxmx.sh` prints this exact pinned command when it installs the spoke.
- Either way the agent installs as `lm-pxmx-agent.service` (root) on the Proxmox node and starts doing all `qm`/`pct` work for that node.

**Create a VM.**
- *Clone from template*: pick a template VM (must live in a configured template pool) and clone it to a new VMID/name from the Hypervisor tab's clone action. Full clones can take a while — the spoke allows up to 10 minutes for the operation to complete.
- *Create from ISO*: pick a node, an ISO volume, and sizing (memory/cores/disk/storage/bridge/pool); the agent runs the actual Proxmox create.
- Either path triggers an immediate VM→NetBox sync afterward, so the new VM shows up in NetBox without waiting for the scheduled sync.

**Clone a VM.** Same clone action as above, targeting any existing template-pool VM as the source; give it a new name and (optionally) an explicit new VMID.

**Open a VM console (VNC).** Click the console/VNC action on a VM in the Hypervisor tab. This opens a noVNC session relayed hub → spoke → agent → Proxmox's `vncwebsocket`. If it hangs or shows a black screen, see Troubleshooting below.

**Sync VMs to NetBox.** Sync happens automatically (interval or daily, configurable in System → Sync → Hypervisor) and immediately after any create/clone/lifecycle change from the WebUI. There's no separate "sync now" button to hunt for — editing a VM is enough to trigger it.

**Enable auto-provisioning.** You need both toggles on:
1. Tenant-level `usb_auto_provision` toggle in the Simulations/Auto-Provisioning card.
2. Per-agent `client_simulation.enabled` flag (pushed to the specific pxmx agent).

You also need at least one certified USB vid:pid configured and at least one clone-source template ID (`image1_template_id`/`image2_template_id`) set — without these the brain logs a gate reason (`no dongle_vidpids configured` / `no template ids configured`) and does nothing, by design.

## Troubleshooting / common questions

**"My agent logs `[Errno 111] Connect call failed (<spoke>, 443)`."** The spoke is almost certainly in **loopback** mode (bound `127.0.0.1:8443`, refuses remote connections) when it should be **standalone**. This happens when `install_all.sh` was used on a box that's actually meant to be a standalone spoke, or `LM_PXMX_AGENT_LOOPBACK=1` got set in `/opt/lm/pxmx/.env`. Fix: reinstall with standalone `install_pxmx.sh` (no `--loopback`) and pin the agent with `--spoke-ip <spoke-host>`. Confirm with `journalctl -u lm-pxmx | grep 'Agent listener on'` — standalone shows `wss://0.0.0.0:443`, loopback shows `ws://127.0.0.1:8443`. See the **Agent listener modes** section above for the full standalone-vs-loopback explanation.

**"The VM console is blank / won't connect."** The Proxmox VNC ticket doubles as the RFB (VNC) password. If it doesn't reach the browser intact — e.g. a code change accidentally sent `VNC_START` fire-and-forget instead of request/response — noVNC authenticates with an empty password and Proxmox silently drops the session, which looks like a blank/frozen console rather than an error. Confirm `VNC_START` completed (it should return promptly, not hang) before assuming a network problem; a genuinely blank screen after a successful start is more likely a guest-OS display issue.

**"The pxmx spoke or agent shows offline/red in the WebUI."** For the spoke: check whether it's running as the `proxmox` role under the generic agent (`lm-agent` unit, sub-spoke `{agent}-proxmox`) or the legacy standalone `lm-pxmx.service`, and check that unit's status/logs accordingly. For a per-host agent: check `lm-pxmx-agent.service` on that specific Proxmox node — a red agent doesn't take down the spoke or other agents, since the bridge spoke fans out to many agents independently.

**"Auto-provisioning is on but nothing provisions."** Walk the gate pipeline top to bottom — the agent log and the WebUI's Auto-Provisioning card both show the reason for the last pass:
1. Both toggles on? (`usb_auto_provision` tenant-level **and** `client_simulation.enabled` per-agent — the classic trap.)
2. Is a certified dongle actually plugged in and not on the ignored list?
3. Is the resource gate blocking (CPU/mem 1h average over threshold, or in the 300s post-delete cooldown)? Cold-start with no samples yet should *not* block — if it does, something else is wrong.
4. Is `provision_halt` set, or is a provisioning run already active (`prov_run active`)?
5. Is the slot cap (`usb_max_slots`) already reached?

**"Why do my sim VMs land in a weird VMID range like 90025-90048 instead of 90000+?"** That's expected — each Proxmox host gets its own VMID batch derived from its hostname's trailing number (stride 24 from the 90000 floor), so multiple hosts provisioning at once never collide on the same VMID. `svr-02` gets 90025-90048, `svr-003` gets 90049-90072, etc. An explicit `vmid_start`/`vmid_end` override in the tenant config replaces the derived range if you need more than 24 slots on one host.

## Related pages

[architecture-topology.md](architecture-topology.md), [cs.md](cs.md), [lm-hub.md](lm-hub.md), [environment-variables.md](environment-variables.md), [install-flags.md](install-flags.md).