#!/bin/bash
set -e

# Default Configuration
# AGENT_ID is OPTIONAL. When --id is not supplied the agent derives its id from
# the current OS hostname at startup (see agent.py __main__), so a cloned+renamed
# Proxmox node reconnects under a new id (correlated to the old one via the
# install UUID by the hub) instead of being frozen to the hostname at install.
# A pinned --id is honored as-is. We only bake AGENT_ID into .env + the unit
# when it was explicitly pinned; otherwise Python owns the id. INSTALL_UUID is
# never written here — the agent mints it at first start.
# Where the agent reports in. The normal way is --spoke-ip: supply ONLY the
# spoke's IP (or hostname) and the agent works out the rest (scheme + port +
# /ws/agent) by probing that host's known listener endpoints. --spoke-url is the
# legacy/power-user form (a fully-pinned ws(s)://host:port/ws/agent) and wins if
# both are given. When NEITHER is supplied the installer auto-discovers the hub
# box via DNS (lm-hub.<dns-suffix>) then mDNS, and if that also finds nothing the
# agent keeps re-discovering at startup.
SPOKE_IP="${SPOKE_IP:-}"
SPOKE_URL="${SPOKE_URL:-}"
# Track whether a target was explicitly given (arg or env). When NOT pinned the
# installer falls back to hub auto-discovery after the venv is ready.
SPOKE_URL_PINNED=0
[ -n "$SPOKE_URL" ] && SPOKE_URL_PINNED=1
AGENT_ID=""
AGENT_ID_PINNED=0
AGENT_SECRET=""

# Parse arguments
while [[ "$#" -gt 0 ]]; do
    case $1 in
        # Preferred: just an IP. The agent auto-determines scheme/port/path.
        --spoke-ip)  SPOKE_IP="$2"; shift ;;
        # Legacy/advanced: a fully-formed ws(s)://host:port/ws/agent URL.
        --spoke-url) SPOKE_URL="$2"; SPOKE_URL_PINNED=1; shift ;;
        --id) AGENT_ID="$2"; AGENT_ID_PINNED=1; shift ;;
        --secret) AGENT_SECRET="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

# A bare IP accidentally passed to --spoke-url (no scheme) is really a --spoke-ip.
# Reclassify it so the operator gets the auto-determine behavior either way.
if [ "$SPOKE_URL_PINNED" = "1" ] && [ -n "$SPOKE_URL" ] && \
   [ -z "$SPOKE_IP" ] && [[ "$SPOKE_URL" != *"://"* ]]; then
    echo "ℹ️  --spoke-url '$SPOKE_URL' has no scheme; treating it as --spoke-ip (auto-determining the WS URL)."
    SPOKE_IP="$SPOKE_URL"
    SPOKE_URL=""
    SPOKE_URL_PINNED=0
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
# Log dir shared with the hub + spokes; the agent (User=root) writes its
# FileHandler here and the systemd unit appends stderr to the same file.
mkdir -p /var/log/lm

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

# ── Resolve where the agent reports in ──────────────────────────────────────
# Precedence: --spoke-ip (probe the given host) > --spoke-url (verbatim pin) >
# hub auto-discovery (DNS lm-hub.* / mDNS). All probing uses the just-installed
# venv + the vendored src/discovery.py. cwd is $INSTALL_DIR so `src.discovery`
# imports (src/ is a package dir).
if [ -n "$SPOKE_IP" ]; then
    # Operator supplied only an IP. Probe its known /ws/agent endpoints so we can
    # confirm reachability now and show the resolved URL — but the agent is baked
    # with --spoke-ip (not the resolved URL) so it re-probes at runtime and keeps
    # working if the spoke is still booting or later changes scheme/port.
    echo "🔎 Probing $SPOKE_IP for an LM agent listener (auto-determining scheme/port/path)…"
    RESOLVED=$(cd "$INSTALL_DIR" && "./venv/bin/python3" -m src.discovery --resolve-agent "$SPOKE_IP" --timeout 6 2>/dev/null || echo NONE)
    if [ -n "$RESOLVED" ] && [ "$RESOLVED" != "NONE" ]; then
        echo "✅ Found agent listener: $RESOLVED"
    else
        echo "⚠️  No agent listener answered at $SPOKE_IP yet — the agent will keep"
        echo "    re-probing at startup. Check the spoke is up and reachable, then it"
        echo "    connects on its own (no reinstall needed)."
    fi
