#!/bin/bash
set -e

# Default Configuration
SPOKE_URL="ws://localhost:8766"
AGENT_ID="pxmx-agent-1"
AGENT_SECRET="pxmx-agent-secret"

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

echo "🚀 Installing Proxmox Local Agent (Direct from GitHub)..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

apt-get update
apt-get install -y python3-pip python3-venv git curl jq

INSTALL_DIR="/root/lm/pxmx/agent"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Clone or update the agent repository
if [ -d ".git" ]; then
    echo "📂 Agent repository already exists. Updating..."
    git pull
else
    echo "🌐 Cloning Proxmox Agent repository..."
    TMP_CLONE="/tmp/pxmx_clone_$(date +%s)"
    git clone https://github.com/lbockenstedt/pxmx.git "$TMP_CLONE"
    cp -r "$TMP_CLONE/agent/." .
    rm -rf "$TMP_CLONE"
fi

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

./venv/bin/python3 -m pip install --upgrade pip -q
if [ -f "requirements.txt" ]; then
    ./venv/bin/python3 -m pip install -r requirements.txt -q
fi

echo "⚙️ Configuring Agent Identity..."
cat <<EOF > .env
SPOKE_URL=$SPOKE_URL
AGENT_ID=$AGENT_ID
AGENT_SECRET=$AGENT_SECRET
EOF

echo "⚙️ Creating systemd service..."
cat <<EOF > /etc/systemd/system/lm-pxmx-agent.service
[Unit]
Description=Lab Manager - Local Proxmox Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 -m src.agent --spoke-url $SPOKE_URL --id $AGENT_ID --secret $AGENT_SECRET
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lm-pxmx-agent
systemctl restart lm-pxmx-agent

echo "🎉 Proxmox Local Agent installation complete!"
echo "🌐 Target Spoke: $SPOKE_URL"
echo "🆔 Agent ID: $AGENT_ID"
echo "📦 Version: 0.01"
