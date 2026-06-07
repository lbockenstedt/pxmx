#!/bin/bash
set -e

echo "🚀 Installing Proxmox Manager Module (Native)..."

if [ "$(id -u)" -ne 0 ]; then
    echo "⚠️  This script must be run as root."
    exit 1
fi

apt-get update
apt-get install -y python3-pip python3-venv git

INSTALL_DIR="/root/lab-manager"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# PXMX depends on the Hub for its BaseSpoke definitions.
if [ ! -d "lm/.git" ]; then
    echo "🌐 Cloning required Hub repository..."
    git clone https://github.com/lbockenstedt/lm.git
fi

if [ -d "pxmx/.git" ]; then
    echo "📂 PXMX repository already exists. Updating..."
    cd pxmx && git pull && cd ..
else
    echo "🌐 Cloning Proxmox Manager repository..."
    git clone https://github.com/lbockenstedt/pxmx.git
fi

echo "🛠️ Setting up Proxmox Manager..."
cd pxmx
python3 -m venv venv
./venv/bin/python3 -m pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    ./venv/bin/python3 -m pip install -r requirements.txt
fi

echo "🎉 Proxmox Manager native installation complete!"
