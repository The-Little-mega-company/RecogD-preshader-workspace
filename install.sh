#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ((EUID == 0)) && [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    TARGET_USER="$SUDO_USER"
    TARGET_GROUP="$(id -gn "$TARGET_USER")"
    TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
    TARGET_UID="$(id -u "$TARGET_USER")"
else
    TARGET_USER="$(id -un)"
    TARGET_GROUP="$(id -gn)"
    TARGET_HOME="$HOME"
    TARGET_UID="$(id -u)"
fi

run_privileged() {
    if ((EUID == 0)); then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "[PRESHADER] Error: root privileges (sudo) required to run: $*" >&2
        exit 1
    fi
}

install_dependencies() {
    if command -v pacman >/dev/null 2>&1; then
        local required_packages=(
            python
            python-gobject
            gtk3
            desktop-file-utils
            hicolor-icon-theme
        )
        if pacman -Q libayatana-appindicator >/dev/null 2>&1 || pacman -Si libayatana-appindicator >/dev/null 2>&1; then
            required_packages+=(libayatana-appindicator)
        elif pacman -Q libappindicator-gtk3 >/dev/null 2>&1 || pacman -Si libappindicator-gtk3 >/dev/null 2>&1; then
            required_packages+=(libappindicator-gtk3)
        fi

        local missing_packages=()
        local pkg
        for pkg in "${required_packages[@]}"; do
            if ! pacman -Q "$pkg" >/dev/null 2>&1; then
                missing_packages+=("$pkg")
            fi
        done

        if ((${#missing_packages[@]})); then
            echo "[PRESHADER] Installing Arch packages: ${missing_packages[*]}"
            if [[ -t 0 ]]; then
                run_privileged pacman -S --needed "${missing_packages[@]}"
            else
                run_privileged pacman -S --needed --noconfirm "${missing_packages[@]}"
            fi
        else
            echo "[PRESHADER] All Arch runtime packages already installed."
        fi
    elif command -v apt-get >/dev/null 2>&1; then
        local required_packages=(
            python3
            python3-gi
            python3-gi-cairo
            gir1.2-gtk-3.0
            desktop-file-utils
            hicolor-icon-theme
            libgtk-3-bin
        )
        local tray_pkg=""
        if dpkg -s gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then
            tray_pkg="gir1.2-ayatanaappindicator3-0.1"
        elif dpkg -s gir1.2-appindicator3-0.1 >/dev/null 2>&1; then
            tray_pkg="gir1.2-appindicator3-0.1"
        elif apt-cache show gir1.2-ayatanaappindicator3-0.1 >/dev/null 2>&1; then
            tray_pkg="gir1.2-ayatanaappindicator3-0.1"
        elif apt-cache show gir1.2-appindicator3-0.1 >/dev/null 2>&1; then
            tray_pkg="gir1.2-appindicator3-0.1"
        else
            tray_pkg="gir1.2-ayatanaappindicator3-0.1"
        fi
        required_packages+=("$tray_pkg")

        local missing_packages=()
        local pkg
        for pkg in "${required_packages[@]}"; do
            if ! dpkg -s "$pkg" >/dev/null 2>&1; then
                missing_packages+=("$pkg")
            fi
        done

        if ((${#missing_packages[@]})); then
            echo "[PRESHADER] Installing Debian/Ubuntu packages: ${missing_packages[*]}"
            run_privileged apt-get update
            run_privileged apt-get install -y "${missing_packages[@]}"
        else
            echo "[PRESHADER] All Debian/Ubuntu runtime packages already installed."
        fi
    else
        echo "[PRESHADER] Error: unsupported distribution (requires pacman or apt-get)." >&2
        exit 1
    fi
}

install_dependencies

APP_DIR="$TARGET_HOME/.local/share/recogd-preshader"
BIN_DIR="$TARGET_HOME/.local/bin"
CONFIG_DIR="$TARGET_HOME/.config/recogd"
APPS_DIR="$TARGET_HOME/.local/share/applications"
ICONS_DIR="$TARGET_HOME/.local/share/icons/hicolor"
AUTOSTART_DIR="$TARGET_HOME/.config/autostart"
SYSTEMD_USER_DIR="$TARGET_HOME/.config/systemd/user"

mkdir -p \
    "$APP_DIR/scripts" \
    "$BIN_DIR" \
    "$CONFIG_DIR" \
    "$APPS_DIR" \
    "$ICONS_DIR/scalable/apps" \
    "$ICONS_DIR/256x256/apps" \
    "$ICONS_DIR/128x128/apps" \
    "$ICONS_DIR/64x64/apps" \
    "$AUTOSTART_DIR" \
    "$SYSTEMD_USER_DIR"

# Install self-contained application payload into ~/.local/share/recogd-preshader
rm -rf "$APP_DIR/src" "$APP_DIR/assets"
cp -r "$INSTALL_DIR/src" "$APP_DIR/src"
cp -r "$INSTALL_DIR/assets" "$APP_DIR/assets"
cp -f "$INSTALL_DIR/scripts/recogd-launch.sh" "$APP_DIR/scripts/recogd-launch.sh"
cp -f "$INSTALL_DIR/LICENSE" "$APP_DIR/LICENSE"
cp -f "$INSTALL_DIR/README.md" "$APP_DIR/README.md"
chmod +x "$APP_DIR/scripts/recogd-launch.sh"
if [[ -w "$INSTALL_DIR/scripts/recogd-launch.sh" ]]; then
    chmod +x "$INSTALL_DIR/scripts/recogd-launch.sh"
fi

if [[ -f "$INSTALL_DIR/config.json" ]]; then
    if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
        cp -f "$INSTALL_DIR/config.json" "$CONFIG_DIR/config.json"
    fi
    if [[ ! -f "$APP_DIR/config.json" ]]; then
        cp -f "$CONFIG_DIR/config.json" "$APP_DIR/config.json"
    fi
fi

# Sync /opt/preshader if an older system-wide package install exists and is writable
if [[ -d /opt/preshader ]]; then
    sync_opt=0
    if [[ -w /opt/preshader ]]; then
        sync_opt=1
    elif ((EUID == 0)) || sudo -n true 2>/dev/null; then
        sync_opt=1
    fi
    if [[ "$sync_opt" == 1 ]]; then
        run_privileged rm -rf /opt/preshader/src /opt/preshader/assets /opt/preshader/scripts
        run_privileged mkdir -p /opt/preshader/scripts
        run_privileged cp -r "$INSTALL_DIR/src" /opt/preshader/src
        run_privileged cp -r "$INSTALL_DIR/assets" /opt/preshader/assets
        run_privileged cp -f "$INSTALL_DIR/scripts/recogd-launch.sh" /opt/preshader/scripts/recogd-launch.sh
        run_privileged chmod +x /opt/preshader/scripts/recogd-launch.sh
    fi
fi

# Install terminal commands ('preshader' and 'Preshader') in ~/.local/bin
for cmd_name in preshader Preshader; do
    cat > "$BIN_DIR/$cmd_name" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/scripts/recogd-launch.sh" "\$@"
EOF
    chmod 755 "$BIN_DIR/$cmd_name"
done

ensure_path_rc() {
    local rc_file="$1"
    local marker="# RecogD Preshader CLI PATH"
    if [[ ! -f "$rc_file" ]]; then
        touch "$rc_file"
    fi
    if ! grep -Fq "$marker" "$rc_file" && ! grep -Fq '.local/bin' "$rc_file"; then
        cat >> "$rc_file" <<'EOF'

# RecogD Preshader CLI PATH
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
EOF
    fi
}

ensure_path_rc "$TARGET_HOME/.bashrc"
ensure_path_rc "$TARGET_HOME/.profile"
if [[ -f "$TARGET_HOME/.zshrc" ]]; then
    ensure_path_rc "$TARGET_HOME/.zshrc"
fi

# Also install /usr/local/bin/preshader (+ Preshader) when writable, passwordless sudo works,
# or ~/.local/bin is missing from PATH in an interactive shell.
need_system_shim=0
if [[ -w /usr/local/bin ]] || ((EUID == 0)) || sudo -n true 2>/dev/null; then
    need_system_shim=1
elif [[ -t 0 && ":$PATH:" != *":$BIN_DIR:"* ]] && command -v sudo >/dev/null 2>&1; then
    need_system_shim=1
fi

if [[ "$need_system_shim" == 1 ]]; then
    tmp_shim="$(mktemp)"
    cat > "$tmp_shim" <<'EOF'
#!/usr/bin/env bash
if [ -x "$HOME/.local/share/recogd-preshader/scripts/recogd-launch.sh" ]; then
    exec "$HOME/.local/share/recogd-preshader/scripts/recogd-launch.sh" "$@"
fi
exec /opt/preshader/scripts/recogd-launch.sh "$@"
EOF
    for cmd_name in preshader Preshader; do
        run_privileged install -Dm755 "$tmp_shim" "/usr/local/bin/$cmd_name"
    done
    rm -f "$tmp_shim"
fi

# Install application icons into hicolor theme
if [[ -d "$APP_DIR/assets/icons" ]]; then
    cp -f "$APP_DIR/assets/icons/recogd-preshader.svg" "$ICONS_DIR/scalable/apps/recogd-preshader.svg"
    cp -f "$APP_DIR/assets/icons/recogd-preshader-256.png" "$ICONS_DIR/256x256/apps/recogd-preshader.png"
    cp -f "$APP_DIR/assets/icons/recogd-preshader-128.png" "$ICONS_DIR/128x128/apps/recogd-preshader.png"
    cp -f "$APP_DIR/assets/icons/recogd-preshader-64.png" "$ICONS_DIR/64x64/apps/recogd-preshader.png"
    if [[ ! -f "$ICONS_DIR/index.theme" && -f /usr/share/icons/hicolor/index.theme ]]; then
        cp -f /usr/share/icons/hicolor/index.theme "$ICONS_DIR/index.theme"
    fi
fi

# Install Application Menu .desktop entry
cat > "$APPS_DIR/recogd-preshader.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=RecogD Preshader Control Deck
GenericName=Predictive Game Asset Preloader & RAM Governor
Comment=Unified Dual-Bus Game Asset Pinner, Predictive Velocity Engine & RAM Budget Governor
Exec=$BIN_DIR/preshader --show-gui
Icon=recogd-preshader
Terminal=false
StartupNotify=true
StartupWMClass=recogd-preshader
Categories=System;Monitor;
Keywords=preshader;recogd;steam;cache;ram;shader;linux;control deck;
Actions=ForceGUI;Prewarm;Purge;

[Desktop Action ForceGUI]
Name=Force Open Control Deck GUI (Wayland Fallback)
Exec=$BIN_DIR/preshader --GUI

[Desktop Action Prewarm]
Name=Pre-Warm All Preshader Caches to RAM
Exec=$BIN_DIR/preshader --prewarm

[Desktop Action Purge]
Name=Purge Pinned RAM Cache (DONTNEED)
Exec=$BIN_DIR/preshader --purge
EOF
chmod 644 "$APPS_DIR/recogd-preshader.desktop"

# Install Permanent System Tray autostart entry
cat > "$AUTOSTART_DIR/recogd-preshader.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=RecogD Preshader Permanent System Tray
Exec=$BIN_DIR/preshader --minimized
Icon=recogd-preshader
Terminal=false
StartupWMClass=recogd-preshader
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
EOF
chmod 644 "$AUTOSTART_DIR/recogd-preshader.desktop"

# Install background systemd user service (no GUI spawn during installation)
cat > "$SYSTEMD_USER_DIR/recogd-preshader.service" <<'EOF'
[Unit]
Description=RecogD Preshader Background Predictive Asset Preloader
After=default.target

[Service]
Type=simple
ExecStart=%h/.local/bin/preshader --nogui
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

if ((EUID == 0)) && [[ "$TARGET_USER" != "root" ]]; then
    chown -R "$TARGET_USER:$TARGET_GROUP" \
        "$APP_DIR" \
        "$CONFIG_DIR" \
        "$BIN_DIR/preshader" \
        "$BIN_DIR/Preshader" \
        "$APPS_DIR/recogd-preshader.desktop" \
        "$ICONS_DIR" \
        "$AUTOSTART_DIR/recogd-preshader.desktop" \
        "$SYSTEMD_USER_DIR/recogd-preshader.service" \
        "$TARGET_HOME/.bashrc" \
        "$TARGET_HOME/.profile"
    if [[ -f "$TARGET_HOME/.zshrc" ]]; then
        chown "$TARGET_USER:$TARGET_GROUP" "$TARGET_HOME/.zshrc"
    fi
fi

run_as_target_user() {
    local runtime_dir="/run/user/$TARGET_UID"
    local dbus_addr="unix:path=$runtime_dir/bus"
    if ((EUID == 0)) && [[ "$TARGET_USER" != "root" ]]; then
        if [[ -S "$runtime_dir/bus" ]]; then
            su - "$TARGET_USER" -c "XDG_RUNTIME_DIR='$runtime_dir' DBUS_SESSION_BUS_ADDRESS='$dbus_addr' $*"
        else
            su - "$TARGET_USER" -c "XDG_RUNTIME_DIR='$runtime_dir' $*"
        fi
    else
        if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "$runtime_dir" ]]; then
            export XDG_RUNTIME_DIR="$runtime_dir"
        fi
        if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "$runtime_dir/bus" ]]; then
            export DBUS_SESSION_BUS_ADDRESS="$dbus_addr"
        fi
        "$@"
    fi
}

command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f "$ICONS_DIR" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$APPS_DIR" 2>/dev/null || true
if ((EUID != 0)) || [[ "$TARGET_USER" == "root" ]]; then
    command -v xdg-desktop-menu >/dev/null 2>&1 && xdg-desktop-menu forceupdate 2>/dev/null || true
    command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 --noincremental 2>/dev/null || true
    command -v kbuildsycoca5 >/dev/null 2>&1 && kbuildsycoca5 --noincremental 2>/dev/null || true
fi

# Stop any outdated daemon processes from older install locations, then restart user service cleanly
pkill -u "$TARGET_UID" -f "src/daemon.py" 2>/dev/null || true
sleep 0.2
run_as_target_user systemctl --user daemon-reload 2>/dev/null || true
run_as_target_user systemctl --user enable recogd-preshader.service 2>/dev/null || true
run_as_target_user systemctl --user restart recogd-preshader.service 2>/dev/null || true

echo "[PRESHADER] Installation complete!"
echo "  - Terminal commands : preshader | preshader --GUI | preshader --stats"
echo "  - Application menu  : RecogD Preshader Control Deck ($APPS_DIR/recogd-preshader.desktop)"
echo "  - Background daemon : systemctl --user status recogd-preshader.service"
if [[ ":$PATH:" != *":$BIN_DIR:"* ]] && [[ ! -x /usr/local/bin/preshader ]]; then
    echo "[PRESHADER] Note: open a new shell or run 'export PATH=\"$BIN_DIR:\$PATH\"' to use 'preshader' in this session."
fi
