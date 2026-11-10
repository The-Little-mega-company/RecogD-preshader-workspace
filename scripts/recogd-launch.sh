#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DRIVE_LABEL="preshader"
DRIVE_MOUNT="/mnt/preshader"
FALLBACK_VAULT="$HOME/.cache/recogd-fallback"

if [ "${1:-}" = "--stats" ] || [ "${1:-}" = "stats" ]; then
    exec python3 "$WORKSPACE_DIR/src/cli_stats.py"
fi

mkdir -p "$FALLBACK_VAULT/games" "$FALLBACK_VAULT/global_shader_cache"

auto_mount_external_ssd() {
    # 1. Check if /mnt/preshader is already mounted
    if mountpoint -q "$DRIVE_MOUNT" 2>/dev/null; then
        return 0
    fi

    # 2. Check if KDE/Desktop already mounted it under /run/media/$USER/preshader
    for media_base in "/run/media/$USER" "/var/run/media/$USER" "/media/$USER"; do
        if [ -d "$media_base/$DRIVE_LABEL" ]; then
            DRIVE_MOUNT="$media_base/$DRIVE_LABEL"
            return 0
        fi
    done

    # 3. Locate the 'preshader' ext4 partition (e.g. /dev/sda4)
    DEV_NODE=""
    if [ -e "/dev/disk/by-label/$DRIVE_LABEL" ]; then
        DEV_NODE="$(readlink -f "/dev/disk/by-label/$DRIVE_LABEL")"
    elif command -v blkid >/dev/null 2>&1; then
        DEV_NODE="$(blkid -L "$DRIVE_LABEL" 2>/dev/null || true)"
    fi

    if [ -z "$DEV_NODE" ] && command -v lsblk >/dev/null 2>&1; then
        DEV_NODE="$(lsblk -rno NAME,LABEL,MOUNTPOINT | awk -v lbl="$DRIVE_LABEL" '$2==lbl {print "/dev/"$1; exit}')"
    fi

    if [ -z "$DEV_NODE" ]; then
        return 1
    fi

    echo "[PRESHADER-MOUNT] Found external ext4 partition: $DEV_NODE (LABEL=$DRIVE_LABEL)"

    # Prefer rootless udisksctl first
    if command -v udisksctl >/dev/null 2>&1; then
        udisksctl mount -b "$DEV_NODE" --options noatime --no-user-interaction >/dev/null 2>&1 || true
        ACTUAL_MNT="$(lsblk -no MOUNTPOINT "$DEV_NODE" | head -n 1)"
        if [ -n "$ACTUAL_MNT" ]; then
            DRIVE_MOUNT="$ACTUAL_MNT"
            return 0
        fi
    fi

    # Fallback to sudo mount to /mnt/preshader
    echo "[PRESHADER-MOUNT] Mounting $DEV_NODE to $DRIVE_MOUNT..."
    sudo mkdir -p "$DRIVE_MOUNT"
    sudo mount -o defaults,noatime,nodiratime "$DEV_NODE" "$DRIVE_MOUNT"
    mountpoint -q "$DRIVE_MOUNT" 2>/dev/null
}

if auto_mount_external_ssd; then
    # Newly formatted ext4 partitions belong to root by default — grant user ownership automatically
    if [ ! -w "$DRIVE_MOUNT" ]; then
        echo "[PRESHADER-MOUNT] Fixing ext4 ownership on $DRIVE_MOUNT for user $USER..."
        sudo chown -R "$USER:$USER" "$DRIVE_MOUNT"
    fi

    # Tune USB block read-ahead queue if passwordless sudo is active
    DEV_SRC="$(findmnt -n -o SOURCE "$DRIVE_MOUNT" 2>/dev/null || true)"
    if [ -n "$DEV_SRC" ]; then
        BASE_BLOCK="$(basename "$DEV_SRC" | sed 's/[0-9]*$//')"
        RA_PATH="/sys/block/$BASE_BLOCK/queue/read_ahead_kb"
        if [ -e "$RA_PATH" ] && sudo -n true 2>/dev/null; then
            echo 4096 | sudo tee "$RA_PATH" >/dev/null 2>&1 || true
        fi
    fi

    VAULT_DIR="$DRIVE_MOUNT/recogd-vault"
    echo "[PRESHADER] Dual-Bus Mode ONLINE: Internal NVMe + External Ext4 SSD ($DRIVE_MOUNT)"
else
    VAULT_DIR="$FALLBACK_VAULT"
    echo "[PRESHADER] External 'preshader' partition not detected. Using Internal NVMe Fallback ($VAULT_DIR)"
fi

mkdir -p "$VAULT_DIR/games" "$VAULT_DIR/global_shader_cache"

export MESA_SHADER_CACHE_DIR="$VAULT_DIR/global_shader_cache"
export MESA_SHADER_CACHE_MAX_SIZE="10G"
export DXVK_STATE_CACHE_PATH="$VAULT_DIR/global_shader_cache"
export RADV_PERFTEST="gpl"

# Run daemon in foreground so live detections stream directly to Konsole and Ctrl+C stops cleanly
exec python3 "$WORKSPACE_DIR/src/daemon.py" \
    --vault-root "$VAULT_DIR" \
    --ext-mount "$DRIVE_MOUNT"
