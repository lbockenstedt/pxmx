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
# TLS cert verification is OFF by default (self-signed hub cert → encrypt
# without auth). Pass --tls-verify --tls-ca-cert <path> to make this spoke
# verify the hub cert. A standalone pxmx spoke is on a different box than the
# hub, so the hub CA cert MUST be supplied (--tls-ca-cert) — there is no local
# hub cert to default to.
TLS_VERIFY=false
TLS_CA_CERT=""
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --hub) HUB_URL="$2"; HUB_URL_PINNED=1; shift ;;
        --id|--name) SPOKE_ID="$2"; SPOKE_ID_PINNED=1; shift ;;
        --secret) SPOKE_SECRET="$2"; shift ;;
        --hub-secret) HUB_SECRET="$2"; shift ;;
        --tls-verify)  TLS_VERIFY=true ;;
        --tls-ca-cert) shift; TLS_CA_CERT="$1" ;;
        --all-prereqs) ;;  # no-op (system prereqs are always installed); accepted so the Hub's install-module call doesn't abort
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if $TLS_VERIFY && [ -z "$TLS_CA_CERT" ]; then
    echo "❌ --tls-verify requires --tls-ca-cert <path> on a standalone spoke (the hub CA cert is not on this box)."
    exit 1
fi
if $TLS_VERIFY; then
    HUB_TLS_VERIFY_ENV=1
    HUB_TLS_CA_ENV="$TLS_CA_CERT"
else
    HUB_TLS_VERIFY_ENV=0
    HUB_TLS_CA_ENV=""
fi

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

# ── TLS for the pxmx agent listener (remote Proxmox agents report in) ──────
# A standalone pxmx spoke serves wss on LM_PXMX_AGENT_PORT (443 here — this box
# isn't the hub, so 443 is free). The cert is self-signed; agents skip
# verification by default (set LM_HUB_TLS_VERIFY=1 + LM_HUB_CA_CERT to verify).
# Skip gracefully if openssl is absent → listener stays plaintext on :8766.
PXMX_CERT_DIR="$INSTALL_DIR/pxmx/certs"
PXMX_CERT="$PXMX_CERT_DIR/hub.crt"
PXMX_KEY="$PXMX_CERT_DIR/hub.key"
mkdir -p "$PXMX_CERT_DIR"
if ! command -v openssl >/dev/null 2>&1; then
    echo "⚠️  openssl not found — skipping pxmx TLS cert (agent listener stays plaintext :8766)."
elif [ -f "$PXMX_CERT" ] && [ -f "$PXMX_KEY" ]; then
    echo "🔒 pxmx TLS cert already present at $PXMX_CERT — preserving."
else
    echo "🔒 Generating self-signed pxmx TLS cert at $PXMX_CERT…"
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$PXMX_KEY" -out "$PXMX_CERT" -days 3650 \
        -subj "/CN=lm-pxmx" -addext "subjectAltName=IP:127.0.0.1,DNS:lm-hub,DNS:lm-hub.local" \
        >/dev/null 2>&1 || echo "⚠️  openssl cert generation failed — agent listener stays plaintext."
fi
if [ -f "$PXMX_KEY" ]; then
    chmod 600 "$PXMX_KEY"
    chown svc_lm:svc_lm "$PXMX_KEY" "$PXMX_CERT" 2>/dev/null || true
fi
# Persist the TLS knobs into .env (the unit's EnvironmentFile loads these).
if ! grep -q "^LM_TLS_CERT=" .env 2>/dev/null; then
    {
        echo "LM_TLS_CERT=$PXMX_CERT"
        echo "LM_TLS_KEY=$PXMX_KEY"
        echo "LM_PXMX_AGENT_PORT=443"
        echo "LM_HUB_TLS_VERIFY=$HUB_TLS_VERIFY_ENV"
        [ -n "$HUB_TLS_CA_ENV" ] && echo "LM_HUB_CA_CERT=$HUB_TLS_CA_ENV"
    } >> .env
