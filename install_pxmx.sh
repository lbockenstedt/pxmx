#!/bin/bash
set -e

# Default Configuration
HUB_URL="${HUB_URL:-}"
# Track whether the hub URL was explicitly given (arg or env). When NOT pinned
# the installer auto-discovers the hub via DNS (lm-hub.<dns-suffix>) then mDNS
# (_lm-hub._tcp.local.) after the venv is ready; if nothing is found HUB_URL is
# left empty and the spoke re-discovers at startup (BaseControlPlane.run).
HUB_URL_PINNED=0
[ -n "$HUB_URL" ] && HUB_URL_PINNED=1
# SPOKE_ID is OPTIONAL. When neither the SPOKE_ID env var nor --id is supplied
# the spoke derives its id from the current OS hostname at startup (see
# control_plane __main__) — so a cloned+renamed container reconnects under a new
# id (correlated to the old one via the install UUID) instead of being frozen to
# the hostname at install. A pinned --id (install_all.sh / explicit --id) wins.
SPOKE_ID="${SPOKE_ID:-}"
SPOKE_ID_PINNED=0
[ -n "$SPOKE_ID" ] && SPOKE_ID_PINNED=1
SPOKE_SECRET="lm-secret"

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub) HUB_URL="$2"; HUB_URL_PINNED=1; shift ;;
        --id|--name) SPOKE_ID="$2"; SPOKE_ID_PINNED=1; shift ;;
        --secret) SPOKE_SECRET="$2"; shift ;;
        --hub-secret) HUB_SECRET="$2"; shift ;;
        --all-prereqs) ;;  # no-op (system prereqs are always installed); accepted so the Hub's install-module call doesn't abort
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$SPOKE_SECRET" ] || [ "$SPOKE_SECRET" == "lm-secret" ]; then
    SPOKE_SECRET=""
    echo "ℹ️  No pre-shared secret — spoke will connect unauthenticated and await admin approval in the LM WebUI."
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
    cd pxmx && git pull --rebase --autostash && cd ..
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

# ── Hub auto-discovery ──────────────────────────────────────────────────────
# When --hub was not given (and no HUB_URL env), auto-locate the hub via DNS
# (lm-hub.<dns-suffix>) then mDNS (_lm-hub._tcp.local.) using the just-installed
# venv + the vendored src/discovery.py. If nothing is found, leave HUB_URL empty
# — the spoke re-discovers at startup (BaseControlPlane.run sentinel) once the
# hub is up. cwd is the pxmx repo ($INSTALL_DIR/pxmx) so `src.discovery` imports.
if [ "$HUB_URL_PINNED" != "1" ]; then
    echo "🔎 No --hub given; auto-discovering the LM hub (DNS lm-hub.* / mDNS)…"
    DISCOVERED=$(./venv/bin/python3 -m src.discovery --timeout 5 2>/dev/null || echo NONE)
    if [ -n "$DISCOVERED" ] && [ "$DISCOVERED" != "NONE" ]; then
        HUB_URL="$DISCOVERED"
        echo "✅ Discovered hub: $HUB_URL"
    else
        echo "⚠️  Hub not found via DNS/mDNS. Leaving HUB_URL empty — the spoke will"
        echo "    retry auto-discovery at startup. To pin it now, re-run with"
        echo "    --hub ws://HUB:8765 (or create an 'lm-hub' DNS record / enable mDNS on the hub)."
        HUB_URL=""
    fi
fi

# --- Persistence Configuration ---
echo "⚙️ Configuring Spoke Identity..."
# Bake SPOKE_ID into .env + the unit ONLY when it was explicitly pinned. In the
# derived case Python computes `<hostname>-spoke` at startup, so a clone that
# was renamed reconnects under a new id (correlated to the old one via the
# install UUID). INSTALL_UUID is NOT written here — the spoke mints it at first
# start. ID_ARG uses \$SPOKE_ID so systemd expands it from EnvironmentFile.
SPOKE_ID_LINE=""
ID_ARG=""
if [ "$SPOKE_ID_PINNED" = "1" ]; then
    SPOKE_ID_LINE="SPOKE_ID=$SPOKE_ID"
    ID_ARG="--id \$SPOKE_ID"
fi
cat <<EOF > .env
HUB_URL=$HUB_URL
${SPOKE_ID_LINE}
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=$HUB_SECRET
EOF

# --- Agent Secret (shared with local Proxmox agent on this machine) ---
# Preserve an existing agent_secret so a re-install doesn't break a running agent.
AGENT_CONFIG="/etc/lm-agent/config.json"
EXISTING_AGENT_SECRET=""
if [ -f "$AGENT_CONFIG" ]; then
    EXISTING_AGENT_SECRET=$(python3 -c "import json,sys; d=json.load(open('$AGENT_CONFIG')); print(d.get('agent_secret',''))" 2>/dev/null || true)
