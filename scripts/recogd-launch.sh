#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
DRIVE_LABEL="preshader"
DRIVE_MOUNT="/mnt/preshader"
LOCAL_USER="${USER:-$(id -un)}"
FALLBACK_VAULT="$HOME/.cache/recogd-fallback"
LOG_FILE="${XDG_CACHE_HOME:-$HOME/.cache}/recogd/daemon.log"

ACTION="run"
RUN_GUI=1
MINIMIZED=0
SHOW_GUI=0
FORCE_WAYLAND_GUI=0
DAEMON_ARGS=()

print_help() {
    cat <<'EOF'
RecogD Preshader — Unified CLI & Control Deck Launcher

Usage:
  preshader                 Launch background daemon (if offline), open Control Deck GUI, and stream live logs
  preshader --GUI           Force-open / re-render Control Deck GUI (Wayland XWayland/Cairo recovery fallback)
  preshader --stats         Display live daemon telemetry, RAM Governor budget, and per-game brain stats
  preshader --prewarm       Pre-warm learned assets and shader caches into RAM
  preshader --purge         Purge currently pinned RAM cache via POSIX_FADV_DONTNEED
  preshader --nogui         Run daemon only without opening the GUI
  preshader --minimized     Launch Control Deck minimized to the system tray
  preshader --stop          Gracefully stop the running daemon and GUI
  preshader --help          Show this help message
EOF
}

while (($#)); do
    case "$1" in
        --stats|stats|-s)
            exec "$PYTHON" "$WORKSPACE_DIR/src/cli_stats.py"
            ;;
        --help|-h|help)
            print_help
            exit 0
            ;;
        --prewarm|prewarm)
            ACTION="prewarm"
            shift
            ;;
        --purge|purge)
            ACTION="purge"
            shift
            ;;
        --stop|stop)
            ACTION="stop"
            shift
            ;;
        --nogui)
            RUN_GUI=0
            shift
            ;;
        --minimized)
            RUN_GUI=1
            MINIMIZED=1
            shift
            ;;
        --show-gui)
            RUN_GUI=1
            MINIMIZED=0
            SHOW_GUI=1
            shift
            ;;
        --GUI|--gui|GUI|gui|--force-gui)
            RUN_GUI=1
            MINIMIZED=0
            FORCE_WAYLAND_GUI=1
            shift
            ;;
        *)
            DAEMON_ARGS+=("$1")
            shift
            ;;
    esac
done

find_user_procs() {
    local script_suffix="$1"
    local pid
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        if [[ -r "/proc/$pid/cmdline" ]] &&
            tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Eq "(^|/)${script_suffix}$"; then
            printf '%s\n' "$pid"
        fi
    done < <(pgrep -u "$(id -u)" -f "$script_suffix" 2>/dev/null || true)
}

