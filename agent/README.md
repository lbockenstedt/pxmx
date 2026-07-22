# 🤖 Proxmox Local Agent

The Proxmox Local Agent is a lightweight service that runs directly on the Proxmox hypervisor. It bridges the gap between the Lab Manager Proxmox Spoke and the actual hardware/API of the Proxmox host.

## 🛠️ Architecture
The agent follows a "Push-Pull" model:
- **Push**: Every 60 seconds, the agent pushes system telemetry (CPU, RAM, Disk) and the current VM list to the Spoke.
- **Pull**: The Spoke can send specific commands (e.g., `AGENT_GET_VM_INFO`, `AGENT_SHELLEXEC`) which the agent executes and returns.

## 🚀 Installation

### One-Liner Installation (Recommended)
The fastest way to install the agent and connect it to your Spoke — supply just
the spoke's IP; the agent auto-determines the scheme, port, and `/ws/agent` path
by probing (`--spoke-ip` works for cs spokes on `:443`/`:8767` and pxmx spokes
alike):
```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/pxmx/main/agent/install_agent.sh | bash -s -- --spoke-ip <SPOKE_IP>
```

### Local Installation (Same Host)
If the agent is on the same machine as the Proxmox Spoke:
```bash
bash install_agent.sh
```
When run on a Proxmox host with **no `--spoke-ip`/`--spoke-url`**, the installer
first auto-detects a **co-located LM spoke running as an LXC** on this host: it
enumerates running containers (`pct list`), resolves each one's IP, and probes it
for an LM agent listener — the first match is pinned as `--spoke-ip` automatically.
If no local container answers (or `pct` isn't present) it falls back to hub
discovery (DNS `lm-hub.*` / mDNS). Pass `--spoke-ip <IP>` to skip detection.

### Remote Installation
If the agent is on a separate host from the Spoke — just the IP:
```bash
bash install_agent.sh --spoke-ip <SPOKE_IP>
```

> Advanced: `--spoke-url ws(s)://<host>:<port>/ws/agent` still pins a fully-formed
> URL verbatim (and wins over `--spoke-ip`). Prefer `--spoke-ip` unless you have a
> reason to override the scheme/port yourself.

## ⚙️ Configuration
- **Port**: The agent connects to the Proxmox Spoke on port `8766`.
- **Authentication**: Uses a shared secret (`pxmx-agent-secret` by default) for a secure handshake.
- **Systemd**: Installed as `lm-pxmx-agent.service`.

## 📊 Collected Data
- **System Metrics**: CPU usage, Virtual Memory usage, and Disk usage via `psutil`.
- **VM Metadata**: Real-time lists of VMs, their status, and resource consumption.
- **Remote Execution**: Ability to execute approved shell commands on the host.