fi
# A re-install toggling --tls-verify should update an existing .env, not leave
# a stale setting from a prior install.
if [ -f .env ]; then
    sed -i "s|^LM_HUB_TLS_VERIFY=.*|LM_HUB_TLS_VERIFY=$HUB_TLS_VERIFY_ENV|" .env 2>/dev/null || true
    if [ -n "$HUB_TLS_CA_ENV" ]; then
        grep -q "^LM_HUB_CA_CERT=" .env 2>/dev/null \
            && sed -i "s|^LM_HUB_CA_CERT=.*|LM_HUB_CA_CERT=$HUB_TLS_CA_ENV|" .env \
            || echo "LM_HUB_CA_CERT=$HUB_TLS_CA_ENV" >> .env
    else
        sed -i "/^LM_HUB_CA_CERT=/d" .env 2>/dev/null || true
    fi
fi

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

# Build the verify fragment for the unit Environment line (empty when off).
_TLS_CA_UNIT=""
[ -n "$HUB_TLS_CA_ENV" ] && _TLS_CA_UNIT=" LM_HUB_CA_CERT=$HUB_TLS_CA_ENV"

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
# TLS: the pxmx agent listener serves wss on LM_PXMX_AGENT_PORT (443 for a
# standalone pxmx spoke). Cert verification OFF by default; --tls-verify at
# install sets LM_HUB_TLS_VERIFY=1 + LM_HUB_CA_CERT so this spoke verifies the
# hub. AmbientCapabilities lets svc_lm bind 443 non-root.
Environment=LM_PXMX_AGENT_PORT=443 LM_HUB_TLS_VERIFY=$HUB_TLS_VERIFY_ENV$_TLS_CA_UNIT
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
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

# ── Failed-update rollback watchdog + sudoers ─────────────────────────────────
# Per-spoke recovery state lives in /var/lib/lm/<spoke_id>/ (created on demand at
# runtime by the spoke): pre-swap code snapshot, pending-update manifest, healthy
# marker, bad-commit registry. The external health-gate watchdog below reads them
# and rolls back a self-update that crashes at boot (git reset --hard <from_commit>)
# instead of letting it crash-loop forever under Restart=always. The spoke (svc_lm)
# schedules it via `sudo -n` right before it os._exit(3)s to load new code; the
# sudoers entry grants only this path. Mirrors the hub's lm-update-restart.
cat > /usr/local/bin/lm-component-update-restart <<'HELPER'
#!/bin/bash
# lm-component-update-restart — external health-gate watchdog for spoke/agent
# self-updates. Scheduled by the component (sudo -n for spokes, direct for the
# root agent) right before it exits to load new code. Runs OUTSIDE the
# component's systemd cgroup (via systemd-run) so it survives the component's
# restart and can roll back a failed update instead of letting it crash-loop
# forever under Restart=always.
#
# Rollback policy: the watchdog waits up to --deadline for a `healthy` marker
# (written by the component after it re-auths with the hub/spoke). If instead
# it sees a crash-loop (NRestarts >= 3) or a failed/inactive unit, it rolls
# back — `git reset --hard <from_commit>` for a spoke (--repo-root, a git repo)
# or a file-tree restore for the agent (--install-dir, non-git) — marks the
# version/commit bad so the next update skips it, and restarts the component.
# A unit that is active-and-running but hasn't written the marker (the hub/spoke
# is unreachable so the component can't auth) is NOT rolled back — the code
# booted; the missing marker is a connectivity issue, not a code failure, and
# rolling back a good update during a hub outage would strand the component on
# old code and mark a good commit/version bad.
#
# State-file ops delegate to the Python CLI update_recovery.py (SINGLE SOURCE OF
# TRUTH for the on-disk recovery state machine). Only poll/systemd/git logic
# lives here. This file is the canonical source; install_cs.sh / install_pxmx.sh
# / install_agent.sh embed it verbatim via here-doc — keep them in sync.
set -uo pipefail