fi

if [ -z "$EXISTING_AGENT_SECRET" ]; then
    AGENT_SECRET=$(openssl rand -base64 32 | tr -d '/+=\n')
    echo "🔑 Generated new agent_secret."
else
    AGENT_SECRET="$EXISTING_AGENT_SECRET"
    echo "🔑 Preserved existing agent_secret."
fi

mkdir -p /etc/lm-agent
python3 -c "
import json, sys
path = '$AGENT_CONFIG'
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {}
data['agent_secret'] = '$AGENT_SECRET'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
"
chmod 600 "$AGENT_CONFIG"
chown svc_lm:svc_lm "$AGENT_CONFIG" 2>/dev/null || true
echo "✅ Agent secret written to $AGENT_CONFIG"

# --- Systemd Service (For Remote/Independent Deployment) ---
echo "⚙️ Creating systemd service for auto-start..."

# Only pass --secret when a value is present; zero-touch provisioning handles the empty case
SECRET_ARG=""
[ -n "$SPOKE_SECRET" ] && SECRET_ARG="--secret=$SPOKE_SECRET"
HUB_SECRET_ARG=""
[ -n "${HUB_SECRET:-}" ] && HUB_SECRET_ARG="--hub-secret=$HUB_SECRET"

cat <<EOF > /etc/systemd/system/lm-pxmx.service
[Unit]
Description=Lab Manager Spoke - Proxmox Manager
After=network.target

[Service]
Type=simple
User=svc_lm
WorkingDirectory=$INSTALL_DIR/pxmx
Environment="PYTHONPATH=$INSTALL_DIR:$INSTALL_DIR/core/src:$INSTALL_DIR/pxmx/src"
EnvironmentFile=$INSTALL_DIR/pxmx/.env
ExecStart=$INSTALL_DIR/pxmx/venv/bin/python3 -m src.control_plane $ID_ARG --hub "\${HUB_URL}" $SECRET_ARG $HUB_SECRET_ARG
StandardOutput=append:/var/log/lm/lm-pxmx.log
StandardError=append:/var/log/lm/lm-pxmx.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable lm-pxmx

# Apply new code now and prevent split-brain: stop the current instance, reap
# any orphaned/stale pxmx control_plane process left by a previous install
# (different unit or invocation), then start fresh. A stale instance holding
# :8766 while a new one reaches the hub with no agent is exactly the split-brain
# that makes the node agent invisible in the UI.
systemctl stop lm-pxmx 2>/dev/null || true
pkill -f 'control_plane.*--id pxmx-spoke-1' 2>/dev/null || true
sleep 1
systemctl start lm-pxmx

echo "🎉 Proxmox Manager installation complete!"
if [ -n "$HUB_URL" ]; then
    echo "🌐 Hub Target: $HUB_URL"
else
    echo "🌐 Hub Target: (auto-discover at startup — no lm-hub DNS/mDNS found yet)"
fi
if [ "$SPOKE_ID_PINNED" = "1" ]; then
    echo "🆔 Spoke ID: $SPOKE_ID  (pinned)"
else
    echo "🆔 Spoke ID: $(hostname -s)-spoke  (derived from hostname at startup)"
fi
echo "📦 Version: $(cat VERSION 2>/dev/null || echo unknown)"

# Print the agent install command so the admin knows what to run on each Proxmox node
LM_HOST=$(echo "$HUB_URL" | sed 's|^ws://||' | cut -d: -f1)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Run this on each Proxmox node to install the pxmx agent:"
echo ""
if [ -n "$LM_HOST" ]; then
    echo "  curl -sSL https://raw.githubusercontent.com/lbockenstedt/pxmx/main/agent/install_agent.sh \\"
    echo "    | sudo bash -s -- \\"
    echo "    --spoke-url ws://${LM_HOST}:8766"
else
    echo "  curl -sSL https://raw.githubusercontent.com/lbockenstedt/pxmx/main/agent/install_agent.sh \\"
    echo "    | sudo bash"
    echo "  (no --spoke-url: the agent auto-discovers the hub via DNS lm-hub.* / mDNS)"
fi
echo "  (omitting --id derives <hostname>-agent; clone+rename auto-correlates via install UUID)"
echo ""
echo "  The agent will appear as 'Pending' in the LM WebUI (Setup → Spokes & Agents → Agents tile)."
echo "  Approve it there and the authentication secret will be provisioned automatically."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
