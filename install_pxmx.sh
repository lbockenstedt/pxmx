#!/bin/bash
set -e

# Default Configuration
HUB_URL="ws://localhost:8765"
SPOKE_ID="pxmx-spoke-1"
SPOKE_SECRET="lab-manager-secret"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub) HUB_URL="$2"; shift ;;
        --id|--name) SPOKE_ID="$2"; shift ;;
        --secret) SPOKE_SECRET="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# Auto-fetch secret if not provided
if [ -z "$SPOKE_SECRET" ] || [ "$SPOKE_SECRET" == "lab-manager-secret" ]; then
    echo "🔑 No secret provided. Attempting to fetch first-secret from Hub..."
    # Derive HTTP API URL from WebSocket URL (ws://host:8765 -> http://host:8000)
    HOST=$(echo "$HUB_URL" | sed 's|^ws://||' | cut -d: -f1)
    API_URL="http://$HOST:8000"

    SPOKE_SECRET=$(curl -s -X POST "$API_URL/setup/generate-secret" \
        -H "Content-Type: application/json" \
        -d "{\"spoke_id\": \"$SPOKE_ID\"}" | jq -r '.secret' 2>/dev/null)

    if [ "$SPOKE_SECRET" == "null" ] || [ -z "$SPOKE_SECRET" ]; then
        echo "⚠️  Could not fetch secret from Hub. Falling back to default."
        SPOKE_SECRET="lab-manager-secret"
    else
        echo "✅ Successfully fetched first-secret from Hub."
    fi
fi

echo "🚀 Installing Proxmox Manager Module (Native)..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

apt-get update
apt-get install -y python3-pip python3-venv git curl

INSTALL_DIR="/root/lab-manager"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ ! -d "lm/.git" ]; then
    echo "🌐 Cloning required Hub repository..."
    git clone https://github.com/lbockenstedt/lm.git
fi

if [ -d "pxmx/.git" ]; then
    echo "📂 PXMX repository already exists. Updating..."
    cd pxmx && git pull && cd ..
else
    echo "🌐 Cloning Proxmox Manager repository..."
    git clone https://github.com/lbockenstedt/pxmx.git
fi

echo "🛠️ Setting up Proxmox Manager..."
cd pxmx

if [ -d "venv" ] && [ ! -f "venv/bin/python3" ]; then
    rm -rf venv
fi
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
if [ ! -f "venv/bin/python3" ]; then
    echo "❌ Critical Error: venv creation failed."
    exit 1
fi

echo "Installing requirements..."
./venv/bin/python3 -m pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    ./venv/bin/python3 -m pip install -r requirements.txt
fi

# --- Persistence Configuration ---
echo "⚙️ Configuring Spoke Identity..."
cat <<EOF > .env
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
EOF

# --- Systemd Service (For Remote/Independent Deployment) ---
echo "⚙️ Creating systemd service for auto-start..."
cat <<EOF > /etc/systemd/system/lab-manager-pxmx.service
[Unit]
Description=Lab Manager Spoke - Proxmox Manager
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR/pxmx
ExecStart=$INSTALL_DIR/pxmx/venv/bin/python3 -m src.control_plane --id $SPOKE_ID --secret $SPOKE_SECRET --hub $HUB_URL
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lab-manager-pxmx

echo "🎉 Proxmox Manager installation complete!"
echo "🌐 Hub Target: $HUB_URL"
echo "🆔 Spoke ID: $SPOKE_ID"
echo "📦 Version: 0.08"