UNIT="" STATE_DIR="" REPO_ROOT="" INSTALL_DIR="" DEADLINE=90
RECOVERY_PY="/opt/lm/core/src/update_recovery.py"

# Re-exec under a transient systemd unit outside the component's cgroup so this
# process survives the `systemctl restart <unit>` it issues (otherwise the
# restart kills us before we can poll or roll back). The guard prevents an
# infinite re-exec loop. Mirrors lm-update-restart's transient-unit trick.
if [ -z "${LM_COMP_UPDATE_GUARD:-}" ]; then
    export LM_COMP_UPDATE_GUARD=1
    exec systemd-run --no-block --quiet --collect \
        --unit="lm-comp-update-$$-$RANDOM" --service-type=oneshot \
        --setenv=LM_COMP_UPDATE_GUARD=1 \
        /usr/local/bin/lm-component-update-restart "$@"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --unit) UNIT="$2"; shift 2;;
        --state-dir) STATE_DIR="$2"; shift 2;;
        --repo-root) REPO_ROOT="$2"; shift 2;;
        --install-dir) INSTALL_DIR="$2"; shift 2;;
        --deadline) DEADLINE="$2"; shift 2;;
        --recovery-py) RECOVERY_PY="$2"; shift 2;;
        *) shift;;
    esac
done

HEALTHY="$STATE_DIR/healthy"
PENDING="$STATE_DIR/pending_update.json"

# 0 if the component is healthy (marker present) OR booted-but-pending-auth
# (active, not crash-looping); 1 if still failing (crash-loop / failed / unknown).
unit_ok() {
    [ -f "$HEALTHY" ] && return 0
    local a n
    a="$(systemctl show "$UNIT" -p ActiveState --value 2>/dev/null || echo "")"
    n="$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo 0)"
    n="${n:-0}"
    [ "$a" = "active" ] && [ "$n" -lt 3 ] && return 0
    return 1
}

clear_and_prune() {
    python3 "$RECOVERY_PY" clearpending --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    python3 "$RECOVERY_PY" prune --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
}

# 1) Wait up to DEADLINE for the new code to boot + re-auth (healthy marker).
waited=0
while [ "$waited" -lt "$DEADLINE" ]; do
    if [ -f "$HEALTHY" ]; then
        clear_and_prune
        exit 0
    fi
    sleep 5; waited=$((waited + 5))
done

# 2) Deadline elapsed, no marker. Active-and-stable → connectivity, not code.
if unit_ok; then
    echo "lm-component-update-restart: $UNIT active but no healthy marker within ${DEADLINE}s — assuming hub/spoke unreachable (not a code failure); no rollback." >&2
    clear_and_prune
    exit 0
fi

# 3) Crash-loop or failed → roll back to the pre-swap code.
pending="$(cat "$PENDING" 2>/dev/null || true)"
bdir="$(printf '%s' "$pending" | jq -r '.backup_dir // empty' 2>/dev/null)"
from_commit="$(printf '%s' "$pending" | jq -r '.from_commit // empty' 2>/dev/null)"
to_commit="$(printf '%s' "$pending" | jq -r '.to_commit // empty' 2>/dev/null)"
to_v="$(printf '%s' "$pending" | jq -r '.to_version // empty' 2>/dev/null)"

echo "lm-component-update-restart: $UNIT failed to boot (crash-loop/failed); rolling back." >&2

if [ -n "$REPO_ROOT" ]; then
    # Spoke (git repo): reset hard to the pre-update commit + clean stray files.
    if [ -n "$from_commit" ]; then
        git -C "$REPO_ROOT" reset --hard "$from_commit" >/dev/null 2>&1 || true
        git -C "$REPO_ROOT" clean -fd >/dev/null 2>&1 || true
    fi
    if [ -n "$to_commit" ]; then
        python3 "$RECOVERY_PY" markbadcommit "$to_commit" --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    fi