elif [ "$SPOKE_URL_PINNED" != "1" ]; then
    echo "🔎 No --spoke-ip/--spoke-url given; auto-discovering the LM hub box (DNS lm-hub.* / mDNS, agent listener)…"
    DISCOVERED=$(cd "$INSTALL_DIR" && "./venv/bin/python3" -m src.discovery --timeout 5 --agent-listener 2>/dev/null || echo NONE)
    if [ -n "$DISCOVERED" ] && [ "$DISCOVERED" != "NONE" ]; then
        SPOKE_URL="$DISCOVERED"
        echo "✅ Discovered hub box: $SPOKE_URL"
    else
        echo "⚠️  Hub box not found via DNS/mDNS. Leaving the target empty — the agent will"
        echo "    retry auto-discovery at startup. To pin it now, re-run with"
        echo "    --spoke-ip <SPOKE_IP>  (just the IP; scheme/port/path are auto-determined)."
        echo "    (Or create an 'lm-hub' DNS record / enable mDNS on the hub.)"
        SPOKE_URL=""
    fi
fi

# Bake AGENT_ID into .env + the unit ONLY when it was explicitly pinned. In the
# derived case Python computes `<hostname>-agent` at startup, so a clone that was
# renamed reconnects under a new id (correlated to the old one via the install
# UUID). INSTALL_UUID is NOT written here — the agent mints it at first start.
AGENT_ID_LINE=""
ID_ARG=""
if [ "$AGENT_ID_PINNED" = "1" ]; then
    AGENT_ID_LINE="AGENT_ID=$AGENT_ID"
    ID_ARG="--id $AGENT_ID"
fi

# ── Write .env (preserving secret) ────────────────────────────────────────────
# SPOKE_IP/SPOKE_URL are recorded here for reference; the authoritative runtime
# value is the flag baked into the unit's ExecStart below (systemd does not
# source this file — only the agent reads it, and only for the secret).
cat <<EOF > "$INSTALL_DIR/.env"
SPOKE_IP=$SPOKE_IP
SPOKE_URL=$SPOKE_URL
${AGENT_ID_LINE}
AGENT_SECRET=$FINAL_SECRET
EOF

# ── Systemd service ───────────────────────────────────────────────────────────
# Build the spoke-target arg conditionally, preferring --spoke-ip (the agent
# auto-determines scheme/port/path and re-probes on failure) over a concrete
# --spoke-url. When BOTH are empty (nothing pinned and hub discovery found
# nothing) we OMIT the flag entirely so argparse falls back to its default and
# the agent's run() sentinel re-discovers at startup — passing an empty-valued
# flag would instead make argparse error ("expected one argument") and
# crash-loop the unit.
SPOKE_URL_ARG=""
if [ -n "$SPOKE_IP" ]; then
    SPOKE_URL_ARG="--spoke-ip $SPOKE_IP"
elif [ -n "$SPOKE_URL" ]; then
    SPOKE_URL_ARG="--spoke-url $SPOKE_URL"
fi
cat <<EOF > /etc/systemd/system/lm-pxmx-agent.service
[Unit]
Description=Lab Manager - Local Proxmox Agent
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 -m src.agent $SPOKE_URL_ARG $ID_ARG
StandardOutput=append:/var/log/lm/pxmx-agent.log
StandardError=append:/var/log/lm/pxmx-agent.log
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

# ── Failed-update rollback watchdog + state dir ───────────────────────────────
# /var/lib/pxmx/update-state holds the pre-swap code snapshot, the pending-update
# manifest, the healthy marker, and the bad-version registry. The agent (root)
# writes the snapshot/pending/marker; the watchdog below (root, via systemd-run)
# reads them and rolls back a self-update that crashes at boot. Created on demand
# at runtime too, but mkdir here so it exists before the first update.
mkdir -p /var/lib/pxmx/update-state
chmod 0755 /var/lib/pxmx/update-state
# The external health-gate watchdog. Scheduled by the agent (root — no sudo)
# right before it os._exit(0)s to load new code; runs outside the agent's cgroup
# via systemd-run so it survives the restart. Same script as the spokes use
# (lm/scripts/lm-component-update-restart is the canonical source — keep in sync).
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
LOG_FILE="/var/log/lm/pxmx-agent.log"
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
if [ -n "$SPOKE_IP" ]; then
    echo "🌐 Target Spoke: $SPOKE_IP  (scheme/port/path auto-determined at startup)"
elif [ -n "$SPOKE_URL" ]; then
    echo "🌐 Target Spoke: $SPOKE_URL"
else
    echo "🌐 Target Spoke: (auto-discover at startup — no lm-hub DNS/mDNS found yet)"
fi
if [ "$AGENT_ID_PINNED" = "1" ]; then
    echo "🆔 Agent ID: $AGENT_ID  (pinned)"
else
    echo "🆔 Agent ID: $(hostname -s)-agent  (derived from hostname at startup)"
fi
echo "📦 Version: $(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo unknown)"
echo "🛡️  Rollback: /usr/local/bin/lm-component-update-restart — a failed self-update"
echo "    (crash at boot) is rolled back to the prior file-tree snapshot automatically."
echo "    NOTE: this watchdog lands only on a full installer re-run; a box that only"
echo "    git-pulled the new agent code must be re-installed once to enable it."
