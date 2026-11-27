#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/The-Little-mega-company/RecogD-preshader-workspace.git"
BUILD_DIR="$(mktemp -d /tmp/recogd-preshader-XXXXXX)"

cleanup() {
    rm -rf "$BUILD_DIR"
}
trap cleanup EXIT

if ! command -v git >/dev/null 2>&1; then
    echo "[PRESHADER] Installing git prerequisite..."
    if command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm git
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y git
    else
        echo "[PRESHADER] Please install 'git' and re-run." >&2
        exit 1
    fi
fi

echo "[PRESHADER] Cloning RecogD Preshader repository..."
git clone --depth 1 "$REPO_URL" "$BUILD_DIR/recogd-workspace"

cd "$BUILD_DIR/recogd-workspace"
chmod +x install.sh
./install.sh
