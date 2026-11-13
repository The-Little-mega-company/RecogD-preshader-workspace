#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SYS_PYTHON="/usr/bin/python3"
[ ! -x "$SYS_PYTHON" ] && SYS_PYTHON="python3"

DRIVE_LABEL="preshader"
DRIVE_MOUNT="/mnt/preshader"
FALLBACK_VAULT="$HOME/.cache/recogd-fallback"

if [ "${1:-}" = "--stats" ] || [ "${1:-}" = "stats" ]; then
    exec "$SYS_PYTHON" "$WORKSPACE_DIR/src/cli_stats.py"
fi

if [ "${1:-}" = "--gui" ] || [ "${1:-}" = "gui" ]; then
    exec "$SYS_PYTHON" "$WORKSPACE_DIR/src/gui.py"
fi

auto_mount_external_ssd() {
    for media_base in "/run/media/$USER" "/var/run/media/$USER" "/media/$USER" "/mnt"; do
        if [ -d "$media_base/$DRIVE_LABEL" ] && [ -w "$media_base/$DRIVE_LABEL" ]; then
            DRIVE_MOUNT="$media_base/$DRIVE_LABEL"
            return 0
        fi
    done

    if command -v lsblk >/dev/null 2>&1; then
        EXISTING_MNT="$(lsblk -rno LABEL,MOUNTPOINT | awk -v lbl="$DRIVE_LABEL" 'tolower($1)==tolower(lbl) && $2!="" {print $2; exit}')"
        if [ -n "$EXISTING_MNT" ] && [ -d "$EXISTING_MNT" ]; then
            DRIVE_MOUNT="$EXISTING_MNT"
            return 0
        fi
    fi

    DEV_NODE=""
    if [ -e "/dev/disk/by-label/$DRIVE_LABEL" ]; then
        DEV_NODE="$(readlink -f "/dev/disk/by-label/$DRIVE_LABEL")"
    elif command -v lsblk >/dev/null 2>&1; then
        DEV_NODE="$(lsblk -rno NAME,LABEL | awk -v lbl="$DRIVE_LABEL" 'tolower($2)==tolower(lbl) {print "/dev/"$1; exit}')"
    fi

    if [ -z "$DEV_NODE" ]; then
        return 1
    fi

    if command -v udisksctl >/dev/null 2>&1; then
        udisksctl mount -b "$DEV_NODE" --options noatime --no-user-interaction >/dev/null 2>&1 || true
        ACTUAL_MNT="$(lsblk -no MOUNTPOINT "$DEV_NODE" | head -n 1)"
        if [ -n "$ACTUAL_MNT" ] && [ -d "$ACTUAL_MNT" ]; then
            DRIVE_MOUNT="$ACTUAL_MNT"
            return 0
        fi
    fi

    return 1
}

if auto_mount_external_ssd; then
    VAULT_DIR="$DRIVE_MOUNT/recogd-vault"
    mkdir -p "$VAULT_DIR" 2>/dev/null || true
    echo "[PRESHADER] Dual-Bus Mode ONLINE: External Ext4 SSD ($DRIVE_MOUNT)"
else
    VAULT_DIR="$FALLBACK_VAULT"
    mkdir -p "$VAULT_DIR"
    echo "[PRESHADER] External SSD not mounted. Using NVMe Fallback ($VAULT_DIR)"
fi

# If launched from Desktop App Icon (no terminal attached) and daemon is already running, just pop up GUI
if [ ! -t 0 ] && pgrep -f "recogd-workspace/src/daemon.py" >/dev/null 2>&1; then
    exec "$SYS_PYTHON" "$WORKSPACE_DIR/src/gui.py"
fi

# Launch Direct Control Deck GUI in background if display is active
GUI_PID=""
if [ "${1:-}" != "--nogui" ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; }; then
    if ! pgrep -f "recogd-workspace/src/gui.py" >/dev/null 2>&1; then
        "$SYS_PYTHON" "$WORKSPACE_DIR/src/gui.py" >/tmp/recogd-gui.log 2>&1 &
        GUI_PID=$!
        echo "[PRESHADER] Direct Control Deck GUI & System Tray Launched (PID: $GUI_PID)"
    fi
fi

cleanup() {
    if [ -n "$GUI_PID" ] && kill -0 "$GUI_PID" 2>/dev/null; then
        kill "$GUI_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

exec "$SYS_PYTHON" "$WORKSPACE_DIR/src/daemon.py" --vault-root "$VAULT_DIR" --ext-mount "$DRIVE_MOUNT"