stop_user_procs() {
    local script_suffix="$1"
    local pids=()
    local pid
    while IFS= read -r pid; do
        [[ -n "$pid" ]] && pids+=("$pid")
    done < <(find_user_procs "$script_suffix")

    if ((${#pids[@]})); then
        kill "${pids[@]}" 2>/dev/null || true
        for _ in {1..15}; do
            local alive=0
            for pid in "${pids[@]}"; do
                if kill -0 "$pid" 2>/dev/null; then
                    alive=1
                    break
                fi
            done
            [[ "$alive" == 0 ]] && break
            sleep 0.1
        done
    fi
}

if [[ "$ACTION" == "purge" ]]; then
    PYTHONPATH="$WORKSPACE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" -c 'import config_ipc; cfg = config_ipc.read_live_config(); config_ipc.update_live_config({"flush_generation": int(cfg.get("flush_generation", 0)) + 1}, save_persistent=False)'
    echo "[PRESHADER] Requested immediate RAM cache purge (POSIX_FADV_DONTNEED)."
    exit 0
fi

if [[ "$ACTION" == "stop" ]]; then
    PYTHONPATH="$WORKSPACE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" -c 'import config_ipc; config_ipc.update_live_config({"shutdown_requested": True}, save_persistent=False)' >/dev/null 2>&1 || true
    stop_user_procs "src/gui.py"
    sleep 0.3
    stop_user_procs "src/daemon.py"
    rm -f "/dev/shm/recogd_icon_$(id -u)_"*.svg 2>/dev/null || true
    echo "[PRESHADER] Daemon and Control Deck stopped."
    exit 0
fi

find_external_mount() {
    local candidate device
    local candidates=(
        "/mnt/$DRIVE_LABEL"
        "/run/media/$LOCAL_USER/$DRIVE_LABEL"
        "/var/run/media/$LOCAL_USER/$DRIVE_LABEL"
        "/media/$LOCAL_USER/$DRIVE_LABEL"
    )

    for candidate in "${candidates[@]}"; do
        if [[ -d "$candidate" && -w "$candidate" ]] && mountpoint -q "$candidate" 2>/dev/null; then
            DRIVE_MOUNT="$(realpath "$candidate")"
            return 0
        fi
    done

    if [[ -e "/dev/disk/by-label/$DRIVE_LABEL" ]] && command -v udisksctl >/dev/null 2>&1; then
        device="$(realpath "/dev/disk/by-label/$DRIVE_LABEL")"
        udisksctl mount -b "$device" --options noatime --no-user-interaction >/dev/null 2>&1 || true
        for candidate in "${candidates[@]}"; do
            if [[ -d "$candidate" && -w "$candidate" ]] && mountpoint -q "$candidate" 2>/dev/null; then
                DRIVE_MOUNT="$(realpath "$candidate")"
                return 0
            fi
        done
    fi

    return 1
}

mkdir -p "$FALLBACK_VAULT"
FALLBACK_VAULT="$(realpath "$FALLBACK_VAULT")"
if find_external_mount; then
    VAULT_DIR="$DRIVE_MOUNT/recogd-vault"
    if ! mkdir -p "$VAULT_DIR/games" "$VAULT_DIR/global_shader_cache" 2>/dev/null; then
        VAULT_DIR="$FALLBACK_VAULT"
        DRIVE_MOUNT="/mnt/preshader"
    fi
else
    VAULT_DIR="$FALLBACK_VAULT"
fi
mkdir -p "$VAULT_DIR/games" "$VAULT_DIR/global_shader_cache"
VAULT_DIR="$(realpath "$VAULT_DIR")"

export MESA_SHADER_CACHE_DIR="$VAULT_DIR/global_shader_cache"
export MESA_SHADER_CACHE_MAX_SIZE="10G"
export DXVK_STATE_CACHE_PATH="$VAULT_DIR/global_shader_cache"
export RADV_PERFTEST="gpl"

if [[ "$ACTION" == "prewarm" ]]; then
    PYTHONPATH="$WORKSPACE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" - "$VAULT_DIR" "$DRIVE_MOUNT" <<'PYEOF'
import os
import sys
import config_ipc
from pinner import AssetPinner
from vault_merger import VaultCacheMerger

vault_dir = os.path.abspath(sys.argv[1])
ext_mount = os.path.abspath(sys.argv[2])

class _PrewarmStorage:
    def __init__(self):
        self.external_mount = ext_mount
        self.external_online = (
            os.path.isdir(ext_mount)
            and vault_dir.startswith(ext_mount)
            and not vault_dir.startswith(os.path.expanduser("~/.cache"))
        )

    def resolve_dual_bus_path(self, filepath, _toggle):
        return filepath

pinner = AssetPinner(_PrewarmStorage(), max_file_mb=16)
pinner.apply_live_config(config_ipc.read_live_config())
merger = VaultCacheMerger()
files_pinned, bytes_queued = merger.prewarm_precompiled_caches(pinner)

global_dir = os.path.join(vault_dir, "global_shader_cache")
if os.path.isdir(global_dir):
    for root, _, files in os.walk(global_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            if pinner.request_pin(fpath):
                files_pinned += 1
                try:
                    bytes_queued += os.path.getsize(fpath)
                except OSError:
                    pass

print(f"[PRESHADER] Pre-warmed {files_pinned} cached files ({bytes_queued / (1024 * 1024):.2f} MB) into RAM.")
PYEOF
    exit 0
fi

ensure_display_env() {
    local uid
    uid="$(id -u)"
    if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$uid" ]]; then
        export XDG_RUNTIME_DIR="/run/user/$uid"
    fi
    if [[ -z "${WAYLAND_DISPLAY:-}" && -n "${XDG_RUNTIME_DIR:-}" ]]; then
        local sock
        for sock in "$XDG_RUNTIME_DIR"/wayland-*; do
            [[ -S "$sock" ]] || continue
            export WAYLAND_DISPLAY
            WAYLAND_DISPLAY="$(basename "$sock")"
            break
        done
    fi
    if [[ -z "${DISPLAY:-}" ]]; then
        local xsock
        for xsock in /tmp/.X11-unix/X*; do
            [[ -S "$xsock" ]] || continue
            export DISPLAY=":${xsock##*X}"
            break
        done
    fi
}

if [[ "$RUN_GUI" == 1 ]]; then
    ensure_display_env
    if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
        printf '[PRESHADER] No graphical session detected; starting daemon without the GUI.\n' >&2
        RUN_GUI=0
    fi
fi

mkdir -p "$(dirname "$LOG_FILE")"

if [[ "$RUN_GUI" == 0 ]]; then
    existing_daemon="$(find_user_procs "src/daemon.py" | head -n 1 || true)"
    if [[ -n "$existing_daemon" ]]; then
        printf '[PRESHADER] Daemon is already running (PID %s).\n' "$existing_daemon"
        exit 0
    fi
    exec "$PYTHON" -u "$WORKSPACE_DIR/src/daemon.py" \
        --vault-root "$VAULT_DIR" \
        --ext-mount "$DRIVE_MOUNT" \
        "${DAEMON_ARGS[@]}"
fi

GUI_ARGS=()
if [[ "$MINIMIZED" == 1 ]]; then
    GUI_ARGS+=(--minimized)
elif [[ "$FORCE_WAYLAND_GUI" == 1 ]]; then
    stop_user_procs "src/gui.py"
    rm -f "/dev/shm/recogd_icon_$(id -u)_"*.svg 2>/dev/null || true
    if [[ -n "${WAYLAND_DISPLAY:-}" || "${XDG_SESSION_TYPE:-}" == "wayland" ]]; then
        if [[ -n "${DISPLAY:-}" ]] || compgen -G "/tmp/.X11-unix/X*" >/dev/null 2>&1; then
            export GDK_BACKEND="x11,wayland"
            export GDK_GL="disable"
        else
            export GDK_BACKEND="wayland"
            export GDK_GL="disable"
            export GSK_RENDERER="cairo"
        fi
    fi
    GUI_ARGS+=(--force-gui)
else
    GUI_ARGS+=(--show-gui)
fi

existing_daemon="$(find_user_procs "src/daemon.py" | head -n 1 || true)"
if [[ -z "$existing_daemon" ]]; then
    nohup "$PYTHON" -u "$WORKSPACE_DIR/src/daemon.py" \
        --vault-root "$VAULT_DIR" \
        --ext-mount "$DRIVE_MOUNT" \
        "${DAEMON_ARGS[@]}" >>"$LOG_FILE" 2>&1 &
    existing_daemon=$!
fi

if [[ -t 0 && -t 1 && "$MINIMIZED" == 0 && "$SHOW_GUI" == 0 ]]; then
    if [[ "$FORCE_WAYLAND_GUI" == 0 ]]; then
        stop_user_procs "src/gui.py"
    fi
    nohup "$PYTHON" "$WORKSPACE_DIR/src/gui.py" "${GUI_ARGS[@]}" >>"$LOG_FILE" 2>&1 &
    GUI_PID=$!
    echo "[PRESHADER] Daemon active (PID $existing_daemon) | Control Deck GUI launched (PID $GUI_PID)."
    echo "[PRESHADER] Streaming live daemon logs (Ctrl+C detaches terminal; use 'preshader --stop' to stop engine):"
    trap 'echo; exit 0' INT TERM HUP
    tail -n 20 -f "$LOG_FILE"
    exit 0
fi

if [[ "$MINIMIZED" == 1 ]]; then
    if find_user_procs "src/gui.py" | head -n 1 | grep -q .; then
        exit 0
    fi
    exec "$PYTHON" "$WORKSPACE_DIR/src/gui.py" "${GUI_ARGS[@]}"
fi

stop_user_procs "src/gui.py"
exec "$PYTHON" "$WORKSPACE_DIR/src/gui.py" "${GUI_ARGS[@]}"
