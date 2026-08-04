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

# uhubctl — per-port USB power switching, used by the missing-dongle diagnostic
# (Setup → Diagnostics) to report whether this host can power-cycle its USB ports
# instead of needing a reboot. Installed SEPARATELY and best-effort on purpose:
# it is a diagnostic aid, not a runtime dependency, so a host whose repos lack
# the package must still get a working agent. Folding it into the line above
# would abort the whole install on a single unavailable package.
#
# Note the agent probes for a PPPS-capable hub at runtime — installing the binary
# does NOT mean port power cycling will work here. Most on-board root hubs do not
# support it; the Diagnostics badge reports the real verdict per host.
if command -v uhubctl >/dev/null 2>&1; then
    echo "   ✔ uhubctl already present ($(uhubctl -v 2>/dev/null | head -1))"
elif apt-get install -y uhubctl >/dev/null 2>&1; then
    echo "   ✔ uhubctl installed (USB port power diagnostics)"
else
    echo "   ⚠ uhubctl unavailable — USB port power-cycle diagnostics will report"
    echo "     'not installed'. Everything else works. Install later with:"
    echo "       apt-get install -y uhubctl"
fi

# ── Host power-management kernel parameters ────────────────────────────────
# Aggressive PCIe/USB power management is what makes dongles vanish from lsusb
# with NO kernel error — the silent multi-day decay that only a reboot or a
# physical replug recovers (the in-guest usb_reset rung cannot help:
# /sys/bus/usb/devices/<bus>/authorized stops existing once the device is gone).
#
# Target line on every pxmx CS agent host:
#   GRUB_CMDLINE_LINUX_DEFAULT="quiet pcie_aspm=off intel_iommu=on pcie_power_pm=off usbcore.autosuspend=-1"
#
# NOTE the sign on autosuspend. It is a DELAY IN SECONDS, not a boolean:
#   =2   kernel default (suspend after 2s idle)
#   =1   suspend after 1s — MORE aggressive than default (found in the field,
#        set in the belief it meant "enabled/on")
#   =-1  the ONLY value that disables autosuspend outright
# An existing "=1" is therefore REPLACED, never left alone.
#
# Sets the parameters and refreshes the bootloader, but deliberately does NOT
# reboot — that is the operator's call on a hypervisor running VMs. The runtime
# fallback below takes effect immediately for already-attached devices.
# Skip with LM_SKIP_KERNEL_PARAMS=1.
LM_KERNEL_PARAMS="pcie_aspm=off intel_iommu=on pcie_power_pm=off usbcore.autosuspend=-1"

