#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/The-Little-mega-company/precog-workspace.git"
BUILD_DIR="$(mktemp -d /tmp/recogd-preshader-XXXXXX)"

cleanup() {
    rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

echo "[PRESHADER] Installing CachyOS / Arch runtime & build dependencies..."
sudo pacman -S --needed --noconfirm git base-devel python python-gobject gtk3 libayatana-appindicator

echo "[PRESHADER] Cloning RecogD Preshader repository..."
git clone --depth 1 "$REPO_URL" "$BUILD_DIR/recogd-workspace"

cd "$BUILD_DIR/recogd-workspace"

# Remove any leftover local/autostart conflicts
rm -f "$HOME/.local/bin/preshader" "$HOME/.config/autostart/recogd-preshader.desktop"

echo "[PRESHADER] Packing source archive for makepkg..."
mkdir -p packaging/aur
tar --exclude='packaging/aur/*.tar.gz' \
    --exclude='packaging/aur/*.pkg.tar.zst' \
    --exclude='packaging/aur/src' \
    --exclude='packaging/aur/pkg' \
    --exclude='.git' \
    -czf packaging/aur/recogd-preshader-unified.tar.gz \
    --transform='s,^\.,recogd-workspace,' .

cd packaging/aur

# Ensure PKGBUILD uses Arch GTK3/AppIndicator dependencies and has no bazzite-launch.sh reference
sed -i '/bazzite-launch\.sh/d' PKGBUILD
sed -i "s/depends=('python' 'tk' 'python-pyqt6')/depends=('python' 'python-gobject' 'gtk3' 'libayatana-appindicator')/" PKGBUILD

echo "[PRESHADER] Building and installing Arch/CachyOS package..."
makepkg -sif --noconfirm

echo "[PRESHADER] Enabling systemd user service..."
systemctl --user daemon-reload
systemctl --user enable --now recogd-preshader.service

echo "[PRESHADER] Installation complete! Run 'preshader --gui' to open the Control Deck."
