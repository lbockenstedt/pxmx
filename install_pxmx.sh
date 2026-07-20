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
# Agent-listener mode. DEFAULT is standalone: this pxmx spoke lives on its OWN
# box, serves wss on :443 directly so a remote Proxmox agent dials
# wss://<this-spoke>:443/ws/agent, and this spoke talks to the hub outbound
# (agent → spoke → hub). --loopback flips to all-in-one/co-located mode (hub on
# the SAME box): the listener binds 127.0.0.1:8443 plaintext and the hub's
# /ws/agent route byte-proxies to it (the hub owns :443). --loopback is intended
# to be passed ONLY by install_all.sh (the rare co-located all-in-one path); a
# standalone install never sets it. See docs/pxmx.md "Agent listener modes".
PXMX_LOOPBACK=0

# --infra-only: run ONLY the host/OS-level agent-host prep (setup_pxmx_host) and
# early-exit — no venv, no clone, no .env, no lm-pxmx unit. This is the entry
# point the generic agent's proxmox-role install calls
# (`bash /opt/lm/pxmx/install_pxmx.sh --infra-only`) when it hosts the pxmx spoke
# IN-PROCESS: the agent owns the spoke code, venv, .env, updates and unit, so
# --infra-only must NOT touch any of those. Idempotent + non-interactive.
INFRA_ONLY=0

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
        --loopback) PXMX_LOOPBACK=1 ;;
        --infra-only) INFRA_ONLY=1 ;;
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

