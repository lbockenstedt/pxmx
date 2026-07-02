#!/bin/bash
# retire_bash_agent.sh — Phase G teardown of the legacy cs bash Proxmox agent.
#
# Run on the Proxmox host AFTER (or alongside) installing the unified pxmx
# agent via install_agent.sh. It stops + removes the bash agent's systemd
# units, binaries, env file, installer dir, watchdog units/state, kernel
# watchdog configs, and /etc/pve/scripts helpers — everything the legacy
# cs/proxmox/install-proxmox-agent.sh deployed.
#
# Idempotent: every step is guarded by is-active/is-enabled/-f/-d checks, so
# re-running or running on a host that never had the bash agent is safe.
# It does NOT touch VMs (qm list is unchanged) and it does NOT remove
# /var/lib/client-sim/ — install_agent.sh folds that into /var/lib/pxmx/ via
# the one-time .migrated migration. Pass --purge-logs to also drop the old
# log files, --yes to skip the confirmation prompt.
#
# Delivered through the pxmx repo (the unified agent self-updates from
# github.com/lbockenstedt/pxmx), so this script arrives on the host via the
# agent's _update_check_loop — no separate cs checkout needed on the host.

set -u

PURGE_LOGS=0
ASSUME_YES=0
for a in "$@"; do
    case "$a" in
        --purge-logs) PURGE_LOGS=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help)
            echo "Usage: $0 [--yes] [--purge-logs]"
            echo "  Retires the legacy cs bash Proxmox agent (Phase G)."
            exit 0 ;;
        *) echo "Unknown arg: $a"; exit 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

SYSTEMD_DIR="/etc/systemd/system"
BASH_SERVICE="client-sim-proxmox-agent.service"
BASH_ALIAS="proxmox-agent.service"
OLD_WD_SERVICE="proxmox-watchdog.service"
OLD_WD_TIMER="proxmox-watchdog.timer"
AGENT_BIN="/usr/local/bin/client-sim-proxmox-agent"
WATCHDOG_BIN="/usr/local/bin/proxmox-watchdog"
ENV_FILE="/etc/client-sim-proxmox-agent.env"
INSTALLER_DIR="/opt/proxmox-agent-installer"
OLD_WD_STATE="/var/lib/proxmox-watchdog"
SYSCTL_CONF="/etc/sysctl.d/99-client-sim-watchdog.conf"
MODULES_CONF="/etc/modules-load.d/client-sim-watchdog.conf"
PVE_SCRIPTS_DIR="/etc/pve/scripts"
PVE_SCRIPT_FILES="clone.sh ini-parser.sh check_guest.sh sync-scripts.sh client-setup.conf"
OLD_AGENT_LOG="/var/log/client-sim-proxmox-agent.log"
OLD_WD_LOG="/var/log/proxmox-watchdog.log"

# A unit is "present" if its file exists OR systemd knows it (enabled/active).
unit_present() {
    [ -f "$SYSTEMD_DIR/$1" ] && return 0
    systemctl cat "$1" >/dev/null 2>&1 && return 0
    systemctl is-enabled "$1" >/dev/null 2>&1 && return 0
    return 1
}

echo "🧹 Phase G — retiring the legacy cs bash Proxmox agent."
echo "   This does NOT touch VMs (qm list unchanged). /var/lib/client-sim is"
echo "   left for install_agent.sh to migrate to /var/lib/pxmx."
if [ "$ASSUME_YES" -ne 1 ]; then
    printf "Proceed? [y/N] "
    read -r ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
fi

# [1/6] Stop + disable the bash agent and the old watchdog units.
for u in "$BASH_SERVICE" "$OLD_WD_TIMER" "$OLD_WD_SERVICE"; do
    if unit_present "$u"; then
        echo "⏹  Stopping $u ..."
        systemctl stop "$u" 2>/dev/null || true
        systemctl disable "$u" 2>/dev/null || true
    fi
done

