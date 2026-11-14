#!/usr/bin/env python3
import os
import sys
import time
import signal
import ctypes
import select
import struct
import argparse
import threading
import math
from collections.abc import Mapping

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from storage import BrainStorage
from pinner import AssetPinner
from engine import RecogEngine
from velocity import PredictiveVelocityTracker
from ram_governor import RamBudgetGovernor
from event_queue import AssetEventQueue
from steam_detector import SteamGameDetector
import config_ipc

IN_ACCESS = 0x00000001
IN_OPEN = 0x00000020
IN_Q_OVERFLOW = 0x00004000
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_IGNORED = 0x00008000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000


class InotifyMonitor:
    def __init__(self, asset_filter=None, max_asset_watches=100000):
        self.libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int

        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        self.wd_to_path = {}
        self.file_watch_descriptors = set()
        self.directory_watch_descriptors = set()
        self.asset_filter = asset_filter or os.path.isfile
        self.max_asset_watches = max_asset_watches
        self.watched_asset_count = 0
        self.kernel_overflows = 0

    def _add_asset_watch(self, filepath):
        if self.fd < 0 or self.watched_asset_count >= self.max_asset_watches:
            return False
        if not self.asset_filter(filepath):
            return False
        wd = self.libc.inotify_add_watch(
            self.fd, filepath.encode("utf-8", errors="ignore"), IN_OPEN | IN_ACCESS
        )
        if wd < 0:
            return False
        if wd not in self.file_watch_descriptors:
            self.watched_asset_count += 1
        self.wd_to_path[wd] = filepath
        self.file_watch_descriptors.add(wd)
        self.directory_watch_descriptors.discard(wd)
        return True

    def add_tree(self, root_path, max_dirs=2500):
        if self.fd < 0 or not os.path.isdir(root_path):
            return 0
        added = 0
        mask = IN_CREATE | IN_MOVED_TO
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            wd = self.libc.inotify_add_watch(self.fd, dirpath.encode("utf-8", errors="ignore"), mask)
            if wd >= 0:
                self.wd_to_path[wd] = dirpath
                if wd not in self.directory_watch_descriptors:
                    added += 1
                self.directory_watch_descriptors.add(wd)
                self.file_watch_descriptors.discard(wd)

            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if self.watched_asset_count >= self.max_asset_watches:
                    break
                self._add_asset_watch(filepath)

            if added >= max_dirs:
                break
        return added

    def read_events(self, timeout=0.20):
        if self.fd < 0:
            time.sleep(timeout)
            return []
        rlist, _, _ = select.select([self.fd], [], [], timeout)
        if not rlist:
            return []
        try:
            raw = os.read(self.fd, 65536)
        except OSError:
            return []

        events = []
        offset = 0
        header_size = 16
        while offset + header_size <= len(raw):
            wd, mask, cookie, name_len = struct.unpack_from("iIII", raw, offset)
            offset += header_size
            name_bytes = raw[offset:offset + name_len]
            offset += name_len
            if mask & IN_Q_OVERFLOW:
                self.kernel_overflows += 1
                continue
            name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="ignore")
            path = self.wd_to_path.get(wd)
            if mask & IN_IGNORED:
                self.wd_to_path.pop(wd, None)
                self.file_watch_descriptors.discard(wd)
                self.directory_watch_descriptors.discard(wd)
                continue

            if wd in self.file_watch_descriptors:
                if mask & (IN_OPEN | IN_ACCESS) and path:
                    events.append(path)
                continue

            if path and name and mask & (IN_CREATE | IN_MOVED_TO):
                full_path = os.path.join(path, name)
                if mask & IN_ISDIR or os.path.isdir(full_path):
                    self.add_tree(full_path)
                else:
                    self._add_asset_watch(full_path)
        return events

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.wd_to_path.clear()
        self.file_watch_descriptors.clear()
        self.directory_watch_descriptors.clear()


def prewarm_shaders(shader_dir, pinner):
    count = 0
    if not os.path.isdir(shader_dir):
        return 0
    for root, _, files in os.walk(shader_dir):
        for f in files:
            if f.endswith((".foz", ".dxvk-cache", ".bin")):
                if pinner.pin_file(os.path.join(root, f), combat_burst_mode=False):
                    count += 1
    return count


def read_standby_ram():
    avail_mb = 8192.0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_mb = int(line.split()[1]) / 1024.0
                    break
    except Exception:
        pass
    return round(avail_mb, 1)


