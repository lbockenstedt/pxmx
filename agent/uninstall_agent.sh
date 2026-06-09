#!/bin/bash
set -e

echo "🗑️ Uninstalling Proxmox Local Agent..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

# 1. Stop and disable the systemd service
if systemctl is-active --quiet lm-pxmx-agent; then
    echo "Stopping lm-pxmx-agent service..."
    systemctl stop lm-pxmx-agent
fi

if systemctl is-enabled --quiet lm-pxmx-agent; then
    echo "Disabling lm-pxmx-agent service..."
    systemctl disable lm-pxmx-agent
fi

echo "Removing systemd service file..."
rm -f /etc/systemd/system/lm-pxmx-agent.service
systemctl daemon-reload

# 2. Remove installation directory
INSTALL_DIR="/opt/lm/pxmx/agent"
if [ -d "$INSTALL_DIR" ]; then
    echo "Removing installation directory $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
else
    echo "Installation directory $INSTALL_DIR not found, skipping."
fi

echo "🎉 Proxmox Local Agent uninstalled successfully!"
