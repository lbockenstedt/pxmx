#!/bin/bash
set -e

echo "🗑️  Uninstalling Proxmox Local Agent..."

# 1. Stop and disable the systemd service
if systemctl is-active --quiet lm-manager-pxmx-agent.service; then
    echo "Stopping service..."
    systemctl stop lm-manager-pxmx-agent.service
fi

if systemctl is-enabled --quiet lm-manager-pxmx-agent.service; then
    echo "Disabling service..."
    systemctl disable lm-manager-pxmx-agent.service
fi

# 2. Remove the systemd service file
SERVICE_FILE="/etc/systemd/ la-manager-pxmx-agent.service"
if [ -f "$SERVICE_FILE" ]; then
    echo "Removing service file..."
    rm "$SERVICE_FILE"
    systemctl daemon-reload
fi

# 3. Remove installation directory
INSTALL_DIR="/root/lm-manager/pxmx/agent"
if [ -d "$INSTALL_DIR" ]; then
    echo "Removing installation directory $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
fi

echo "🎉 Proxmox Local Agent has been successfully removed."
