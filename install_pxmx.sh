#!/bin/bash
set -e

# Default Configuration
HUB_URL="ws://localhost:8765"
SPOKE_ID="pxmx-spoke-1"
SPOKE_SECRET="lm-secret"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub) HUB_URL="$2"; shift ;;
        --id|--name) SPOKE_ID="$2"; shift ;;
        --secret) SPOKE_SECRET="$2"; shift ;;
        --hub-secret) HUB_SECRET="$2"; shift ;;
        --admin-token) ADMIN_TOKEN="$2"; shift ;;
        --all-prereqs) ;;  # no-op (system prereqs are always installed); accepted so the Hub's install-module call doesn't abort
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# Admin token for auto-fetch (env fallback: LM_ADMIN_TOKEN)
ADMIN_TOKEN="${ADMIN_TOKEN:-$LM_ADMIN_TOKEN}"

# Auto-fetch secret if not provided. /setup/generate-secret is auth-protected,
# so a Bearer admin token is required to mint a first-secret. If you cannot
# provide one, pass --secret <first-secret> from the Hub dashboard instead.
if [ -z "$SPOKE_SECRET" ] || [ "$SPOKE_SECRET" == "lm-secret" ]; then
    if [ -z "$ADMIN_TOKEN" ]; then
        echo "❌ No spoke secret provided and no admin token to fetch one."
        echo "   Provide --secret <first-secret> (from the Hub dashboard), or"
        echo "   --admin-token <LM_ADMIN_TOKEN> / export LM_ADMIN_TOKEN to auto-fetch."
        exit 1
    fi
    echo "🔑 No secret provided. Fetching first-secret from Hub with admin token..."
    HOST=$(echo "$HUB_URL" | sed 's|^ws://||' | cut -d: -f1)
    API_URL="http://$HOST:8000"

    SPOKE_SECRET=$(curl -s -X POST "$API_URL/setup/generate-secret" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $ADMIN_TOKEN" \
        -d "{\"spoke_id\": \"$SPOKE_ID\"}" | jq -r '.secret' 2>/dev/null) || true

    if [ "$SPOKE_SECRET" == "null" ] || [ -z "$SPOKE_SECRET" ]; then
        echo "❌ Could not fetch secret from Hub (verify LM_ADMIN_TOKEN, Hub URL, and spoke_id)."
        echo "   Alternatively, provide --secret <first-secret> from the Hub dashboard."
        exit 1
    fi
    echo "✅ Successfully fetched first-secret from Hub."
fi

echo "🚀 Installing Proxmox Manager Module (Native)..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

apt-get update
apt-get install -y python3-pip python3-venv git curl

INSTALL_DIR="/opt/lm"
OLD_INSTALL_DIR="/opt/lm-manager"

# Cleanup legacy installation
if [ -d "$OLD_INSTALL_DIR" ]; then
    echo "🗑️  Removing legacy installation at $OLD_INSTALL_DIR..."
    rm -rf "$OLD_INSTALL_DIR"
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ -d "pxmx/.git" ]; then
    echo "📂 PXMX repository already exists. Updating..."
    cd pxmx && git pull && cd ..
else
    echo "🌐 Cloning Proxmox Manager repository..."
    git clone https://github.com/lbockenstedt/pxmx.git
fi

echo "🛠️ Setting up Proxmox Manager..."
cd pxmx

# Always remove existing venv to ensure clean local environment (prevents cross-platform path issues)
echo "♻️ Resetting virtual environment..."
rm -rf venv

python3 -m venv venv
if [ ! -f "venv/bin/python3" ]; then
    echo "❌ Critical Error: venv creation failed."
    exit 1
fi

echo "Installing requirements..."
./venv/bin/python3 -m pip install --upgrade pip -q
if [ -f "requirements.txt" ]; then
    ./venv/bin/python3 -m pip install -r requirements.txt -q
fi

# --- Persistence Configuration ---
echo "⚙️ Configuring Spoke Identity..."
cat <<EOF > .env
HUB_URL=$HUB_URL
SPOKE_ID=$SPOKE_ID
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=$HUB_SECRET
EOF

# --- Systemd Service (For Remote/Independent Deployment) ---
echo "⚙️ Creating systemd service for auto-start..."
cat <<EOF > /etc/systemd/system/lm-pxmx.service
[Unit]
Description=Lab Manager Spoke - Proxmox Manager
After=network.target

[Service]
Type=simple
User=svc_lm
WorkingDirectory=$INSTALL_DIR/pxmx
Environment="PYTHONPATH=$INSTALL_DIR/core/src:$INSTALL_DIR/pxmx/src"
ExecStart=$INSTALL_DIR/pxmx/venv/bin/python3 -m src.control_plane --id $SPOKE_ID --secret $SPOKE_SECRET --hub $HUB_URL --hub-secret $HUB_SECRET
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lm-pxmx

echo "🎉 Proxmox Manager installation complete!"
echo "🌐 Hub Target: $HUB_URL"
echo "🆔 Spoke ID: $SPOKE_ID"
echo "📦 Version: $(cat VERSION 2>/dev/null || echo unknown)"
