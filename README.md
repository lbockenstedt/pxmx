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
sudo ./install_pxmx.sh --hub ws://<hub-host>:8765 --id pxmx-spoke-1 [--secret <psk>]
```
- Without `--secret`, the spoke connects unauthenticated and awaits admin
  approval in the LM WebUI.
- Installs to `/opt/lm`, creates a systemd service, and starts it.

### Ports
- LM Hub control plane: **443** wss (`/ws/spoke` — the spoke connects to this).
- pxmx agent listener: **443** wss **standalone (default)** — the spoke (on its own box) serves `wss://0.0.0.0:443` and a Proxmox agent dials `wss://<spoke>:443/ws/agent` directly (**agent → spoke → hub**; agent pinned via `--spoke-ip` — just the spoke's IP; the agent auto-determines the scheme/port/`/ws/agent` path by probing; a standalone spoke does not broadcast `_lm-hub` mDNS). **8443** loopback (co-located all-in-one, `--loopback`/`install_all.sh` only — `agent → hub → spoke`, hub `/ws/agent` byte-proxies to it). **8766** is the legacy no-cert plaintext fallback. See [docs/pxmx.md](docs/pxmx.md).

### Notable fix
A recurring **agent-blackout** bug (the agent-server task died and the spoke
stopped relaying to agents) was fixed in **v2.0.3** — the agent-server task is
now kept alive and self-heals on exit (`93f09df`). Current version: see `VERSION`.

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
- LM Hub repo: `vscode/lm` — see `docs/modules/core.md` and `docs/architecture.md`.
- cs source: `github.com/solutions-hpe`.