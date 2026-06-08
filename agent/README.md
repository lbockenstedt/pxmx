# 🤖 Proxmox Local Agent

The Proxmox Local Agent is a lightweight service that runs directly on the Proxmox hypervisor. It bridges the gap between the Lab Manager Proxmox Spoke and the actual hardware/API of the Proxmox host.

## 🛠️ Architecture
The agent follows a "Push-Pull" model:
- **Push**: Every 60 seconds, the agent pushes system telemetry (CPU, RAM, Disk) and the current VM list to the Spoke.
- **Pull**: The Spoke can send specific commands (e.g., `AGENT_GET_VM_INFO`, `AGENT_SHELLEXEC`) which the agent executes and returns.

## 🚀 Installation

### Local Installation (Same Host)
If the agent is on the same machine as the Proxmox Spoke:
```bash
bash install_agent.sh
```

### Remote Installation
If the agent is on a separate host from the Spoke:
```bash
bash install_agent.sh --spoke-url ws://<SPOKE_IP>:8766
```

## ⚙️ Configuration
- **Port**: The agent connects to the Proxmox Spoke on port `8766`.
- **Authentication**: Uses a shared secret (`pxmx-agent-secret` by default) for a secure handshake.
- **Systemd**: Installed as `lm-pxmx-agent.service`.

## 📊 Collected Data
- **System Metrics**: CPU usage, Virtual Memory usage, and Disk usage via `psutil`.
- **VM Metadata**: Real-time lists of VMs, their status, and resource consumption.
- **Remote Execution**: Ability to execute approved shell commands on the host.
