# pxmx — Proxmox Manager (LM module)

`pxmx` is the Lab Manager module for Proxmox VE. It has two cooperating parts: a
**host agent** that runs on a Proxmox node and does the actual VM work (clone,
destroy, USB provisioning, watchdogs), and a **spoke** that bridges multiple
agents into the LM Hub control plane. The agent is where the Client-Sim (cs)
auto-provisioning "brain" lives.

For the topology and where the brain lives, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Operators

### What it does
- **VM lifecycle** — start/stop/reboot/snapshot/clone/destroy QEMU and LXC
  containers via `qm`/`pct` (`agent/src/pve_cmds.py`).
- **Client-Sim (cs) control** — fast (<15s) cs commands plus long-running
  operations (fleet reclone, update-all) dispatched in the background
  (`agent/src/cs_commands.py`, `agent/src/cs_sim.py`).
- **USB auto-provisioning** — the cs "brain": toggle gate, 1h resource
  thresholds, delete-gate + 300s cooldown, `provision_halt`, `prov_run`,
  VMID-gap audit, slot cap (`agent/src/usb_provision.py:run_provision_loop`).
- **Watchdogs** — hardware + guest-agent watchdogs ported from the legacy cs
  bash agent (`agent/src/watchdogs.py`).
- **Self-update** — pulls new code from GitHub and restarts itself.

### Install
```bash
sudo ./install_pxmx.sh --hub wss://<hub-host>:443/ws/spoke [--id <spoke-id>] [--secret <psk>]
```
- The hub serves the spoke WebSocket on the unified `:443` uvicorn (`/ws/spoke`,
  wss when a cert is configured). Omit `--hub` to auto-discover via mDNS/DNS.
- Without `--secret`, the spoke connects unauthenticated and awaits admin
  approval in the LM WebUI. IDs default to `<hostname>-spoke`.
- Installs to `/opt/lm/pxmx`, creates the `lm-pxmx.service` systemd unit, and
  starts it.

### Ports
- LM Hub control plane: **443** wss (`/ws/spoke` — the spoke connects to this).
- pxmx agent listener: **443** wss **standalone (default)** — the spoke (on its own box) serves `wss://0.0.0.0:443` and a Proxmox agent dials `wss://<spoke>:443/ws/agent` directly (**agent → spoke → hub**; agent pinned via `--spoke-ip` — just the spoke's IP; the agent auto-determines the scheme/port/`/ws/agent` path by probing; a standalone spoke does not broadcast `_lm-hub` mDNS). **8443** loopback (co-located all-in-one, `--loopback`/`install_all.sh` only — `agent → hub → spoke`, hub `/ws/agent` byte-proxies to it). **8766** is the legacy no-cert plaintext fallback. See [docs/pxmx.md](docs/pxmx.md).

### Notable fix
A recurring **agent-blackout** bug (the agent-server task died and the spoke
stopped relaying to agents) was fixed — the agent-server task is now kept alive
and self-heals on exit, and the spoke guarantees the agent-listener port is
released before a new instance starts. Current version: see `VERSION`.

## Developers

### Repo layout
| Path | Role |
|------|------|
| `agent/src/agent.py` | `ProxmoxAgent` — the host agent: telemetry, cs event relay, USB provision loop. |
| `agent/src/usb_provision.py` | The auto-provisioning brain + host-side USB state machine. |
| `agent/src/cs_commands.py` | Fast cs command dispatcher. |
| `agent/src/cs_sim.py` | Long-op implementations (Phase E). |
| `agent/src/cs_guard.py` | Execution-layer sim-VM guard (90000 floor + `PROTECTED_VMIDS`). |
| `agent/src/pve_cmds.py` | Async `qm`/`pct` wrappers. |
| `agent/src/watchdogs.py` | Hardware + guest-agent watchdogs. |
| `agent/src/security_utils.py` | HMAC message signing. |
| `src/control_plane.py` | `PxmxControlPlane` — Hub-side: runs the agent listener (`run_agent_server` — `:443` wss standalone default, `:8443` loopback via `LM_PXMX_AGENT_LOOPBACK=1`/`--loopback`, `:8766` no-cert fallback), runs self-update. |
| `src/proxmox_spoke.py` | `ProxmoxSpoke` — the multi-agent bridge spoke (Hub ↔ agents). |
| `install_pxmx.sh` | Installer (systemd service + prereqs). |
| `pxmx.Dockerfile` | Container build. |

### Where the auto-provisioning brain lives
Only the **agent** has Proxmox clone/destroy, so the brain runs in
`agent/src/usb_provision.py:run_provision_loop` — **not** in the LM Hub and
**not** in the LM cs spoke (which is relay-only). The toggle arrives under two
key names — `usb_auto_provision` (webui-spoke 6-key blob) or `auto_provision`
(lm-spoke full payload) — so readers take the union. Same for
`usb_missing_timeout`/`missing_timeout` and `usb_max_slots`/`max_slots`. See
[ARCHITECTURE.md](ARCHITECTURE.md).

Every clone (first-clone + reclone) also schedules a **+15-min post-clone
settle reboot** (`post_prov_reboot[vmid]` in `usb_state.json`, swept by
`_run_post_prov_reboot_queue`; env `POST_PROV_REBOOT_DELAY_S`, default 900)
so the box restarts after settling + pulling engine config + running
`update.sh`. Two reboots are intentional — the immediate post-clone reboot
only sets hostname/first-boot bits. See `lm/docs/pxmx.md` → *Post-clone
settle reboot*.

### Conventions
The agent code follows a **"Phase X port of legacy `cs/proxmox/proxmox-agent.sh`…"**
docstring convention with line-range cross-references to the solutions-hpe cs
source. When porting more cs logic, mirror that: cite the legacy file + line
range and the Phase milestone.

### Build & test
```bash
pip install -r requirements.txt
python3 -m py_compile agent/src/*.py src/*.py   # syntax check (no node/runtime needed for docs)
```
The cs spokes do **not** self-update — they are manually redeployed. pxmx
self-updates from GitHub on a Hub `SPOKE_UPDATE`.

### Related
- LM Hub repo: `vscode/lm` — see `lm/docs/architecture-topology.md`, `lm/docs/pxmx.md`, and `lm/docs/README.md` for the full canonical doc set.
- cs source: `github.com/solutions-hpe`.