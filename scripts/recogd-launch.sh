#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
DRIVE_LABEL="preshader"
DRIVE_MOUNT="/mnt/preshader"
LOCAL_USER="${USER:-$(id -un)}"
FALLBACK_VAULT="$HOME/.cache/recogd-fallback"
RUN_GUI=1
MINIMIZED=0
TERMINAL_MODE=0
RUN_BACKUP=0
DAEMON_ARGS=()

while (($#)); do
    case "$1" in
        --stats|stats)
            exec "$PYTHON" "$WORKSPACE_DIR/src/cli_stats.py"
            ;;
        --bench|bench)
            exec "$PYTHON" "$WORKSPACE_DIR/src/benchmark.py"
            ;;
        --backup|backup)
            RUN_BACKUP=1
            shift
            ;;
        --nogui)
            RUN_GUI=0
            shift
            ;;
        --gui|gui|--show-gui)
            RUN_GUI=1
            MINIMIZED=0
            shift
            ;;
        --minimized)
            RUN_GUI=1
            MINIMIZED=1
            shift
            ;;
        *)
            DAEMON_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ -t 0 && -t 1 ]]; then
    TERMINAL_MODE=1
fi

find_external_mount() {
    local candidate device
    local candidates=(
        "/mnt/$DRIVE_LABEL"
        "/run/media/$LOCAL_USER/$DRIVE_LABEL"
        "/media/$LOCAL_USER/$DRIVE_LABEL"
    )

    for candidate in "${candidates[@]}"; do
        if [[ -d "$candidate" && -w "$candidate" ]] && mountpoint -q "$candidate"; then
            DRIVE_MOUNT="$(realpath "$candidate")"
            return 0
        fi
    done

    if [[ -e "/dev/disk/by-label/$DRIVE_LABEL" ]] && command -v udisksctl >/dev/null 2>&1; then
        device="$(realpath "/dev/disk/by-label/$DRIVE_LABEL")"
        udisksctl mount -b "$device" --options noatime >/dev/null 2>&1 || true
        for candidate in "${candidates[@]}"; do
            if [[ -d "$candidate" && -w "$candidate" ]] && mountpoint -q "$candidate"; then
                DRIVE_MOUNT="$(realpath "$candidate")"
                return 0
            fi
        done
    fi

    return 1
}

if [[ "$RUN_BACKUP" == 1 ]]; then
    if find_external_mount; then
        VAULT_DIR="$DRIVE_MOUNT/recogd-vault"
    else
        VAULT_DIR="$FALLBACK_VAULT"
    fi
    exec "$PYTHON" "$WORKSPACE_DIR/src/backup.py" --vault-root "$VAULT_DIR"
fi

mkdir -p "$FALLBACK_VAULT"
FALLBACK_VAULT="$(realpath "$FALLBACK_VAULT")"
if find_external_mount; then
    VAULT_DIR="$DRIVE_MOUNT/recogd-vault"
    if ! mkdir -p "$VAULT_DIR/games" "$VAULT_DIR/global_shader_cache"; then
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

if [[ "$RUN_GUI" == 1 && -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
    printf '[RECOGD] No graphical session detected; starting daemon without the GUI.\n' >&2
    RUN_GUI=0
fi

find_running_pid() {
    local script_path="$1"
    local pid
    while IFS= read -r pid; do
        if [[ -r "/proc/$pid/cmdline" ]] &&
            tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq "$script_path"; then
            printf '%s\n' "$pid"
            return 0
        fi
    done < <(pgrep -u "$(id -u)" -f "$script_path" || true)
    return 1
}

request_daemon_shutdown() {
    PYTHONPATH="$WORKSPACE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON" -c 'import config_ipc; config_ipc.update_live_config({"shutdown_requested": True}, save_persistent=False)'
}

GUI_ARGS=()
if [[ "$MINIMIZED" == 1 ]]; then
    GUI_ARGS+=(--minimized)
fi

if existing_daemon="$(find_running_pid "$WORKSPACE_DIR/src/daemon.py")"; then
    GUI_PID=""
    existing_gui=""
    if [[ "$RUN_GUI" == 1 ]]; then
        if existing_gui="$(find_running_pid "$WORKSPACE_DIR/src/gui.py")"; then
            PYTHONPATH="$WORKSPACE_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
                "$PYTHON" -c 'import config_ipc; config_ipc.request_gui_show()'
            printf '[RECOGD] Bringing the existing control deck to the foreground (PID %s).\n' \
                "$existing_gui"
            GUI_PID="$existing_gui"
        else
            "$PYTHON" "$WORKSPACE_DIR/src/gui.py" "${GUI_ARGS[@]}" &
            GUI_PID=$!
        fi
    fi

    if [[ "$TERMINAL_MODE" == 1 ]]; then
        DAEMON_PID="$existing_daemon"
        terminal_cleanup() {
            local status=$?
            trap - EXIT INT HUP TERM
            if kill -0 "$DAEMON_PID" 2>/dev/null; then
                printf '\n[RECOGD] Stopping daemon and control deck...\n'
                request_daemon_shutdown || true
                while kill -0 "$DAEMON_PID" 2>/dev/null; do
                    sleep 0.2
                done
            fi
            if [[ -n "$GUI_PID" ]] && kill -0 "$GUI_PID" 2>/dev/null; then
                kill "$GUI_PID" 2>/dev/null || true
                wait "$GUI_PID" 2>/dev/null || true
            fi
            exit "$status"
        }
        trap terminal_cleanup EXIT
        trap 'exit 130' INT
        trap 'exit 129' HUP
        trap 'exit 143' TERM
        printf '[RECOGD] Attached to the running daemon. Ctrl+C or closing this terminal stops RecogD.\n'
        while kill -0 "$DAEMON_PID" 2>/dev/null; do
            sleep 0.2
        done
    else
        if [[ "$RUN_GUI" == 1 && -z "$existing_gui" ]]; then
            wait "$GUI_PID"
        fi
        if [[ "$RUN_GUI" == 0 ]]; then
            printf '[RECOGD] Daemon is already running (PID %s).\n' "$existing_daemon"
        fi
    fi
    exit 0
fi

DAEMON_COMMAND=(
    "$PYTHON" "$WORKSPACE_DIR/src/daemon.py"
    --vault-root "$VAULT_DIR"
    --ext-mount "$DRIVE_MOUNT"
    "${DAEMON_ARGS[@]}"
)

if [[ "$RUN_GUI" == 0 ]]; then
    exec "${DAEMON_COMMAND[@]}"
fi

DAEMON_PID=""
GUI_PID=""
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if [[ -n "$GUI_PID" ]] && kill -0 "$GUI_PID" 2>/dev/null; then
        kill "$GUI_PID" 2>/dev/null || true
        wait "$GUI_PID" 2>/dev/null || true
    fi
    if [[ -n "$DAEMON_PID" ]] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 129' HUP
trap 'exit 143' TERM

"${DAEMON_COMMAND[@]}" &
DAEMON_PID=$!
"$PYTHON" "$WORKSPACE_DIR/src/gui.py" "${GUI_ARGS[@]}" &
GUI_PID=$!

wait "$DAEMON_PID"