def resolve_app_cache_cap(config, game_info):
    appid = game_info.get("appid")
    override = game_info.get("max_cache_mb")
    if override is None:
        app_caps = config.get("app_ram_caps_mb", {})
        if not isinstance(app_caps, Mapping):
            raise ValueError("app_ram_caps_mb must be an AppID-to-MB object")
        if appid is not None:
            override = app_caps.get(str(appid))
    if override is None:
        return None
    if isinstance(override, bool):
        raise ValueError(f"Invalid RAM cap for AppID {appid}: expected non-negative MB")
    try:
        cap_mb = float(override)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid RAM cap for AppID {appid}: expected non-negative MB"
        ) from exc
    if not math.isfinite(cap_mb) or cap_mb < 0:
        raise ValueError(f"Invalid RAM cap for AppID {appid}: expected non-negative MB")
    return cap_mb


def live_config_with_app_cache_cap(config, app_cache_cap_mb):
    effective_config = dict(config)
    effective_config.pop("app_cache_cap_mb", None)
    if app_cache_cap_mb is not None:
        effective_config["app_cache_cap_mb"] = app_cache_cap_mb
    return effective_config


def main():
    parser = argparse.ArgumentParser(description="RecogD Preshader Universal Daemon")
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--ext-mount", default="/mnt/preshader")
    args = parser.parse_args()

    cfg = config_ipc.init_live_config()
    last_flush_gen = cfg.get("flush_generation", 0)
    game_detector = SteamGameDetector(extra_mount=args.ext_mount)

    vault_root = os.path.realpath(os.path.expanduser(args.vault_root))
    cache_root = os.path.realpath(os.path.expanduser("~/.cache"))
    try:
        is_dual_bus = os.path.commonpath((vault_root, cache_root)) != cache_root
    except ValueError:
        is_dual_bus = True
    vault_label = f"Dual-Bus SSD ({args.ext_mount})" if is_dual_bus else "NVMe Fallback (~/.cache)"

    games = game_detector.manifests
    print("==========================================================")
    print("   PRESHADER UNIVERSAL DAEMON ACTIVE (Ctrl+C to stop)     ")
    print("==========================================================")
    print(f"[PRESHADER] Vault Mode:       {vault_label}")
    print(f"[PRESHADER] OOM Protection:   16MB Max File Cap + 4500MB Free RAM Guard")
    print(f"[PRESHADER] Spike Shield:     ACTIVE (Dynamic I/O Rate-Limiter)")
    print(f"[PRESHADER] Steam Libraries:  {len(game_detector.libraries)} discovered | {len(games)} games indexed")
    print("[PRESHADER] Standby: Waiting for you to launch any Steam game...")

    running = True

    def sig_handler(signum, frame):
        nonlocal running
        running = False
        print("\n[PRESHADER] Shutting down cleanly...")

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGHUP, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    while running:
        cfg = config_ipc.read_live_config()
        if cfg.get("shutdown_requested", False):
            running = False
            break

        avail_mb = read_standby_ram()
        config_ipc.write_live_state({
            "daemon_online": True,
            "status_text": "PAUSED" if cfg.get("pause_engine") else "STANDBY (WAITING FOR GAME)",
            "vault_mode": vault_label,
            "game_name": "Waiting for Steam game...",
            "appid": "-",
            "velocity": 0.0,
            "hop_depth": 1,
            "combat_burst": bool(cfg.get("force_combat_shield", False)),
            "active_ram_mb": 0.0,
            "budget_ram_mb": float(cfg.get("max_cache_mb", 1536)),
            "avail_ram_mb": avail_mb,
            "last_pinned_file": "",
            "events_received": 0,
            "events_processed": 0,
            "events_coalesced": 0,
            "events_dropped": 0,
            "event_processing_errors": 0,
            "event_queue_depth": 0,
            "inotify_overflows": 0
        })

        detected_game = game_detector.detect_running_game()
        active_game = None
        if detected_game:
            active_game = {
                **detected_game,
                "install_dir": detected_game.get("install_dir", detected_game.get("game_dir", "")),
                "workshop_dir": detected_game.get("workshop_dir", ""),
                "shader_dir": detected_game.get(
                    "shader_dir", detected_game.get("shadercache_dir", "")
                )
            }
        if not active_game:
            time.sleep(1.0)
            continue

        print("\n==========================================================")
        print(f"[PRESHADER] >>> GAME DETECTED: {active_game['name']} (AppID: {active_game['appid']}) <<<")
        print("==========================================================")

        engine_config = config_ipc.read_persistent_config()
        engine_config.update(cfg)
        try:
            app_cache_cap_mb = resolve_app_cache_cap(engine_config, active_game)
        except ValueError as exc:
            print(f"[PRESHADER-CONFIG] {exc}; using the global RAM cap.")
            app_cache_cap_mb = None
        if app_cache_cap_mb is not None:
            print(
                f"[PRESHADER-CONFIG] AppID {active_game['appid']} RAM cap: "
                f"{app_cache_cap_mb:g} MB"
            )
        engine_cfg = engine_config.get("engine", {})
        drive_cfg = engine_config.get("external_drive", {})
        storage = BrainStorage(
            vault_root,
            active_game,
            external_mount=args.ext_mount,
            max_parent_nodes=int(engine_cfg.get("max_parent_nodes", 12000)),
            enable_striping=bool(drive_cfg.get("enable_cross_bus_striping", True)),
            max_stripe_file_bytes=int(drive_cfg.get("max_stripe_file_bytes", 8388608)),
            require_external_mount=is_dual_bus
        )
        ram_governor = RamBudgetGovernor(config=engine_config)
        pinner = AssetPinner(
            storage,
            max_file_mb=16,
            config=engine_config,
            ram_governor=ram_governor
        )
        velocity = PredictiveVelocityTracker(
            config=engine_config,
            enable_evdev=bool(engine_config.get("velocity", {}).get("enable_evdev", False))
        )
        engine = RecogEngine(
            engine_config,
            pinner,
            storage,
            velocity_tracker=velocity,
            ram_governor=ram_governor
        )
        engine.apply_live_config(live_config_with_app_cache_cap(cfg, app_cache_cap_mb))
        graph = engine.graph
        engine_lock = threading.Lock()

        print(f"[PRESHADER-STORAGE] Loaded '{active_game['name']}' brain: {len(graph)} triggers, {len(storage.stripe_map)} striped assets.")

        warmed = prewarm_shaders(active_game["shader_dir"], pinner)
        print(f"[PRESHADER] Pre-warmed {warmed} Vulkan/DXVK shader files into RAM")

        monitor = InotifyMonitor(asset_filter=engine.is_valid_target)
        watch_dirs = 0
        for p in [active_game["install_dir"], active_game["workshop_dir"], active_game["shader_dir"], storage.shader_cache_dir]:
            if os.path.isdir(p):
                print(f"[PRESHADER] Watching Game Path: {p}")
                watch_dirs += monitor.add_tree(p)

        print(
            f"[PRESHADER] Kernel Inotify Attached: {watch_dirs} directories and "
            f"{monitor.watched_asset_count} asset files monitored."
        )

        boost_count = 0
        last_save = time.monotonic()
        last_proc_check = time.monotonic()
        last_ipc_sync = 0.0
        last_external_mount_available = storage.is_external_mount_available()

        def process_asset_event(fpath):
            nonlocal boost_count
            with engine_lock:
                profile = engine.process_event(fpath)
                current_vel = profile.get("event_rate", 0.0)
                hop_depth = profile.get("lookahead_hops", 1)
                combat_burst = profile.get("combat_burst", False)
                storage.update_live_velocity(current_vel)
                if engine.boost_triggers_fired > boost_count:
                    boost_count += 1
                    if boost_count <= 20 or boost_count % 25 == 0:
                        stats = pinner.get_stats()
                        mode_tag = "SHIELD" if combat_burst else f"Hops:{hop_depth}"
                        print(
                            f"[BOOST #{boost_count}] {os.path.basename(fpath)} -> "
                            f"{engine.last_pinned_file} ({mode_tag} | Vel:{current_vel:.1f}/s | "
                            f"Cache:{stats['active_ram_pinned_mb']}MB | SysFree:{stats['system_avail_ram_mb']}MB)"
                        )

        event_queue = AssetEventQueue(process_asset_event, max_pending=2048)

        while running:
            now_mono = time.monotonic()

            # 1-Second Proactive RAM Safety & GUI Sync Loop
            if (now_mono - last_ipc_sync) >= 0.5:
                cfg = config_ipc.read_live_config()
                if cfg.get("shutdown_requested", False):
                    running = False
                    break

                with engine_lock:
                    engine.apply_live_config(
                        live_config_with_app_cache_cap(cfg, app_cache_cap_mb)
                    )

                storage.sensor.update_limits(
                    cfg.get("min_free_ram_mb", 4500),
                    cfg.get("shield_velocity_threshold", 18.0),
                    cfg.get("max_write_mbps", 6.0),
                    cfg.get("force_combat_shield", False)
                )

                if cfg.get("flush_generation", 0) > last_flush_gen:
                    last_flush_gen = cfg["flush_generation"]
                    with engine_lock:
                        flushed = pinner.flush_all_pins()
                    print(f"[GUI-COMMAND] Purged {flushed} pinned files from RAM via DONTNEED!")

                with engine_lock:
                    current_vel = velocity.get_asset_event_rate(now_mono)
                    combat_burst = (
                        bool(cfg.get("force_combat_shield", False))
                        or current_vel >= float(cfg.get("shield_velocity_threshold", 18.0))
                    )
                    hop_depth = engine.last_profile.get("lookahead_hops", 1)
                    storage.update_live_velocity(current_vel)
                    pstats = engine.get_runtime_stats()
                    last_pinned_file = engine.last_pinned_file
                event_stats = event_queue.get_stats()
                external_mount_available = storage.is_external_mount_available()
                if external_mount_available != last_external_mount_available:
                    state = "reconnected" if external_mount_available else "disconnected"
                    print(f"[PRESHADER-STORAGE] External SSD {state}; vault writes are guarded.")
                    last_external_mount_available = external_mount_available

                if cfg.get("pause_engine"):
                    st_text = "PAUSED BY USER"
                elif storage.requires_external_mount and not external_mount_available:
                    st_text = "SSD DISCONNECTED - VAULT WRITES PAUSED"
                elif combat_burst:
                    st_text = "COMBAT SHIELD ACTIVE (FPS PROTECTED)"
                elif event_stats.get("events_processed", 0) == 0:
                    st_text = "ACTIVE - WAITING FOR ASSET EVENTS"
                elif engine.boost_triggers_fired == 0:
                    st_text = "LEARNING ASSET CO-ACCESS"
                else:
                    st_text = "ACTIVE & PRE-PINNING"

                config_ipc.write_live_state({
                    "daemon_online": True,
                    "status_text": st_text,
                    "vault_mode": vault_label,
                    "vault_online": external_mount_available,
                    "game_name": active_game["name"],
                    "appid": active_game["appid"],
                    "velocity": current_vel,
                    "hop_depth": hop_depth,
                    "combat_burst": combat_burst,
                    "active_ram_mb": pstats["active_ram_pinned_mb"],
                    "budget_ram_mb": pstats["ram_budget_ceiling_mb"],
                    "avail_ram_mb": pstats["system_avail_ram_mb"],
                    "nvme_files": pstats["nvme_files"],
                    "nvme_mb": pstats["nvme_mb"],
                    "sata_files": pstats["sata_files"],
                    "sata_mb": pstats["sata_mb"],
                    "striped_count": len(storage.stripe_map),
                    "evicted_files": pstats["governor_evicted_files"],
                    "evicted_mb": pstats["governor_evicted_mb"],
                    "shed_writes": storage.dropped_burst_writes,
                    "io_stall_pct": round(storage.sensor.io_stall_avg10, 2),
                    "last_pinned_file": last_pinned_file,
                    "inotify_overflows": monitor.kernel_overflows,
                    **event_stats
                })
                last_ipc_sync = now_mono

            if (now_mono - last_proc_check) > 3.0:
                if not game_detector.detect_running_game(active_game["appid"]):
                    break
                last_proc_check = now_mono

            events = monitor.read_events(timeout=0.20)
            if events:
                tracked_batch = []
                for fpath in events:
                    if not engine.is_valid_target(fpath):
                        continue
                    tracked_batch.append(fpath)
                event_queue.submit_batch(tracked_batch)

            if (now_mono - last_save) > 90.0 and storage.is_safe_to_autosave():
                with engine_lock:
                    stats = engine.get_runtime_stats()
                    stats["peak_velocity_score"] = velocity.get_stats()["peak_velocity_score"]
                    stats["max_hop_depth_used"] = engine.max_hop_depth
                    storage.save(graph, stats)
                last_save = now_mono

        monitor.close()
        event_queue.stop()
        print(f"\n[PRESHADER] Game '{active_game['name']}' closed. Saving brain to disk...")
        saved = engine.shutdown()
        if saved:
            print(f"[PRESHADER-STORAGE] Saved '{active_game['name']}' brain ({len(graph)} nodes, {len(storage.stripe_map)} striped) -> {storage.db_path}")
        else:
            print("[PRESHADER-STORAGE] Brain save skipped or failed; check SSD mount and storage errors.")
        print("[PRESHADER] Standby: Waiting for next Steam game launch...")

    config_ipc.write_live_state({"daemon_online": False, "status_text": "OFFLINE"})
    print("[PRESHADER] All game brains saved. Goodbye!")


if __name__ == "__main__":
    main()