elif [ -n "$INSTALL_DIR" ]; then
    # Agent (non-git install dir): file-tree restore from the pre-swap snapshot.
    if [ -n "$bdir" ] && [ -d "$bdir/src" ]; then
        python3 "$RECOVERY_PY" rollback --hub-root "$INSTALL_DIR" --backup-dir "$bdir" \
            --tree src --state-dir "$STATE_DIR" --chown-user root >/dev/null 2>&1 || true
    fi
    if [ -n "$to_v" ]; then
        python3 "$RECOVERY_PY" markbad "$to_v" --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    fi
fi

python3 "$RECOVERY_PY" clearpending --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
systemctl restart "$UNIT" 2>/dev/null || true

# 4) Did the rolled-back code come back? (marker OR active-and-stable.)
waited=0
while [ "$waited" -lt 30 ]; do
    if unit_ok; then
        echo "lm-component-update-restart: $UNIT rolled back; marked bad; recovered." >&2
        python3 "$RECOVERY_PY" prune --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
        exit 0
    fi
    sleep 5; waited=$((waited + 5))
done

# 5) Rolled-back code ALSO failed — last-resort marker for manual recovery.
python3 "$RECOVERY_PY" writefailed --to-version "${to_v:-${to_commit:-unknown}}" \
    --backup-dir "$bdir" --reason "rollback did not come healthy within 30s" \
    --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
echo "lm-component-update-restart: $UNIT rollback also failed; left for manual recovery (snapshot at $bdir)." >&2
exit 1
HELPER
chmod 0755 /usr/local/bin/lm-component-update-restart
cat > /etc/sudoers.d/lm-component-update <<SUDOERS
svc_lm ALL=(ALL) NOPASSWD: /usr/local/bin/lm-component-update-restart
SUDOERS
chmod 0440 /etc/sudoers.d/lm-component-update
visudo -cf /etc/sudoers.d/lm-component-update >/dev/null 2>&1 || true

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
echo "🛡️  Rollback: /usr/local/bin/lm-component-update-restart — a failed self-update"
echo "    (crash at boot) is rolled back to the prior commit automatically. NOTE:"
echo "    this watchdog + sudoers land only on a full installer re-run; a box that"
echo "    only git-pulled the new spoke code must be re-installed once to enable it."

# Print the agent install command so the admin knows what to run on each Proxmox node.
# Default to mDNS/DNS auto-discovery: the agent reads this box's _lm-hub._tcp TXT
# agent_port record (443 on a standalone pxmx spoke with TLS on, 8766 legacy no-TLS)
# and picks ws:// vs wss:// automatically — no --spoke-url / port needed. Pinning is
# shown only as an optional fallback.
LM_HOST=$(echo "$HUB_URL" | sed 's|^wss://||;s|^ws://||' | cut -d: -f1)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Run this on each Proxmox node to install the pxmx agent:"
echo ""
echo "  curl -sSL https://raw.githubusercontent.com/lbockenstedt/pxmx/main/agent/install_agent.sh \\"
echo "    | sudo bash"
echo "  (auto-discovers this spoke via DNS lm-hub.* / mDNS _lm-hub._tcp — no port needed;"
echo "   the agent reads the agent_port TXT record, 443 here with TLS on.)"
if [ -n "$LM_HOST" ]; then
    echo "  To pin instead:  --spoke-url wss://${LM_HOST}:443   (or ws://${LM_HOST}:8766 with TLS off)"
fi
echo "  (omitting --id derives <hostname>-agent; clone+rename auto-correlates via install UUID)"
echo ""
echo "  The agent will appear as 'Pending' in the LM WebUI (Setup → Spokes & Agents → Agents tile)."
echo "  Approve it there and the authentication secret will be provisioned automatically."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
