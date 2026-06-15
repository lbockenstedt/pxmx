#!/bin/bash
set -e

# Default Configuration
SPOKE_URL="ws://localhost:8766"
AGENT_ID="pxmx-agent-1"
AGENT_SECRET=""
HUB_SECRET=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --spoke-url) SPOKE_URL="$2"; shift ;;
        --id) AGENT_ID="$2"; shift ;;
        --secret) AGENT_SECRET="$2"; shift ;;
        --hub-secret) HUB_SECRET="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# Auto-fetch secret if not provided
if [ -z "$AGENT_SECRET" ]; then
    echo "🔑 No secret provided. Attempting to fetch first-secret from Hub..."
    # Extract hostname/IP from ws://hostname:port
    API_HOST=$(echo $SPOKE_URL | sed 's/ws\:\/\///' | cut -d: -f1)
    API_URL="http://$API_HOST:8000"

    AGENT_SECRET=$(curl -s -X POST "$API_URL/setup/generate-secret" \
        -H "Content-Type: application/json" \
        -d "{\"spoke_id\": \"$AGENT_ID\"}" | jq -r '.secret' 2>/dev/null)

    if [ "$AGENT_SECRET" == "null" ] || [ -z "$AGENT_SECRET" ]; then
        echo "❌ Could not fetch secret from Hub. Please provide --secret manually."
        exit 1
    else
        echo "✅ Successfully fetched first-secret from Hub."
    fi
fi

echo "🚀 Installing Proxmox Local Agent (Direct from GitHub)..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

apt-get update
apt-get install -y python3-pip python3-venv git curl jq

INSTALL_DIR="/opt/lm/pxmx/agent"
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
StandardOutput=append:/var/log/lm-pxmx-agent.log
StandardError=append:/var/log/lm-pxmx-agent.log
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