# [2/6] Remove the unit files (incl. the Alias=proxmox-agent.service) + reload.
for u in "$BASH_SERVICE" "$BASH_ALIAS" "$OLD_WD_SERVICE" "$OLD_WD_TIMER"; do
    if [ -f "$SYSTEMD_DIR/$u" ]; then
        echo "🗑  Removing unit $u"
        rm -f "$SYSTEMD_DIR/$u"
    fi
done
systemctl daemon-reload 2>/dev/null || true

# [3/6] Remove the bash agent + watchdog binaries and env file.
for f in "$AGENT_BIN" "$WATCHDOG_BIN" "$ENV_FILE"; do
    if [ -e "$f" ]; then
        echo "🗑  Removing $f"
        rm -f "$f"
    fi
done

# [4/6] Remove the installer dir + old watchdog state dir.
for d in "$INSTALLER_DIR" "$OLD_WD_STATE"; do
    if [ -d "$d" ]; then
        echo "🗑  Removing $d"
        rm -rf "$d"
    fi
done

# [5/6] Remove the legacy kernel-watchdog sysctl + module-load configs. These
# are the OLD client-sim-prefixed files the bash agent deployed. The unified
# agent re-provides the same kernel crash-hardening under lm-pxmx-prefixed
# filenames (install_agent.sh), so this only cleans up the stale bash copies —
# it does NOT unload the unified agent's softdog/sysctl.
if [ -f "$SYSCTL_CONF" ]; then
    echo "🗑  Removing $SYSCTL_CONF"
    rm -f "$SYSCTL_CONF"
    sysctl --system 2>/dev/null || true
fi
if [ -f "$MODULES_CONF" ]; then
    echo "🗑  Removing $MODULES_CONF"
    rm -f "$MODULES_CONF"
fi
if lsmod 2>/dev/null | grep -q "^softdog\b"; then
    # Only unload softdog if the unified agent isn't using it (it re-provides
    # kernel crash-hardening under an lm-pxmx-prefixed modules-load file).
    if [ ! -f "/etc/modules-load.d/lm-pxmx-watchdog.conf" ]; then
        echo "🗑  Unloading softdog module"
        rmmod softdog 2>/dev/null || true
    else
        echo "   softdog still in use by the unified agent — leaving loaded"
    fi
fi

# [6/6] Remove the /etc/pve/scripts helpers (reimplemented in the unified
# agent: usb_provision.py, configparser, pve_cmds.py). chmod isn't supported
# on the pve cluster FS, but rm is.
if [ -d "$PVE_SCRIPTS_DIR" ]; then
    for f in $PVE_SCRIPT_FILES; do
        if [ -f "$PVE_SCRIPTS_DIR/$f" ]; then
            echo "🗑  Removing $PVE_SCRIPTS_DIR/$f"
            rm -f "$PVE_SCRIPTS_DIR/$f"
        fi
    done
fi

if [ "$PURGE_LOGS" -eq 1 ]; then
    for f in "$OLD_AGENT_LOG" "$OLD_WD_LOG"; do
        if [ -e "$f" ]; then
            echo "🗑  Removing $f (--purge-logs)"
            rm -f "$f"
        fi
    done
fi

echo ""
echo "✅ Legacy cs bash agent retired."
echo "   Next: install/reinstall the unified agent so it picks up the hardened"
echo "   unit (WatchdogSec= + NotifyAccess=), the lm-pxmx-net-watchdog timer,"
echo "   and the /var/lib/client-sim → /var/lib/pxmx migration:"
echo "     bash /opt/lm/pxmx/agent/install_agent.sh --spoke-url <ws://hub:8766>"
echo ""
echo "   Verify the one-socket invariant:"
echo "     systemctl is-active lm-pxmx-agent            # active"
echo "     systemctl is-active client-sim-proxmox-agent # inactive (not-found)"
echo "     ss -tnp | grep -E '8766|:8000'               # only 8766 to the pxmx spoke"
echo "     qm list                                      # unchanged"