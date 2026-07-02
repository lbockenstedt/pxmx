#!/bin/bash
set -e

# Default Configuration
SPOKE_URL=""
AGENT_ID="pxmx-agent-1"
AGENT_SECRET=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --spoke-url) SPOKE_URL="$2"; shift ;;
        --id) AGENT_ID="$2"; shift ;;
        --secret) AGENT_SECRET="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$SPOKE_URL" ]; then
    echo "❌ --spoke-url is required. Example: --spoke-url ws://<LM-SERVER-IP>:8766"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

echo "📦 Installing system dependencies..."
apt-get update
apt-get install -y python3-pip python3-venv git curl jq

echo "🚀 Installing Proxmox Local Agent..."

INSTALL_DIR="/opt/lm/pxmx/agent"
REPO_DIR="$INSTALL_DIR/.pxmx_repo"
mkdir -p "$INSTALL_DIR"

# ── Preserve existing AGENT_SECRET across reinstalls ──────────────────────────
# Precedence: --secret arg > existing .env value > empty (zero-touch)
EXISTING_SECRET=""
if [ -f "$INSTALL_DIR/.env" ]; then
    EXISTING_SECRET=$(grep "^AGENT_SECRET=" "$INSTALL_DIR/.env" 2>/dev/null \
                      | cut -d= -f2- | tr -d '\r\n' || true)
fi
FINAL_SECRET="${AGENT_SECRET:-$EXISTING_SECRET}"

if [ -z "$AGENT_SECRET" ] && [ -z "$EXISTING_SECRET" ]; then
    echo "ℹ️  No pre-shared secret. Agent will connect unauthenticated and await admin approval."
    echo "   Approve it in the LM WebUI (Setup → Spokes & Agents → Agents tile) to complete provisioning."
elif [ -z "$AGENT_SECRET" ] && [ -n "$EXISTING_SECRET" ]; then
    echo "🔑 Preserved existing agent secret."
fi

# ── Clone or update the repository ────────────────────────────────────────────
if [ -d "$REPO_DIR/.git" ]; then
    echo "📂 Updating agent repository..."
    git -C "$REPO_DIR" pull --rebase --autostash
else
    echo "🌐 Cloning Proxmox Agent repository..."
    git clone https://github.com/lbockenstedt/pxmx.git "$REPO_DIR"
fi

# ── Sync code from repo to install dir (preserve .env and venv) ───────────────
find "$REPO_DIR/agent" -mindepth 1 -maxdepth 1 \
    ! -name '.env' ! -name 'venv' \
    -exec cp -r {} "$INSTALL_DIR/" \;

# Copy the repo-root VERSION so get_version() and the install banner report a real version.
cp "$REPO_DIR/VERSION" "$INSTALL_DIR/VERSION" 2>/dev/null || true

# ── Virtualenv + requirements ──────────────────────────────────────────────────
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/python3" -m pip install --upgrade pip -q
if [ -f "$INSTALL_DIR/requirements.txt" ]; then
    "$INSTALL_DIR/venv/bin/python3" -m pip install -r "$INSTALL_DIR/requirements.txt" -q
fi

# ── Write .env (preserving secret) ────────────────────────────────────────────
cat <<EOF > "$INSTALL_DIR/.env"
SPOKE_URL=$SPOKE_URL
AGENT_ID=$AGENT_ID
AGENT_SECRET=$FINAL_SECRET
EOF

# ── Systemd service ───────────────────────────────────────────────────────────
cat <<EOF > /etc/systemd/system/lm-pxmx-agent.service
[Unit]
Description=Lab Manager - Local Proxmox Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 -m src.agent --spoke-url $SPOKE_URL --id $AGENT_ID
StandardOutput=append:/var/log/lm-pxmx-agent.log
StandardError=append:/var/log/lm-pxmx-agent.log
Restart=always
RestartSec=10
# Phase G: service-hang detection. The agent sends WATCHDOG=1 from its
# heartbeat loop (best-effort sd_notify; no-op outside systemd). With
# Type=simple + NotifyAccess=main, systemd restarts the agent if it stops
# notifying for WatchdogSec — catching a hung event loop that Restart=always
# (crash-only) would miss.
NotifyAccess=main
WatchdogSec=60

[Install]
WantedBy=multi-user.target
EOF

# ── Phase G: state-dir migration (/var/lib/client-sim → /var/lib/pxmx) ────────
# One-time fold of the retired bash agent's state into the unified agent's
# state dir (so e.g. orphan_vms.json survives the cutover). Idempotent via the
# .migrated marker; runs before the agent (re)starts so it sees the migrated
# state on first launch. cp -a merges into any existing /var/lib/pxmx.
if [ -d /var/lib/client-sim ] && [ ! -f /var/lib/pxmx/.migrated ]; then
    mkdir -p /var/lib/pxmx
    echo "📦 Migrating /var/lib/client-sim → /var/lib/pxmx ..."
    cp -a /var/lib/client-sim/. /var/lib/pxmx/ 2>/dev/null || true
    touch /var/lib/pxmx/.migrated
