#!/usr/bin/env bash
set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v pacman >/dev/null 2>&1; then
    echo "[RECOGD] This installer supports Arch Linux and Arch-based distributions only." >&2
    exit 1
fi

required_packages=(python python-gobject gtk3 libayatana-appindicator)
missing_packages=()
for package in "${required_packages[@]}"; do
    if ! pacman -Q "$package" >/dev/null 2>&1; then
        missing_packages+=("$package")
    fi
done

if ((${#missing_packages[@]})); then
    if ((EUID == 0)); then
        pacman -S --needed "${missing_packages[@]}"
    elif command -v sudo >/dev/null 2>&1; then
        sudo pacman -S --needed "${missing_packages[@]}"
    else
        printf '[RECOGD] Install the required packages: %s\n' "${missing_packages[*]}" >&2
        exit 1
    fi
fi

mkdir -p "$HOME/.local/bin"
mkdir -p "$HOME/.local/share/applications"
mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$HOME/.local/share/icons/hicolor/128x128/apps"
mkdir -p "$HOME/.local/share/icons/hicolor/64x64/apps"
mkdir -p "$HOME/.config/systemd/user"
rm -f "$HOME/.config/autostart/recogd-preshader.desktop"

cat > "$HOME/.local/bin/preshader" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/scripts/recogd-launch.sh" "\$@"
EOF
chmod +x "$HOME/.local/bin/preshader" "$INSTALL_DIR/scripts/recogd-launch.sh"

if [ -d "$INSTALL_DIR/assets/icons" ]; then
    cp -f "$INSTALL_DIR/assets/icons/recogd-preshader.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/recogd-preshader.svg" 2>/dev/null || true
    cp -f "$INSTALL_DIR/assets/icons/recogd-preshader-256.png" "$HOME/.local/share/icons/hicolor/256x256/apps/recogd-preshader.png" 2>/dev/null || true
    cp -f "$INSTALL_DIR/assets/icons/recogd-preshader-128.png" "$HOME/.local/share/icons/hicolor/128x128/apps/recogd-preshader.png" 2>/dev/null || true
    cp -f "$INSTALL_DIR/assets/icons/recogd-preshader-64.png" "$HOME/.local/share/icons/hicolor/64x64/apps/recogd-preshader.png" 2>/dev/null || true
fi

cat > "$HOME/.local/share/applications/recogd-preshader.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=RecogD Preshader Control Deck
GenericName=Predictive Game Asset Preloader & RAM Governor
Comment=Unified Dual-Bus Game Asset Pinner, Predictive Velocity Engine & RAM Budget Governor
Exec=$HOME/.local/bin/preshader --show-gui
Icon=$HOME/.local/share/icons/hicolor/256x256/apps/recogd-preshader.png
Terminal=false
StartupNotify=true
StartupWMClass=recogd-preshader
Categories=System;Monitor;
Keywords=preshader;recogd;steam;cache;ram;dayz;shader;control deck;
EOF

cat > "$HOME/.config/systemd/user/recogd-preshader.service" <<EOF
[Unit]
Description=RecogD predictive asset preloader and GTK control deck
After=graphical-session.target plasma-plasmashell.service

[Service]
Type=simple
ExecStart=$HOME/.local/bin/preshader --minimized
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
kbuildsycoca6 --noincremental 2>/dev/null || true

if command -v qdbus6 >/dev/null 2>&1; then
    qdbus6 org.kde.ActivityManager /ActivityManager/Resources/Linking \
        org.kde.ActivityManager.ResourcesLinking.LinkResourceToActivity \
        "org.kde.plasma.favorites.applications" "applications:recogd-preshader.desktop" ":global" >/dev/null 2>&1 || true

    qdbus6 org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript '
    var allPanels = panels();
    for (var i = 0; i < allPanels.length; i++) {
        var widgets = allPanels[i].widgets();
        for (var j = 0; j < widgets.length; j++) {
            var w = widgets[j];
            if (w.type == "org.kde.plasma.icontasks") {
                w.currentConfigGroup = ["General"];
                var current = w.readConfig("launchers", "").split(",");
                var cleaned = [];
                var hasRecogd = false;
                for (var k = 0; k < current.length; k++) {
                    var item = current[k].trim();
                    if (!item || item == "file:python3") continue;
                    if (item == "applications:recogd-preshader.desktop") hasRecogd = true;
                    cleaned.push(item);
                }
                if (!hasRecogd) cleaned.push("applications:recogd-preshader.desktop");
                w.writeConfig("launchers", cleaned.join(","));
                w.reloadConfig();
            }
        }
    }
    ' >/dev/null 2>&1 || true
fi

systemctl --user daemon-reload 2>/dev/null || true
systemctl --user enable --now recogd-preshader.service 2>/dev/null || true

echo "[PRESHADER] Installed CLI, Application Menu, and systemd user service."
