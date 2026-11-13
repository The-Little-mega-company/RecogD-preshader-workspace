#!/usr/bin/env python3
import os
import json
import time

WORKSPACE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSISTENT_CONFIG_PATH = os.path.join(WORKSPACE_DIR, "config.json")
LIVE_CONFIG_PATH = f"/dev/shm/recogd_config_{os.getuid()}.json"
LIVE_STATE_PATH = f"/dev/shm/recogd_state_{os.getuid()}.json"

DEFAULT_CONFIG = {
    "max_cache_mb": 1536,
    "min_free_ram_mb": 4500,
    "max_hop_depth": 2,
    "shield_velocity_threshold": 18.0,
    "max_write_mbps": 6.0,
    "force_combat_shield": False,
    "pause_engine": False,
    "flush_generation": 0,
    "shutdown_requested": False
}

DEFAULT_STATE = {
    "daemon_online": False,
    "updated_at": 0.0,
    "status_text": "STANDBY",
    "vault_mode": "Checking...",
    "game_name": "Waiting for Steam game...",
    "appid": "-",
    "velocity": 0.0,
    "hop_depth": 1,
    "combat_burst": False,
    "active_ram_mb": 0.0,
    "budget_ram_mb": 1536.0,
    "avail_ram_mb": 8192.0,
    "nvme_files": 0,
    "nvme_mb": 0.0,
    "sata_files": 0,
    "sata_mb": 0.0,
    "striped_count": 0,
    "evicted_files": 0,
    "evicted_mb": 0.0,
    "shed_writes": 0,
    "io_stall_pct": 0.0
}


def _atomic_write_json(path, data):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _read_json(path, fallback):
    if not os.path.exists(path):
        return dict(fallback)
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        merged = dict(fallback)
        if isinstance(loaded, dict):
            merged.update(loaded)
        return merged
    except Exception:
        return dict(fallback)


def init_live_config():
    saved = _read_json(PERSISTENT_CONFIG_PATH, DEFAULT_CONFIG)
    saved["force_combat_shield"] = False
    saved["pause_engine"] = False
    saved["shutdown_requested"] = False
    _atomic_write_json(LIVE_CONFIG_PATH, saved)
    return saved


def read_live_config():
    if not os.path.exists(LIVE_CONFIG_PATH):
        return init_live_config()
    return _read_json(LIVE_CONFIG_PATH, DEFAULT_CONFIG)


def update_live_config(updates, save_persistent=True):
    cfg = read_live_config()
    cfg.update(updates)
    _atomic_write_json(LIVE_CONFIG_PATH, cfg)
    if save_persistent:
        persistent_copy = dict(cfg)
        persistent_copy["force_combat_shield"] = False
        persistent_copy["pause_engine"] = False
        persistent_copy["shutdown_requested"] = False
        _atomic_write_json(PERSISTENT_CONFIG_PATH, persistent_copy)
    return cfg


def write_live_state(state_updates):
    state = _read_json(LIVE_STATE_PATH, DEFAULT_STATE)
    state.update(state_updates)
    state["updated_at"] = time.time()
    _atomic_write_json(LIVE_STATE_PATH, state)


def read_live_state():
    state = _read_json(LIVE_STATE_PATH, DEFAULT_STATE)
    if (time.time() - state.get("updated_at", 0.0)) > 4.0:
        state["daemon_online"] = False
        state["status_text"] = "DAEMON OFFLINE"
    return state