# ─────────────────────────────────────────────────────────────────────────────
# setup_pxmx_host — host/OS-level infrastructure the pxmx spoke needs to act as
# an AGENT-HOST (accept the local Proxmox NODE agent dialing its listener). This
# is the ONLY part the generic agent's proxmox-role install needs from this
# installer: the agent owns the in-process spoke code, venv, updates, .env and
# the lm-pxmx unit, so this function deliberately touches NONE of those. It is
# idempotent + non-interactive so `install_pxmx.sh --infra-only` can call it
# repeatedly. Called by BOTH the full install (below) and the --infra-only path.
#
# Loopback vs standalone agent-listener mode is NOT decided here. It is selected
# at spoke startup by two env vars the pxmx spoke reads (src/control_plane.py:
# AGENT_LOOPBACK_ENV / AGENT_PORT_ENV):
#     LM_PXMX_AGENT_LOOPBACK=1  → listener binds 127.0.0.1:8443 plaintext and the
#                                 co-located hub /ws/agent route byte-proxies to
#                                 it; NO cert needed (TLS terminates at hub :443).
#     LM_PXMX_AGENT_PORT=8443   → the loopback listener port.
# Standalone (LM_PXMX_AGENT_LOOPBACK unset/0) serves wss on :443 and needs the
# self-signed cert the FULL installer generates — that cert step is standalone-
# only and is intentionally excluded from --infra-only. Under the agent-hosted /
# hub-colocated topology loopback is the relevant mode, so the agent (the lm-side
# wiring) must export LM_PXMX_AGENT_LOOPBACK=1 + LM_PXMX_AGENT_PORT=8443 into the
# in-process spoke's environment; this function only preps the host for it.
setup_pxmx_host() {
    # Shared host dirs: spoke agent-listener/runtime state (/var/lib/pxmx) and
    # the log dir the lm-pxmx unit / agent-hosted role append to (/var/log/lm).
    mkdir -p /var/log/lm /var/lib/pxmx

    # Circular logging: cap /var/log/lm/*.log so it can't fill the disk
    # (copytruncate keeps the inode → the running O_APPEND FileHandler + systemd
    # stderr keep appending). Belt-and-suspenders alongside logging_setup's
    # RotatingFileHandler (LM_LOG_MAX_BYTES).
    cat > /etc/logrotate.d/lm <<'LOGROTATE'
/var/log/lm/*.log /var/log/client-sim-*.log {
    su root root
    size 50M
    rotate 5
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
LOGROTATE

    # Agent secret shared with the local Proxmox NODE agent that dials this
    # spoke's agent listener. The spoke reads it from AGENT_CONFIG_PATH
    # (/etc/lm-agent/config.json). Preserve an existing secret so a re-run /
    # re-install doesn't break an already-registered node agent.
    local agent_config="/etc/lm-agent/config.json"
    local existing_secret=""
    if [ -f "$agent_config" ]; then
        existing_secret=$(python3 -c "import json; print(json.load(open('$agent_config')).get('agent_secret',''))" 2>/dev/null || true)
    fi
    if [ -z "$existing_secret" ]; then
        if command -v openssl >/dev/null 2>&1; then
            AGENT_SECRET=$(openssl rand -base64 32 | tr -d '/+=\n')
        else
            AGENT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        fi
        echo "🔑 Generated new agent_secret."
    else
        AGENT_SECRET="$existing_secret"
        echo "🔑 Preserved existing agent_secret."
    fi
    mkdir -p /etc/lm-agent
    python3 -c "
import json
path = '$agent_config'
try:
    with open(path) as f:
        data = json.load(f)
except Exception:
    data = {}
data['agent_secret'] = '$AGENT_SECRET'
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
"
    chmod 600 "$agent_config"
    chown svc_lm:svc_lm "$agent_config" 2>/dev/null || true
    echo "✅ Agent secret written to $agent_config"
}

# --infra-only: host prep only, then stop. Runs BEFORE apt/clone/venv/.env/unit
# because the agent (which invokes this) owns all of that for the in-process
# spoke. Everything above (arg parse, root check) has already run.
if [ "$INFRA_ONLY" = "1" ]; then
    echo "🧱 pxmx --infra-only: host-level agent-host prep only."
    echo "   (agent owns the in-process spoke code, venv, .env, self-updates and unit.)"
    setup_pxmx_host
    echo "ℹ️  Agent-listener mode is selected by env the pxmx spoke reads at startup:"
    echo "    loopback (hub-colocated): export LM_PXMX_AGENT_LOOPBACK=1 LM_PXMX_AGENT_PORT=8443"
    echo "    standalone: leave LM_PXMX_AGENT_LOOPBACK unset (serves wss :443; needs the"
    echo "    self-signed cert the FULL installer generates — not created in --infra-only)."
    echo "✅ pxmx host infra ready."
    exit 0
fi

apt-get update
apt-get install -y python3-pip python3-venv git curl sudo

INSTALL_DIR="/opt/lm"
OLD_INSTALL_DIR="/opt/lm-manager"

# Cleanup legacy installation
if [ -d "$OLD_INSTALL_DIR" ]; then
    echo "🗑️  Removing legacy installation at $OLD_INSTALL_DIR..."
    rm -rf "$OLD_INSTALL_DIR"
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ── Retire any legacy lm-generic-agent on this box ───────────────────────────
# Vendored from lm/agent/install_agent.sh:retire_legacy_agent — keep in sync.
# The legacy leaf (lm-generic-agent, /opt/lm/generic-agent/src/agent.py) is
# protocol-incompatible with the session-key-adopting hub: it has no
# SPOKE_UPDATE_SESSION_KEY / LOAD_ROLE handler, connects + passes mTLS but never
# adopts a session key, and the hub refuses to dispatch to it (every role on
# the box times out while the WS stays "online"). Purge it before the clone so
# even an aborted install can't leave the zombie connecting under this box's
# id. Idempotent + non-fatal if absent; never touches this installer's own unit
# ($SERVICE_NAME) — it's (re)written below.
SERVICE_NAME="lm-pxmx"
retire_legacy_agent() {
    # Match the legacy leaf by BOTH its historical unit name AND — crucially —
    # by any unit whose definition ExecStarts the legacy path
    # (/opt/lm/generic-agent/src/agent.py). Older template-menu builders named
    # the unit variously (not always lm-generic-agent), so a name-only purge
    # silently misses it and the zombie keeps connecting. Never touch the
    # role-capable unit ($SERVICE_NAME) — the install (re)writes it below.
    local names="lm-generic-agent"
    local f
    # Scan ALL standard systemd unit dirs, not just /etc — older builders dropped
    # the unit under /lib or /usr/lib, so an /etc-only grep misses it entirely.
    for f in /etc/systemd/system/*.service /etc/systemd/system/*/*.service \
             /run/systemd/system/*.service \
             /lib/systemd/system/*.service /usr/lib/systemd/system/*.service; do
        [ -e "$f" ] || continue
        if grep -qE "/opt/lm/generic-agent" "$f" 2>/dev/null; then
            names="$names $(basename "$f" .service)"
        fi
    done
    # Also ask systemd directly which unit (if any) currently has a process whose
    # ExecStart is the legacy path — catches a unit in a non-standard location.
    local u
    for u in $(systemctl list-units --type=service --state=running,failed --no-legend --plain 2>/dev/null | awk '{print $1}'); do
        if systemctl show "$u" -p ExecStart 2>/dev/null | grep -q "/opt/lm/generic-agent"; then
            names="$names ${u%.service}"
        fi
    done
    local svc purged=0
    for svc in $(printf '%s\n' $names | sort -u); do
        [ -n "$svc" ] || continue
        [ "$svc" = "$SERVICE_NAME" ] && continue   # protect the new role-capable unit
        if [ -e "/etc/systemd/system/${svc}.service" ] \
           || systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -qE "^${svc}\.service"; then
            systemctl stop    "$svc" 2>/dev/null || true
            systemctl disable "$svc" 2>/dev/null || true
            rm -f "/etc/systemd/system/${svc}.service"
            systemctl mask    "$svc" 2>/dev/null || true   # after rm → mask sticks (blocks manual restart)
            echo "🧹  Purged legacy leaf unit ${svc}.service."
            purged=1
        fi
    done
    # Also stop any live process still exec'ing the legacy path (belt-and-
    # suspenders if it was launched outside systemd), then remove the dir.
    if [ -d /opt/lm/generic-agent ]; then
        pkill -f "/opt/lm/generic-agent/src/agent.py" 2>/dev/null || true
        rm -rf /opt/lm/generic-agent
        echo "🧹  Removed legacy leaf dir /opt/lm/generic-agent."
        purged=1
    fi
    if [ "$purged" = 1 ]; then
        systemctl daemon-reload 2>/dev/null || true
        echo "    The role-capable ${SERVICE_NAME} now owns this box's spoke connection."
    fi
}
retire_legacy_agent

# ── Ensure the shared LM core is present ─────────────────────────────────────
# The pxmx spoke imports dep_guard / BaseControlPlane from $INSTALL_DIR/core/src
# (see control_plane.py's import block + the unit's PYTHONPATH). install_all.sh
# and the hub-orchestrated install normally lay core down FIRST; a STANDALONE
# spoke install on a bare or freshly-wiped box (e.g. after uninstall_lm.sh) has
# no core, so control_plane.py crash-loops on
#   ModuleNotFoundError: No module named 'dep_guard'
# Provision it here as a real GIT CHECKOUT of the lm repo at $INSTALL_DIR
# (mirrors install_all.sh) so SPOKE_UPDATE's shared-core git-pull keeps working.
# `git reset --hard` only touches TRACKED paths, so the untracked module dirs
# (pxmx/, cs/, venv/, .env, certs/, data/) are preserved.
ensure_core() {
    if [ -f "$INSTALL_DIR/core/src/dep_guard.py" ]; then
        echo "✅ Shared LM core already present at $INSTALL_DIR/core."
        return 0
    fi
    echo "🌐 Shared LM core missing — provisioning $INSTALL_DIR/core from lm.git…"
    rm -rf "$INSTALL_DIR/lm_tmp"
    if ! git clone --depth 1 "https://github.com/lbockenstedt/lm.git" "$INSTALL_DIR/lm_tmp"; then
        echo "❌ Failed to clone the lm core repository."; exit 1
    fi
    git config --global --add safe.directory "$INSTALL_DIR" 2>/dev/null || true
    if [ -d "$INSTALL_DIR/.git" ]; then
        # $INSTALL_DIR is already an lm checkout (core removed but .git kept) —
        # just re-materialize the tracked tree from HEAD.
        rm -rf "$INSTALL_DIR/lm_tmp"
        ( cd "$INSTALL_DIR" && git reset --hard HEAD ) || true
    else
        # Graft the clone's .git into $INSTALL_DIR and let git lay the tracked
        # tree (core/, WebUI/, dns/, dhcp/, VERSION, scripts) in place. Drop any
        # stray untracked copies first so `reset --hard` can't collide.
        rm -rf "$INSTALL_DIR/core" "$INSTALL_DIR/WebUI" "$INSTALL_DIR/dns" "$INSTALL_DIR/dhcp"
        mv "$INSTALL_DIR/lm_tmp/.git" "$INSTALL_DIR/.git"
        rm -rf "$INSTALL_DIR/lm_tmp"
        ( cd "$INSTALL_DIR" && git reset --hard HEAD ) || { echo "❌ core git checkout failed."; exit 1; }
    fi
    if [ ! -f "$INSTALL_DIR/core/src/dep_guard.py" ]; then
        echo "❌ core still missing after provisioning ($INSTALL_DIR/core/src/dep_guard.py)."; exit 1
    fi
    chown -R svc_lm:svc_lm "$INSTALL_DIR/core" "$INSTALL_DIR/.git" 2>/dev/null || true
    echo "✅ Shared LM core laid down at $INSTALL_DIR/core (git checkout)."
}
ensure_core

if [ -d "pxmx/.git" ]; then
    echo "📂 PXMX repository already exists. Updating..."
    cd pxmx && git pull --rebase --autostash && cd ..
else
    echo "🌐 Cloning Proxmox Manager repository..."
    git clone https://github.com/lbockenstedt/pxmx.git
fi

# The git clone/pull above ran as root; the spoke runs as svc_lm and
# self-updates via `git pull`/`git reset --hard` as that user — root-owned
# .git/objects → "insufficient permission for adding an object" → self-update
# fails. Hand the repo to svc_lm + trust the dir (mirrors cs/netbox installers).
chown -R svc_lm:svc_lm "$INSTALL_DIR/pxmx" 2>/dev/null || true
runuser -u svc_lm -- git config --global --add safe.directory "$INSTALL_DIR/pxmx" 2>/dev/null || true

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
# The spoke imports the shared LM core (dep_guard, BaseControlPlane, …) using
# THIS venv via PYTHONPATH=$INSTALL_DIR/core/src, so core's deps must be present
# here too — dep_guard only self-heals the spoke's OWN requirements.txt, not
# core's. Install them into the pxmx venv so the core imports resolve.
if [ -f "$INSTALL_DIR/core/requirements.txt" ]; then
    ./venv/bin/python3 -m pip install -r "$INSTALL_DIR/core/requirements.txt" -q
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

# Preserve the minted INSTALL_UUID across a re-install / update so the hub-side
# fingerprint (install_uuid) stays stable. The cat > below truncates .env, so
# without this the existing UUID line is wiped and the spoke mints a fresh one
# on next start → hub records a `reimaged` (fingerprint-changed) event for a box
# that was only updated. _ensure_install_uuid mints on first start only when
# this line is absent, so omitting it on a fresh install is unchanged.
INSTALL_UUID_LINE=""
if [ -f .env ] && grep -q "^INSTALL_UUID=" .env; then
    EXISTING_UUID=$(grep "^INSTALL_UUID=" .env | cut -d= -f2-)
    if [ -n "$EXISTING_UUID" ]; then
        INSTALL_UUID_LINE="INSTALL_UUID=$EXISTING_UUID"
        echo "Preserving existing install UUID (hub fingerprint)."
    fi
fi
cat <<EOF > .env
HUB_URL=$HUB_URL
${SPOKE_ID_LINE}
SPOKE_SECRET=$SPOKE_SECRET
HUB_SECRET=$HUB_SECRET
${INSTALL_UUID_LINE}
EOF

# ── Agent-listener TLS (standalone mode only) ──────────────────────────────
# Standalone (default): this pxmx spoke is on its OWN box and serves wss on
# LM_PXMX_AGENT_PORT (443 — this box isn't the hub, so 443 is free) so a remote
# Proxmox agent dials wss://<this-spoke>:443/ws/agent directly. The cert is
# self-signed; agents skip verification by default (set LM_HUB_TLS_VERIFY=1 +
# LM_HUB_CA_CERT to verify). Skip gracefully if openssl is absent → listener
# falls back to plaintext :8766.
# Loopback (--loopback, install_all only): no cert — the listener binds
# 127.0.0.1:8443 plaintext and the hub's /ws/agent route byte-proxies to it;
# TLS terminates at the hub's :443, which the hub owns.
PXMX_CERT_DIR="$INSTALL_DIR/pxmx/certs"
PXMX_CERT="$PXMX_CERT_DIR/hub.crt"
PXMX_KEY="$PXMX_CERT_DIR/hub.key"
if [ "$PXMX_LOOPBACK" != "1" ]; then
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
fi
# Persist the agent-listener mode knobs into .env (the unit's EnvironmentFile
# loads these). .env was just (re)written above with the base identity lines, so
# append the mode-specific block fresh.
if [ "$PXMX_LOOPBACK" = "1" ]; then
    {
        echo "# Loopback (all-in-one, --loopback): bind 127.0.0.1:8443 plaintext;"
        echo "# the hub /ws/agent route byte-proxies here. TLS terminates at the hub :443."
        echo "LM_PXMX_AGENT_PORT=8443"
        echo "LM_PXMX_AGENT_LOOPBACK=1"
        echo "LM_HUB_TLS_VERIFY=$HUB_TLS_VERIFY_ENV"
        [ -n "$HUB_TLS_CA_ENV" ] && echo "LM_HUB_CA_CERT=$HUB_TLS_CA_ENV"
    } >> .env
else
    if ! grep -q "^LM_TLS_CERT=" .env 2>/dev/null; then
        {
            echo "LM_TLS_CERT=$PXMX_CERT"
            echo "LM_TLS_KEY=$PXMX_KEY"
            echo "LM_PXMX_AGENT_PORT=443"
            echo "LM_HUB_TLS_VERIFY=$HUB_TLS_VERIFY_ENV"
            [ -n "$HUB_TLS_CA_ENV" ] && echo "LM_HUB_CA_CERT=$HUB_TLS_CA_ENV"
        } >> .env
    fi
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

# CRITICAL: .env is created/rewritten above AS ROOT (the chown -R at clone time
# ran BEFORE .env existed), but the spoke runs as svc_lm and must WRITE .env at
# runtime — _ensure_install_uuid persists the minted INSTALL_UUID there, and the
# hub-secret rotation persists HUB_SECRET there. A root-owned .env →
#   "[Errno 13] Permission denied: /opt/lm/pxmx/.env"
# → the spoke can NEVER persist a stable per-clone identity: it reports an
# empty/stale UUID every boot, so cloned boxes step on each other on the hub.
# Hand .env to svc_lm (and mode 600) so the runtime persistence writes succeed.
chown svc_lm:svc_lm .env 2>/dev/null || true
chmod 600 .env 2>/dev/null || true

# --- Agent Secret + host dirs (shared with local Proxmox agent on this machine) ---
# Host/OS-level agent-host prep (dirs + /etc/lm-agent/config.json agent_secret),
# extracted into setup_pxmx_host so the agent's --infra-only path reuses it.
setup_pxmx_host

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

# Agent-listener port per mode: 443 wss standalone (default), 8443 loopback
# (--loopback / install_all co-located). AmbientCapabilities lets svc_lm bind
# 443 non-root (harmless in loopback, which binds 8443 >1024).
if [ "$PXMX_LOOPBACK" = "1" ]; then
    PXMX_AGENT_PORT_UNIT=8443
else
    PXMX_AGENT_PORT_UNIT=443
fi

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
# Agent listener: standalone serves wss on :443 (remote Proxmox agents dial
# wss://<this-spoke>:443/ws/agent directly — agent → spoke → hub); loopback
# (--loopback, install_all co-located only) binds 127.0.0.1:8443 plaintext and
# the hub /ws/agent route byte-proxies to it (agent → hub → spoke). Hub-cert
# verification OFF by default; --tls-verify sets LM_HUB_TLS_VERIFY=1 +
# LM_HUB_CA_CERT so this spoke verifies the hub.
Environment=LM_PXMX_AGENT_PORT=$PXMX_AGENT_PORT_UNIT LM_HUB_TLS_VERIFY=$HUB_TLS_VERIFY_ENV$_TLS_CA_UNIT
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
# Dual-repo rollback: when the spoke update ALSO pulled the shared /opt/lm core
# checkout (--core-repo-root + core_from_commit/core_to_commit in the pending
# manifest), a boot failure resets BOTH repos — the spoke first, then core.
# The core to_commit is marked bad so the next SPOKE_UPDATE skips a crash-
# looping core. v1 is NON-ATOMIC across the two repos: a watchdog crash between
# the two `git reset --hard`s leaves the spoke rolled back but core forward —
# recoverable via the on-disk manifest + the `writefailed` marker. Atomic
# two-repo rollback is deferred.
#
# State-file ops delegate to the Python CLI update_recovery.py (SINGLE SOURCE OF
# TRUTH for the on-disk recovery state machine). Only poll/systemd/git logic
# lives here. This file is the canonical source; install_cs.sh / install_pxmx.sh
# / install_agent.sh embed it verbatim via here-doc — keep them in sync.
set -uo pipefail

UNIT="" STATE_DIR="" REPO_ROOT="" INSTALL_DIR="" DEADLINE=90 CORE_REPO_ROOT=""
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
        --core-repo-root) CORE_REPO_ROOT="$2"; shift 2;;
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
core_from="$(printf '%s' "$pending" | jq -r '.core_from_commit // empty' 2>/dev/null)"
core_to="$(printf '%s' "$pending" | jq -r '.core_to_commit // empty' 2>/dev/null)"

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

# Dual-repo rollback: reset the shared /opt/lm core checkout AFTER the spoke
# repo so a crash-looping core (e.g. a bad BaseControlPlane change) is rolled
# back too. The core to_commit is marked bad so the next SPOKE_UPDATE skips it
# (the spoke's _is_known_bad_commit guard) instead of re-pulling it. Skipped
# entirely when no --core-repo-root / core fields were recorded — single-repo
# behavior is unchanged.
if [ -n "$CORE_REPO_ROOT" ] && [ -n "$core_from" ]; then
    echo "lm-component-update-restart: rolling back shared core at $CORE_REPO_ROOT to $core_from." >&2
    git -C "$CORE_REPO_ROOT" reset --hard "$core_from" >/dev/null 2>&1 || true
    git -C "$CORE_REPO_ROOT" clean -fd >/dev/null 2>&1 || true
    if [ -n "$core_to" ]; then
        python3 "$RECOVERY_PY" markbadcommit "$core_to" --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
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
# /etc/sudoers.d is created by the sudo package's postinst (installed above);
# mkdir here too as a defensive belt-and-suspenders in case a minimal image
# ever lacks it.
mkdir -p /etc/sudoers.d
cat > /etc/sudoers.d/lm-component-update <<SUDOERS
svc_lm ALL=(ALL) NOPASSWD: /usr/local/bin/lm-component-update-restart
SUDOERS
chmod 0440 /etc/sudoers.d/lm-component-update
visudo -cf /etc/sudoers.d/lm-component-update >/dev/null 2>&1 || true

# Apply new code now and prevent split-brain: stop the current instance, reap
# any orphaned/stale pxmx control_plane process left by a previous install
# (different unit or invocation), then start fresh. A stale instance holding
# :443 while a new one reaches the hub with no agent is exactly the split-brain
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
# Standalone (default): the agent dials THIS spoke directly (agent → spoke → hub).
#   A standalone spoke does NOT broadcast _lm-hub mDNS (only the hub does), so the
#   agent cannot auto-discover it — --spoke-url (pinned to this box) is REQUIRED.
# Loopback (--loopback, install_all co-located): the agent auto-discovers the HUB
#   via _lm-hub mDNS / lm-hub DNS and dials wss://<hub>:443/ws/agent; the hub's
#   /ws/agent route byte-proxies to this spoke's loopback :8443.
LM_HOST=$(echo "$HUB_URL" | sed 's|^wss://||;s|^ws://||' | cut -d: -f1)
SPOKE_HOST="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^127\.' | head -1)"
[ -z "$SPOKE_HOST" ] && SPOKE_HOST="$(hostname -s)"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Run this on each Proxmox node to install the pxmx agent:"
echo ""
echo "  curl -sSL https://raw.githubusercontent.com/lbockenstedt/pxmx/main/agent/install_agent.sh \\"
if [ "$PXMX_LOOPBACK" = "1" ]; then
    echo "    | sudo bash"
    echo "  (loopback/all-in-one: the agent auto-discovers the HUB via DNS lm-hub.* / mDNS"
    echo "   _lm-hub._tcp and dials wss://<hub>:443/ws/agent; the hub /ws/agent route"
    echo "   byte-proxies to this spoke's loopback :8443 — agent → hub → spoke.)"
    if [ -n "$LM_HOST" ]; then
        echo "  To pin instead:  --spoke-ip ${LM_HOST}   (just the IP; scheme/port/path auto-determined)"
    fi
else
    echo "    | sudo bash -s -- --spoke-ip ${SPOKE_HOST}"
    echo "  (standalone spoke: the agent dials THIS spoke directly — agent → spoke → hub."
    echo "   Supply just this spoke's IP with --spoke-ip; the agent auto-determines the"
    echo "   scheme/port/path by probing. A standalone spoke does not broadcast _lm-hub"
    echo "   mDNS, so a pinned --spoke-ip is REQUIRED.)"
fi
echo "  (omitting --id derives <hostname>-agent; clone+rename auto-correlates via install UUID)"
echo ""
echo "  The agent will appear as 'Pending' in the LM WebUI (Setup → Spokes & Agents → Agents tile)."
echo "  Approve it there and the authentication secret will be provisioned automatically."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