fi

# ── Phase G: gateway-loss net-watchdog (survives an agent crash) ───────────────
# Slimmed rename of the retired proxmox-watchdog.*: pings the default gateway
# and reboots the host if it has been unreachable for NET_DOWN_REBOOT_SECS. It
# is a separate timer precisely so it runs when the agent itself may be down.
cp "$INSTALL_DIR/lm-pxmx-net-watchdog.sh"     /usr/local/bin/lm-pxmx-net-watchdog 2>/dev/null || true
chmod 0755 /usr/local/bin/lm-pxmx-net-watchdog 2>/dev/null || true
cp "$INSTALL_DIR/lm-pxmx-net-watchdog.service" /etc/systemd/system/ 2>/dev/null || true
cp "$INSTALL_DIR/lm-pxmx-net-watchdog.timer"   /etc/systemd/system/ 2>/dev/null || true

# ── Kernel crash-hardening ────────────────────────────────────────────────────
# Re-provide the kernel-level recovery the legacy cs bash agent deployed
# (install-proxmox-agent.sh [6/7]) that the unified agent dropped in favor of
# systemd WatchdogSec= + sd_notify. That catches a hung *agent event loop* and
# the net-watchdog reboots on *gateway loss*, but neither detects a kernel
# hung-task, auto-reboots on a kernel panic/oops, or collects a crash dump.
# Use lm-pxmx-prefixed files so retire_bash_agent.sh (which removes only the
# old client-sim-prefixed ones) doesn't clobber these. Idempotent: re-runs only
# write when content changes.
SYSCTL_CONF="/etc/sysctl.d/99-lm-pxmx-watchdog.conf"
if [ ! -f "$SYSCTL_CONF" ] || ! grep -q "kernel.panic=10" "$SYSCTL_CONF" 2>/dev/null; then
    cat > "$SYSCTL_CONF" <<'SYSCTL'
# Lab Manager pxmx agent: detect and recover from kernel hangs / panics
kernel.hung_task_timeout_secs=120
kernel.panic=10
kernel.panic_on_oops=1
SYSCTL
    sysctl -p "$SYSCTL_CONF" >/dev/null 2>&1 \
        && echo "  OK: kernel hang/panic sysctl applied" \
        || echo "  WARNING: sysctl apply failed — settings take effect on next reboot"
fi

MODULES_CONF="/etc/modules-load.d/lm-pxmx-watchdog.conf"
if ! grep -q "^softdog" "$MODULES_CONF" 2>/dev/null; then
    echo "softdog" >> "$MODULES_CONF"
fi
modprobe softdog soft_margin=60 2>/dev/null \
    && echo "  OK: softdog watchdog module loaded" \
    || echo "  WARNING: softdog module unavailable — kernel-level reboot watchdog not active"

# Crash dumps to /var/crash/ (survive reboots). Best-effort — not all
# kernels/distros support kdump-tools.
if ! dpkg -l kdump-tools >/dev/null 2>&1; then
    if apt-get install -y -qq kdump-tools 2>/dev/null; then
        systemctl enable kdump-tools 2>/dev/null || true
        echo "  OK: kdump-tools installed — crash dumps written to /var/crash/"
    else
        echo "  INFO: kdump-tools unavailable on this kernel/distro — skipping crash dump setup"
    fi
fi

systemctl daemon-reload
systemctl enable --now lm-pxmx-net-watchdog.timer --no-block 2>/dev/null || true
systemctl enable lm-pxmx-agent
systemctl restart lm-pxmx-agent

echo "⏳ Verifying agent started..."
LOG_FILE="/var/log/lm-pxmx-agent.log"
MAX_RETRIES=10
COUNT=0
CONNECTED=false

while [ $COUNT -lt $MAX_RETRIES ]; do
    if grep -qE "Spoke identity verified|waiting for admin approval|APPROVAL_REQUIRED" "$LOG_FILE" 2>/dev/null; then
        CONNECTED=true
        break
    fi
    echo -n "."
    sleep 1
    ((COUNT++))
done

echo ""
if [ "$CONNECTED" = true ]; then
    if grep -q "waiting for admin approval" "$LOG_FILE" 2>/dev/null; then
        echo "⏳ Agent connected and waiting for admin approval."
        echo "   Go to the LM WebUI → Setup → Spokes & Agents → Agents tile to approve this agent."
    else
        echo "✅ Agent verified and connected successfully!"
    fi
else
    echo "❌ Agent did not connect within ${MAX_RETRIES}s."
    echo "👉 Check the logs: tail -n 20 $LOG_FILE"
fi

echo "🎉 Proxmox Local Agent installation complete!"
echo "🌐 Target Spoke: $SPOKE_URL"
echo "🆔 Agent ID: $AGENT_ID"
echo "📦 Version: $(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo unknown)"
