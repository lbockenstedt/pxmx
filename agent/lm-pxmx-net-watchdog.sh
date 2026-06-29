#!/bin/bash
# lm-pxmx-net-watchdog.sh — gateway-loss reboot watchdog for the unified pxmx agent.
#
# Phase G split of the legacy cs/proxmox/watchdog.sh (498 lines). The old
# watchdog bundled service-supervision, OS-crash detection, and gateway-loss
# reboot. In the unified world:
#   - service supervision  -> systemd Restart=always on lm-pxmx-agent.service
#   - OS-crash / hang       -> systemd WatchdogSec= + the agent's sd_notify
#   - gateway-loss reboot   -> THIS script (the only piece that must survive an
#                             agent crash, so it stays a separate systemd timer)
#
# What it does (and only what it does): detect the default route's gateway,
# ping it, and if it has been unreachable for NET_DOWN_REBOOT_SECS, reboot the
# host. It deliberately does NOT report events to the cs spoke (the agent emits
# CS_HW_RESET_EVENT/CS_WATCHDOG_EVENT up through the hub when it is up; this
# timer runs precisely when the agent may be down, so it cannot depend on it).
#
# Installed/enabled by install_agent.sh as lm-pxmx-net-watchdog.{service,timer}
# (rename of the retired proxmox-watchdog.*). State lives under /var/lib/pxmx.

set -u
STATE_DIR="/var/lib/pxmx"
NET_FAIL_FILE="${STATE_DIR}/net-fail-since"
LOG_FILE="/var/log/lm-pxmx-net-watchdog.log"
NET_DOWN_REBOOT_SECS="${NET_DOWN_REBOOT_SECS:-3600}"   # 60 min default

mkdir -p "$STATE_DIR" 2>/dev/null || true
log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"; }

check_network_gateway() {
    local gw now fail_since elapsed
    gw=$(ip route show default 2>/dev/null | awk '/default via/{print $3; exit}')
    if [ -z "$gw" ]; then
        # No default route — nothing to gate on. Clear any stale fail marker.
        rm -f "$NET_FAIL_FILE" 2>/dev/null || true
        return 0
    fi
    if ping -c 2 -W 3 -q "$gw" >/dev/null 2>&1; then
        if [ -f "$NET_FAIL_FILE" ]; then
            fail_since=$(cat "$NET_FAIL_FILE" 2>/dev/null || echo 0)
            now=$(date +%s)
            log "NET_RECOVERY gateway=$gw outage_secs=$((now - fail_since))"
            rm -f "$NET_FAIL_FILE" 2>/dev/null || true
        fi
        return 0
    fi
    # Gateway unreachable.
    now=$(date +%s)
    if [ ! -f "$NET_FAIL_FILE" ]; then
        echo "$now" > "$NET_FAIL_FILE" 2>/dev/null || true
        log "NET_DOWN gateway=$gw — marking fail-since"
        return 0
    fi
    fail_since=$(cat "$NET_FAIL_FILE" 2>/dev/null || echo "$now")
    elapsed=$(( now - fail_since ))
    if [ "$elapsed" -ge "$NET_DOWN_REBOOT_SECS" ]; then
        log "NET_REBOOT gateway=$gw down_secs=$elapsed >= $NET_DOWN_REBOOT_SECS — rebooting host"
        sync
        /sbin/reboot || reboot || true
    else
        log "NET_DOWN gateway=$gw down_secs=$elapsed (threshold $NET_DOWN_REBOOT_SECS)"
    fi
    return 0
}

check_network_gateway || true