#!/bin/bash
# uninstall_pxmx.sh — Remove the Proxmox (pxmx) spoke from this host.
#
# Reverses install_pxmx.sh: stops+removes the lm-pxmx systemd unit and deletes
# the /opt/lm/pxmx install tree (repo + venv + .env + certs). Optionally purges
# pxmx logs/state and deregisters the spoke from the hub.
#
# SAFETY: this NEVER removes assets shared with other LM components:
#   - the svc_lm system user            (cs, netbox, … share it)
#   - /opt/lm/core                      (base_spoke; every spoke depends on it)
#   - /var/log/lm                       (shared log dir — only pxmx logs go)
#   - /usr/local/bin/lm-component-update-restart AND
#     /etc/sudoers.d/lm-component-update  (the cs spoke and the pxmx AGENT both
#                                          schedule this helper — removing it
#                                          breaks their self-update rollback)
#   - /etc/logrotate.d/lm               (shared rotation for /var/log/lm/*.log)
#
# The Proxmox host AGENT is a separate install with its own uninstaller:
#   bash /opt/lm/pxmx/agent/uninstall_agent.sh
# Removing this spoke while an agent still points at it leaves that agent
# offline and retrying — uninstall the agent too, or repoint it.
#
# Usage:
#   sudo bash uninstall_pxmx.sh --yes
#   sudo bash uninstall_pxmx.sh --yes --purge-logs --purge-state \
#        --hub-api https://lm-hub.example.com --spoke-id pxmx-node1
set -euo pipefail

INSTALL_DIR="/opt/lm"
PXMX_DIR="$INSTALL_DIR/pxmx"
SHARED_CORE="$INSTALL_DIR/core"
SERVICE_NAME="lm-pxmx"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="$PXMX_DIR/.env"
LOG_GLOB="/var/log/lm/lm-pxmx.log"
STATE_DIR="/var/lib/pxmx"

YES=0
PURGE_LOGS=0
PURGE_STATE=0
HUB_API=""
SPOKE_ID=""
SPOKE_ID_SET=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)      YES=1; shift ;;
        --purge-logs)  PURGE_LOGS=1; shift ;;
        --purge-state) PURGE_STATE=1; shift ;;
        --hub-api)     HUB_API="$2"; shift 2 ;;
        --spoke-id)    SPOKE_ID="$2"; SPOKE_ID_SET=1; shift 2 ;;
        -h|--help)     sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
done

if [[ "$(id -u)" -ne 0 ]]; then
    echo "ERROR: run as root (sudo bash $0 …)" >&2
    exit 1
fi

# Deregistering needs the id the spoke actually registered under. The installer
# only bakes SPOKE_ID into .env when it was explicitly pinned; otherwise the
# spoke derives it from the hostname at startup. Read .env first, fall back to
# the same hostname derivation so we deregister the RIGHT spoke.
if [[ $SPOKE_ID_SET -eq 0 ]]; then
    if [[ -f "$ENV_FILE" ]] && grep -q '^SPOKE_ID=' "$ENV_FILE"; then
        SPOKE_ID="$(grep '^SPOKE_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
    else
        # Matches control_plane.py:220 — f"{socket.gethostname()}-spoke".
        SPOKE_ID="$(hostname)-spoke"
    fi
fi

echo "=== pxmx spoke uninstall ==="
echo "  Service     : $SERVICE_NAME"
echo "  Install tree: $PXMX_DIR  (repo + venv + .env + certs)"
echo "  Purge logs  : $([[ $PURGE_LOGS -eq 1 ]] && echo "yes ($LOG_GLOB*)" || echo no)"
echo "  Purge state : $([[ $PURGE_STATE -eq 1 ]] && echo "yes ($STATE_DIR, /var/lib/lm/$SPOKE_ID)" || echo no)"
echo "  Hub dereg   : $([[ -n $HUB_API ]] && echo "$HUB_API (spoke=$SPOKE_ID)" || echo no)"
echo "  Preserved   : svc_lm, $SHARED_CORE, /var/log/lm, lm-component-update-restart + its sudoers, logrotate"
echo

if [[ $YES -ne 1 ]]; then
    read -r -p "Proceed? [y/N] " ans
    [[ "$ans" == "y" || "$ans" == "Y" ]] || { echo "Aborted."; exit 0; }
fi

# ── [1] Stop + remove the unit ───────────────────────────────────────────────
if systemctl list-unit-files 2>/dev/null | grep -q "^${SERVICE_NAME}.service"; then
    echo "  Stopping $SERVICE_NAME ..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
fi
rm -f "$UNIT_FILE"
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true
echo "  Unit removed."

# Reap anything still holding the code (a spoke mid-self-update restarts
# outside the unit's cgroup, so a stale process can survive the stop above).
pkill -f "$PXMX_DIR/venv/bin/python.*proxmox_spoke" 2>/dev/null || true

# ── [2] Remove the install tree ──────────────────────────────────────────────
if [[ -d "$PXMX_DIR" ]]; then
    rm -rf "$PXMX_DIR"
    echo "  Removed $PXMX_DIR"
else
    echo "  $PXMX_DIR not present — skipping."
fi

# ── [3] Optional logs ────────────────────────────────────────────────────────
if [[ $PURGE_LOGS -eq 1 ]]; then
    rm -f "${LOG_GLOB}"* 2>/dev/null || true
    echo "  Purged ${LOG_GLOB}*  (/var/log/lm itself kept — it is shared)"
fi

# ── [4] Optional state ───────────────────────────────────────────────────────
if [[ $PURGE_STATE -eq 1 ]]; then
    rm -rf "$STATE_DIR" 2>/dev/null || true
    rm -rf "/var/lib/lm/${SPOKE_ID}" 2>/dev/null || true
    echo "  Purged $STATE_DIR and /var/lib/lm/$SPOKE_ID"
    echo "  NOTE: this drops the install fingerprint — a reinstall registers as a NEW spoke needing approval."
fi

# ── [5] Optional hub deregister ──────────────────────────────────────────────
if [[ -n "$HUB_API" ]]; then
    echo "  Deregistering spoke '$SPOKE_ID' from hub at $HUB_API ..."
    if curl -sf -X DELETE "${HUB_API}/setup/spokes/${SPOKE_ID}" >/dev/null 2>&1; then
        echo "  Deregistered: $SPOKE_ID"
    else
        echo "  WARNING: could not deregister $SPOKE_ID (hub unreachable / not found / auth) — remove it in Setup → Spokes"
    fi
fi

echo
echo "=== Uninstall complete ==="
echo "  The pxmx spoke is no longer installed on this host."
echo "  Preserved: svc_lm user, $SHARED_CORE, /var/log/lm, the shared update-restart helper."
if [[ -d "$INSTALL_DIR/pxmx/agent" || -f /etc/systemd/system/lm-pxmx-agent.service ]]; then
    echo "  A Proxmox AGENT is still installed — remove it with:"
    echo "      bash $INSTALL_DIR/pxmx/agent/uninstall_agent.sh"
fi