_lm_apply_cmdline() {
    # $1 = file, $2 = "grub" | "cmdline". Ensures every key=value in
    # $LM_KERNEL_PARAMS: an existing key has its VALUE replaced, a missing key is
    # appended. Returns 0 only if the file actually changed.
    #
    # awk + atomic replace rather than `sed -i`: sed's in-place flag differs
    # between GNU and BSD (BSD reads the next argument as a backup suffix, so
    # `sed -i -E` silently edits nothing), and a boot-config edit is the last
    # place to rely on that. awk behaves identically everywhere, which also makes
    # this testable off-host.
    local f="$1" kind="$2" tmp
    [ -f "$f" ] || return 1
    tmp="$f.lm-tmp.$$"
    awk -v P="$LM_KERNEL_PARAMS" -v KIND="$kind" '
        function esc(s) { gsub(/\./, "\\.", s); return s }
        # Canonical result: strip every managed key wherever it sits, then
        # re-append all of them in the declared order. Replacing in place would
        # leave each host ordered by whatever it happened to have first, so two
        # hosts with the same effective settings would still diff — this makes
        # /etc/default/grub byte-identical across the fleet. Unmanaged params
        # keep their position and are never touched.
        function ensure(l,   i, n, a, kv, re) {
            n = split(P, a, " ")
            for (i = 1; i <= n; i++) {
                split(a[i], kv, "=")
                re = "[[:space:]]*" esc(kv[1]) "=[^[:space:]\"]*"
                gsub(re, "", l)
            }
            gsub(/[[:space:]]+/, " ", l); sub(/^ /, "", l); sub(/ $/, "", l)
            for (i = 1; i <= n; i++) l = (l == "" ? a[i] : l " " a[i])
            return l
        }
        KIND == "grub" && /^GRUB_CMDLINE_LINUX_DEFAULT=/ && !seen {
            seen = 1
            inner = $0
            sub(/^GRUB_CMDLINE_LINUX_DEFAULT="/, "", inner)
            sub(/"[[:space:]]*$/, "", inner)
            $0 = "GRUB_CMDLINE_LINUX_DEFAULT=\"" ensure(inner) "\""
        }
        KIND == "cmdline" && NR == 1 { $0 = ensure($0) }
        { print }' "$f" > "$tmp" || { rm -f "$tmp"; return 1; }
    # Never leave a truncated or unchanged boot config behind.
    [ -s "$tmp" ] || { rm -f "$tmp"; return 1; }
    if cmp -s "$f" "$tmp"; then rm -f "$tmp"; return 1; fi
    cp -a "$f" "$f.lm-bak-$(date +%Y%m%d-%H%M%S)"
    mv "$tmp" "$f"
    return 0
}

if [ "${LM_SKIP_KERNEL_PARAMS:-0}" = "1" ]; then
    echo "🔌 Kernel power-management params: skipped (LM_SKIP_KERNEL_PARAMS=1)"
else
    echo "🔌 Applying kernel params: $LM_KERNEL_PARAMS"
    _lm_boot_changed=0
    # Proxmox uses systemd-boot (/etc/kernel/cmdline, ZFS/UEFI installs) OR GRUB.
    # Update whichever is present — some hosts carry both files, and keeping the
    # inactive one consistent is harmless.
    if [ -f /etc/kernel/cmdline ] && command -v proxmox-boot-tool >/dev/null 2>&1; then
        if _lm_apply_cmdline /etc/kernel/cmdline cmdline; then
            _lm_boot_changed=1
            proxmox-boot-tool refresh >/dev/null 2>&1 \
                && echo "   ✔ /etc/kernel/cmdline updated + proxmox-boot-tool refreshed" \
                || echo "   ⚠ /etc/kernel/cmdline updated but proxmox-boot-tool refresh FAILED — run it by hand"
        fi
    fi
    if [ -f /etc/default/grub ] && command -v update-grub >/dev/null 2>&1; then
        if _lm_apply_cmdline /etc/default/grub grub; then
            _lm_boot_changed=1
            update-grub >/dev/null 2>&1 \
                && echo "   ✔ /etc/default/grub updated + update-grub run" \
                || echo "   ⚠ /etc/default/grub updated but update-grub FAILED — run it by hand"
        fi
    fi
    if [ "$_lm_boot_changed" = "1" ]; then
        grep -h '^GRUB_CMDLINE_LINUX_DEFAULT=' /etc/default/grub 2>/dev/null | sed 's/^/     /'
        echo "   ⚠ REBOOT REQUIRED for the kernel parameters to take effect."
    else
        echo "   ✔ already set (no bootloader change needed)"
    fi
    # Runtime fallback: pin currently-attached USB devices on now, so a host that
    # will not be rebooted for a while stops suspending its dongles today. Devices
    # that enumerate later still follow the old default until reboot — which is
    # exactly why the kernel parameter above is the real fix.
    _lm_pinned=0
    for _f in /sys/bus/usb/devices/*/power/control; do
        [ -w "$_f" ] || continue
        if echo on > "$_f" 2>/dev/null; then _lm_pinned=$((_lm_pinned + 1)); fi
    done
    [ "$_lm_pinned" -gt 0 ] && echo "   ✔ pinned $_lm_pinned attached USB device(s) to power/control=on"
    echo "   verify after reboot: cat /sys/module/usbcore/parameters/autosuspend   (expect -1)"
fi

echo "🚀 Installing Proxmox Local Agent..."

INSTALL_DIR="/opt/lm/pxmx/agent"
REPO_DIR="$INSTALL_DIR/.pxmx_repo"
mkdir -p "$INSTALL_DIR"
# Log dir shared with the hub + spokes; the agent (User=root) writes its
# FileHandler here and the systemd unit appends stderr to the same file.
mkdir -p /var/log/lm

# Circular logging: cap /var/log/lm/*.log (+ legacy client-sim logs) so they
# can't fill the disk. copytruncate keeps the same inode so the running
# spoke/agent FileHandler + systemd StandardError=append: writers keep appending
# (both O_APPEND → no sparse files). Belt-and-suspenders alongside the app's
# RotatingFileHandler (LM_LOG_MAX_BYTES) in logging_setup.py.
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
# co-located LXC spoke auto-detect (this Proxmox host) > hub auto-discovery (DNS
# lm-hub.* / mDNS). All probing uses the just-installed venv + the vendored
# src/discovery.py. cwd is $INSTALL_DIR so `src.discovery` imports (src/ is a
# package dir).

# Co-located spoke auto-detect: in the common single-node topology the LM spoke
# runs as an LXC ON THIS Proxmox host, so the operator shouldn't have to type its
# IP. When nothing was pinned and `pct` is present, enumerate RUNNING containers,
# resolve each one's IP, and probe it for an LM agent listener (same probe the
# --spoke-ip path uses). The first that answers becomes --spoke-ip. Falls through
# to hub DNS/mDNS discovery below if pct is absent or no local container answers.
if [ -z "$SPOKE_IP" ] && [ "$SPOKE_URL_PINNED" != "1" ] && command -v pct >/dev/null 2>&1; then
    echo "🔎 No target given — scanning this Proxmox host's LXC containers for a co-located LM spoke…"
    for _vmid in $(pct list 2>/dev/null | awk 'NR>1 && /running/ {print $1}'); do
        _cip=$(pct exec "$_vmid" -- hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v '^127\.' | head -1)
        [ -z "$_cip" ] && continue
        _r=$(cd "$INSTALL_DIR" && "./venv/bin/python3" -m src.discovery --resolve-agent "$_cip" --timeout 3 2>/dev/null || echo NONE)
        if [ -n "$_r" ] && [ "$_r" != "NONE" ]; then
            SPOKE_IP="$_cip"
            echo "✅ Found a co-located LM spoke in LXC $_vmid at $_cip ($_r) — using --spoke-ip $_cip"
            break
        fi
    done
    [ -z "$SPOKE_IP" ] && echo "   No local LXC answered an LM agent listener; falling back to hub discovery."
fi

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
# derived case Python uses the bare `<hostname>` at startup, so a clone that was
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
    local a n
    n="$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo 0)"
    n="${n:-0}"
    # A crash-loop (NRestarts>=3) is NEVER ok — even with a (stale) healthy marker,
    # a unit that keeps restarting has not come up cleanly. Check this FIRST so a
    # stale marker can't override it.
    [ "$n" -ge 3 ] && return 1
    [ -f "$HEALTHY" ] && return 0
    a="$(systemctl show "$UNIT" -p ActiveState --value 2>/dev/null || echo "")"
    [ "$a" = "active" ] && return 0
    return 1
}

clear_and_prune() {
    python3 "$RECOVERY_PY" clearpending --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
    python3 "$RECOVERY_PY" prune --state-dir "$STATE_DIR" >/dev/null 2>&1 || true
}

# 1) Wait up to DEADLINE for the new code to boot + re-auth (healthy marker).
#    A healthy marker only counts when the unit is NOT crash-looping: a STALE
#    marker (left by the OLD version, never cleared by a broken new one that
#    exits before it can clear it) must never mask a crash-loop. NRestarts>=3
#    means the new code isn't staying up, so the marker is stale — fall through
#    to rollback. (The cs-svr-05 escape: a stale 0.07 marker made this exit 0.)
waited=0
while [ "$waited" -lt "$DEADLINE" ]; do
    if [ -f "$HEALTHY" ]; then
        _n="$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo 0)"; _n="${_n:-0}"
        if [ "$_n" -lt 3 ]; then
            clear_and_prune
            exit 0
        fi
        echo "lm-component-update-restart: $UNIT has a healthy marker but NRestarts=$_n (crash-loop) — stale marker; rolling back." >&2
        break
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
# ── KSM (kernel samepage merging) ───────────────────────────────────────────
# These hosts run a dozen-plus near-identical sim VMs cloned from one template,
# so their guest memory is highly dedupable. ksmtuned only starts KSM once free
# memory drops below KSM_THRES_COEF PERCENT of total; the stock 20 waits until
# the host is nearly full, which is far too late. 80 keeps KSM working
# essentially all the time -- a little CPU for a lot of reclaimed RAM.
# The agent re-asserts this on every start (host_tuning.py), so deployed hosts
# pick it up without a reinstall; this covers a fresh box.
KSM_THRES_COEF_WANT="${LM_KSM_THRES_COEF:-80}"
if [ -f /etc/ksmtuned.conf ]; then
    if grep -qE '^[[:space:]]*#?[[:space:]]*KSM_THRES_COEF[[:space:]]*=' /etc/ksmtuned.conf; then
        # Rewrite in place, commented or not -- appending past a commented
        # default leaves two plausible-looking lines in the file.
        sed -i -E "s|^[[:space:]]*#?[[:space:]]*KSM_THRES_COEF[[:space:]]*=.*|KSM_THRES_COEF=${KSM_THRES_COEF_WANT}|" /etc/ksmtuned.conf
    else
        echo "KSM_THRES_COEF=${KSM_THRES_COEF_WANT}" >> /etc/ksmtuned.conf
    fi
    systemctl enable --now ksmtuned >/dev/null 2>&1 || true
    systemctl restart ksmtuned >/dev/null 2>&1 || true
    if systemctl is-active --quiet ksmtuned; then
        echo "🧠 ksmtuned running (KSM_THRES_COEF=${KSM_THRES_COEF_WANT})"
    else
        echo "   ⚠ ksmtuned configured but NOT running — check: systemctl status ksmtuned"
    fi
else
    echo "   ⚠ /etc/ksmtuned.conf absent — ksm-control-daemon not installed; skipping KSM tuning"
fi

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
    echo "🆔 Agent ID: $(hostname)  (derived from hostname at startup)"
fi
echo "📦 Version: $(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo unknown)"
echo "🛡️  Rollback: /usr/local/bin/lm-component-update-restart — a failed self-update"
echo "    (crash at boot) is rolled back to the prior file-tree snapshot automatically."
echo "    NOTE: this watchdog lands only on a full installer re-run; a box that only"
echo "    git-pulled the new agent code must be re-installed once to enable it."
