#!/usr/bin/env bash
set -e

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.local/bin"
mkdir -p "$HOME/.local/share/applications"
mkdir -p "$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$HOME/.local/share/icons/hicolor/256x256/apps"
mkdir -p "$HOME/.local/share/icons/hicolor/128x128/apps"
mkdir -p "$HOME/.local/share/icons/hicolor/64x64/apps"
mkdir -p "$HOME/.config/autostart"
mkdir -p "$HOME/.config/systemd/user"

cat > "$HOME/.local/bin/preshader" <<EOF
#!/usr/bin/env bash
exec "$INSTALL_DIR/scripts/recogd-launch.sh" "\$@"
EOF
chmod +x "$HOME/.local/bin/preshader" "$INSTALL_DIR/scripts/recogd-launch.sh" "$INSTALL_DIR/scripts/bazzite-launch.sh"

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
Categories=Game;System;Utility;Monitor;
Keywords=preshader;recogd;steam;cache;ram;dayz;shader;bazzite;control deck;
Actions=Prewarm;Purge;DiskCheck;

[Desktop Action Prewarm]
Name=Pre-Warm All Preshader Caches to RAM
Exec=$HOME/.local/bin/preshader --prewarm

[Desktop Action Purge]
Name=Purge Pinned RAM Cache (DONTNEED)
Exec=$HOME/.local/bin/preshader --purge

[Desktop Action DiskCheck]
Name=Run Disk Health & Block Integrity Check
Exec=$HOME/.local/bin/preshader --disk-check
EOF

cat > "$HOME/.config/autostart/recogd-preshader.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=RecogD Preshader Permanent System Tray
Exec=$HOME/.local/bin/preshader --minimized
Icon=$HOME/.local/share/icons/hicolor/256x256/apps/recogd-preshader.png
Terminal=false
StartupWMClass=recogd-preshader
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
EOF

cat > "$HOME/.config/systemd/user/recogd-preshader.service" <<EOF
[Unit]
Description=RecogD Preshader Unified Single-Process Engine & Permanent System Tray
After=graphical-session.target plasma-plasmashell.service

[Service]
Type=simple
Environment=DISPLAY=:0
ExecStart=$HOME/.local/bin/preshader --minimized
Restart=always
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

echo "[PRESHADER] Installed CLI, Application Menu, Permanent System Tray, and Systemd Service."
