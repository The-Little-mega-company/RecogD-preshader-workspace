#!/usr/bin/env python3
import os
import sys
import json
import time
import signal
import struct
import select
import ctypes
import ctypes.util
import argparse
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinner import RamPinner
from storage import BrainStorage
from engine import RecogEngine
from steam_detector import SteamGameDetector
from telemetry import RuntimeTelemetry
from velocity import PredictiveVelocityTracker
from ram_governor import RamBudgetGovernor
from vault_merger import VaultCacheMerger, detect_preshader_mounts
from disk_health import DiskHealthChecker

IN_OPEN = 0x00000020
IN_CREATE = 0x00000100
IN_ISDIR = 0x40000000
EVENT_HEADER = struct.Struct("iIII")


class KernelInotifyWatcher:
    def __init__(self, root_paths):
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        self.libc = ctypes.CDLL(libc_name, use_errno=True)

        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int

        self.libc.inotify_add_watch.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32
        ]
        self.libc.inotify_add_watch.restype = ctypes.c_int

        self.fd = self.libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
        if self.fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"inotify_init1 failed: {os.strerror(err)}")

        self.wd_to_path = {}
        for path in root_paths:
            self._register_tree(path)

    def _add_single_watch(self, dir_path):
        mask = IN_OPEN | IN_CREATE
        wd = self.libc.inotify_add_watch(
            self.fd,
            dir_path.encode("utf-8", errors="surrogateescape"),
            mask
        )
        if wd >= 0:
            self.wd_to_path[wd] = dir_path

    def _register_tree(self, root_path):
        if not root_path or not os.path.isdir(root_path):
            return
        self._add_single_watch(root_path)
        for current_root, dirs, _ in os.walk(root_path):
            for d in dirs:
                full_dir = os.path.join(current_root, d)
                self._add_single_watch(full_dir)

    def read_events(self, timeout=0.25):
        if self.fd < 0:
            return []
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []

        try:
            raw_buf = os.read(self.fd, 65536)
        except BlockingIOError:
            return []

        events = []
        offset = 0
        buf_len = len(raw_buf)

        while offset + EVENT_HEADER.size <= buf_len:
            wd, mask, _, name_len = EVENT_HEADER.unpack_from(raw_buf, offset)
            offset += EVENT_HEADER.size

            name_bytes = raw_buf[offset : offset + name_len]
            offset += name_len

            name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="replace")
            if not name:
                continue

            parent_dir = self.wd_to_path.get(wd)
            if not parent_dir:
                continue

            full_path = os.path.join(parent_dir, name)

            if mask & IN_ISDIR:
                if mask & IN_CREATE:
                    self._register_tree(full_path)
                continue

            if mask & IN_OPEN:
                events.append(full_path)

        return events

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1


def load_config(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


class InProcessPreshaderDaemon:
    """
    Unified Preshader Daemon controller that runs either in-process alongside the
    Control Deck GUI (single-process mode) or standalone in CLI mode.
    Connects both /run/media/$USER/preshader/recogd-vault and ~/.cache/recogd-fallback.
    """

    def __init__(
        self,
        config_path=None,
        vault_root=None,
        ext_mount=None,
        profile=None,
        telemetry_path=None,
        log_callback=None
    ):
        self.config_path = os.path.abspath(
            os.path.expanduser(
                config_path or os.path.join(os.path.dirname(__file__), "..", "config.json")
            )
        )
        self.config = load_config(self.config_path)

        detected_mount, detected_ext_vault, detected_nvme_vault = detect_preshader_mounts()
        self.ext_mount = ext_mount or detected_mount
        self.merger = VaultCacheMerger(
            ext_vault=(vault_root if vault_root and "preshader" in vault_root else detected_ext_vault),
            nvme_vault=detected_nvme_vault
        )
        self.vault_root = vault_root or self.merger.primary_vault_root()
        self.profile = profile
        self.log_callback = log_callback

        self.telemetry = RuntimeTelemetry(
            telemetry_path or "~/.cache/recogd/runtime.json"
        )
        self.ram_governor = RamBudgetGovernor(config=self.config)
        self.velocity_tracker = PredictiveVelocityTracker(config=self.config, enable_evdev=True)

        eng_cfg = self.config.get("engine", {})
        self.pinner = RamPinner(
            cooldown_sec=eng_cfg.get("ram_pin_cooldown_seconds", 5.0),
            max_size_bytes=eng_cfg.get("max_file_size_bytes", 16777216),
            external_mount=self.ext_mount,
            preload_callback=self.telemetry.record_preload,
            ram_governor=self.ram_governor
        )
        self.detector = SteamGameDetector(extra_mount=self.ext_mount)
        self.disk_health_checker = DiskHealthChecker(
            ext_mount=self.ext_mount,
            vault_paths=[self.merger.ext_vault, self.merger.nvme_vault],
            log_callback=self.log
        )
        self.last_disk_health = self.telemetry.data.get("disk_health")

        self.active_game = None
        self.active_engine = None
        self.active_watcher = None
        self.paused = False
        self.combat_shield = False
        self.running = False
        self.thread = None
        self.lock = threading.Lock()

        try:
            self.last_config_mtime = os.path.getmtime(self.config_path)
        except OSError:
            self.last_config_mtime = 0.0

    def log(self, message):
        print(message, flush=True)
        if self.log_callback is not None:
            try:
                self.log_callback(message)
            except Exception:
                pass

    def start_background(self):
        with self.lock:
            if self.running and self.thread and self.thread.is_alive():
                return
            self.running = True
            self.thread = threading.Thread(
                target=self.run_loop,
                name="PreshaderUnifiedDaemon",
                daemon=True
            )
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive() and threading.current_thread() != self.thread:
            self.thread.join(timeout=3.0)

    def reload_config(self, new_config=None):
        if new_config is not None:
            self.config = new_config
        else:
            self.config = load_config(self.config_path)

        eng_cfg = self.config.get("engine", {})
        ext_cfg = self.config.get("external_drive", {})
        self.pinner.cooldown_sec = float(eng_cfg.get("ram_pin_cooldown_seconds", 5.0))
        self.pinner.max_size_bytes = int(eng_cfg.get("max_file_size_bytes", 16777216))
        self.velocity_tracker.apply_config(self.config)
        self.ram_governor.apply_config(self.config)
        if self.combat_shield:
            self.velocity_tracker.max_lookahead_hops = 1
        if self.active_engine is not None:
            self.active_engine.apply_config(self.config)
            self.active_engine.storage.enable_striping = (
                bool(ext_cfg.get("enable_cross_bus_striping", True))
                and not self.combat_shield
                and self.active_engine.storage.external_online
            )
            self.active_engine.storage.max_stripe_file_bytes = int(
                ext_cfg.get("max_stripe_file_bytes", 8388608)
            )
        self.telemetry.update_stats(self.get_combined_stats())

    def set_paused(self, paused):
        self.paused = bool(paused)
        state_msg = (
            "Engine paused by user"
            if self.paused
            else f"Active · Watching ({self.active_game['name'] if self.active_game else 'Autonomous Steam Standby'})"
        )
        self.telemetry.set_status("paused" if self.paused else "running", self.profile or "autonomous", state_msg)
        self.telemetry.update_stats(self.get_combined_stats())
        self.log(f"[PRESHADER] Pre-pinning engine {'PAUSED' if self.paused else 'RESUMED'}.")

    def set_combat_shield(self, enabled):
        self.combat_shield = bool(enabled)
        if self.combat_shield:
            self.velocity_tracker.max_lookahead_hops = 1
            if self.active_engine is not None:
                self.active_engine.storage.enable_striping = False
        else:
            vel_cfg = self.config.get("velocity", {})
            self.velocity_tracker.max_lookahead_hops = int(vel_cfg.get("max_lookahead_hops", 2))
            if self.active_engine is not None:
                ext_cfg = self.config.get("external_drive", {})
                self.active_engine.storage.enable_striping = (
                    bool(ext_cfg.get("enable_cross_bus_striping", True))
                    and self.active_engine.storage.external_online
                )
        self.telemetry.update_stats(self.get_combined_stats())
        self.log(
            f"[SPIKE-SHIELD] Combat Spike Shield {'LOCKED ON (1-Hop + Write Shedding)' if self.combat_shield else 'OFF (Auto Adaptive)'}."
        )

    def purge_ram_cache(self):
        ev_files, ev_bytes = self.pinner.trim_to_budget(0)
        self.telemetry.update_stats(self.get_combined_stats())
        self.log(
            f"[RAM-GOVERNOR] Released {ev_files} pre-pinned files "
            f"({ev_bytes / (1024 * 1024):.2f} MB) from kernel page cache via POSIX_FADV_DONTNEED."
        )
        return ev_files, ev_bytes

    def prewarm_caches(self, safe_id=None):
        self.merger.sync_and_merge_vaults()
        files_pinned, bytes_queued = self.merger.prewarm_precompiled_caches(
            self.pinner, safe_id=safe_id, bypass_cooldown=True
        )
        target_label = safe_id if safe_id else "all unified Preshader vaults"
        self.log(
            f"[PRESHADER-CACHE] Pre-warmed {files_pinned} precompiled cache files "
            f"({bytes_queued / (1024 * 1024):.2f} MB) from {target_label} into RAM."
        )
        self.telemetry.update_stats(self.get_combined_stats())
        return files_pinned, bytes_queued

    def simulate_corner_burst(self, safe_id=None):
        self.velocity_tracker.record_input_sample(
            mouse_delta=2400.0, key_weight=4.0, stick_deflection=0.98
        )
        self.velocity_tracker.record_corner_boost(1)
        files_pinned, bytes_queued = self.merger.prewarm_precompiled_caches(
            self.pinner, safe_id=safe_id, bypass_cooldown=True
        )
        prof = self.velocity_tracker.get_adaptive_profile(
            self.config.get("engine", {}).get("max_preload_children", 35),
            self.config.get("engine", {}).get("time_window_seconds", 0.75),
            self.config.get("engine", {}).get("min_confidence_hits", 2)
        )
        self.telemetry.update_stats(self.get_combined_stats())
        self.log(
            f"[VELOCITY-BURST] Corner Lookahead Burst -> Velocity: {prof['velocity']:.2f} "
            f"({prof['state']}, {prof['multiplier']}x) | Pre-pinned {files_pinned} cache files "
            f"({bytes_queued / (1024 * 1024):.2f} MB)"
        )
        return prof, files_pinned

    def save_brains(self):
        if self.active_engine is not None:
            try:
                self.active_engine.storage.save(self.active_engine.graph, self.active_engine.get_runtime_stats())
            except Exception:
                pass
        self.merger.sync_and_merge_vaults()

    def run_disk_health_check(self, repair=False, target_path=None):
        report = self.disk_health_checker.run_check(repair=repair, target_path=target_path)
        self.last_disk_health = report
        if repair and report.get("vault_integrity", {}).get("repaired_files"):
            try:
                self.merger.sync_and_merge_vaults()
                self.log("[DISK-HEALTH] Re-synced healthy vault copies after repair.")
            except Exception as err:
                self.log(f"[DISK-HEALTH] Post-repair vault re-sync warning: {err}")
        self.telemetry.record_disk_health(report)
        self.telemetry.update_stats(self.get_combined_stats())
        return report

    def get_combined_stats(self):
        if self.active_engine is not None:
            st = self.active_engine.get_runtime_stats()
        else:
            st = dict(self.pinner.stats)
            st.update(self.velocity_tracker.get_stats())
            st.update(self.ram_governor.get_stats())
        st["paused"] = self.paused
        st["combat_shield"] = self.combat_shield
        if isinstance(self.last_disk_health, dict):
            st["disk_health_status"] = self.last_disk_health.get("status", "UNKNOWN")
            st["disk_health_checked_at"] = self.last_disk_health.get("timestamp", "")
        if self.active_game:
            st["active_game_name"] = self.active_game.get("name", "")
            st["active_game_appid"] = self.active_game.get("appid", "")
        return st

    def run_loop(self):
        eng_cfg = self.config.get("engine", {})
        ext_cfg = self.config.get("external_drive", {})
        scan_interval = float(eng_cfg.get("process_scan_interval_seconds", 2.0))

        # 1. Connect and merge both vault folders (/run/media/kessia/preshader/recogd-vault + ~/.cache/recogd-fallback)
        merged_brains = self.merger.sync_and_merge_vaults()
        gov_snap = self.ram_governor.evaluate(force=True)

        if os.path.isdir(self.ext_mount):
            self.log(f"[PRESHADER] Dual-Bus Mode ONLINE: Internal NVMe + External Ext4 SSD ({self.ext_mount})")
        else:
            self.log(f"[PRESHADER] External 'preshader' partition not detected. Using Internal NVMe Fallback ({self.vault_root})")

        self.log("==========================================================")
        self.log("   PRESHADER UNIVERSAL DAEMON ACTIVE (Ctrl+C to stop)     ")
        self.log("==========================================================")
        self.log(f"[PRESHADER] Vault Root:       {self.vault_root}")
        self.log(f"[PRESHADER] Fallback Vault:   {self.merger.nvme_vault} ({merged_brains} brains synced)")
        self.log(f"[PRESHADER] Steam Libraries:  {len(self.detector.libraries)} discovered")
        self.log(f"[PRESHADER] Installed Games:  {len(self.detector.manifests)} indexed")
        self.log(
            f"[PRESHADER] RAM Governor:     {gov_snap['state']} "
            f"(Budget: {gov_snap['dynamic_budget_bytes'] // (1024 * 1024)} MB, "
            f"Avail: {gov_snap['mem_available_bytes'] // (1024 * 1024)} MB)"
        )
        if not self.profile:
            self.log("[PRESHADER] Standby: Waiting for you to launch any Steam game...")
            self.telemetry.set_status("running", "autonomous", "Unified Engine Online · Precompiled caches warm")
            self.telemetry.update_stats(self.get_combined_stats())

            # 2. Pre-warm precompiled caches from /run/media/$USER/preshader/recogd-vault on startup
            warmed_files, warmed_bytes = self.merger.prewarm_precompiled_caches(
                self.pinner, bypass_cooldown=True
            )
            if warmed_files > 0:
                self.log(
                    f"[PRESHADER] Pre-warmed {warmed_files} precompiled cache assets "
                    f"({warmed_bytes / (1024 * 1024):.2f} MB) from {self.ext_mount} into RAM"
                )
        else:
            profiles = self.config.get("profiles", {})
            if self.profile not in profiles:
                self.telemetry.set_status("error", self.profile, f"Unknown profile: {self.profile}")
                self.running = False
                return

            prof = profiles[self.profile]
            watch_dirs = [
                os.path.expanduser(d)
                for d in prof.get("watch_directories", [])
                if d and os.path.isdir(os.path.expanduser(d))
            ]
            if not watch_dirs:
                self.telemetry.set_status("error", self.profile, "No valid watch directories found.")
                self.running = False
                return

            self.active_game = {
                "appid": self.profile,
                "name": prof.get("name", self.profile),
                "safe_id": f"profile_{self.profile}"
            }
            vault_base = os.path.dirname(os.path.abspath(os.path.expanduser(
                prof.get("brain_db_path", os.path.join(self.vault_root, "brain.json"))
            )))
            storage = BrainStorage(
                vault_root=vault_base,
                game_info=self.active_game,
                external_mount=ext_cfg.get("mount_point", self.ext_mount),
                max_parent_nodes=eng_cfg.get("max_parent_nodes", 60000),
                enable_striping=ext_cfg.get("enable_cross_bus_striping", True),
                max_stripe_file_bytes=ext_cfg.get("max_stripe_file_bytes", 8388608)
            )
            self.active_engine = RecogEngine(
                self.config,
                self.pinner,
                storage,
                velocity_tracker=self.velocity_tracker,
                ram_governor=self.ram_governor
            )
            self.active_watcher = KernelInotifyWatcher(watch_dirs)
            self.telemetry.set_status("running", self.profile, f"Watching {len(watch_dirs)} directories.")
            self.telemetry.update_stats(self.get_combined_stats())

        last_scan = 0.0
        last_gov_check = 0.0

        try:
            while self.running:
                now = time.monotonic()

                self.velocity_tracker.poll_hardware_input(now=now)

                if (now - last_gov_check) >= self.ram_governor.poll_interval_sec:
                    last_gov_check = now
                    try:
                        mtime = os.path.getmtime(self.config_path)
                        if mtime > self.last_config_mtime:
                            self.last_config_mtime = mtime
                            self.reload_config()
                    except OSError:
                        pass

                    evicted = self.ram_governor.enforce_budget(self.pinner, now=now)
                    if evicted > 0:
                        snap = self.ram_governor.evaluate(now=now)
                        self.log(
                            f"[RAM-GOVERNOR] Memory pressure ({snap['state']}, "
                            f"Avail: {snap['mem_available_bytes'] // (1024 * 1024)} MB) -> "
                            f"Evicted {evicted} pre-pinned assets to prevent swap thrashing"
                        )
                    self.telemetry.update_stats(self.get_combined_stats())

                if not self.profile and (now - last_scan) >= scan_interval:
                    last_scan = now
                    detected = self.detector.detect_running_game()

                    if detected and (not self.active_game or detected["safe_id"] != self.active_game["safe_id"]):
                        if self.active_engine:
                            self.log(f"[PRESHADER] Closing previous session for {self.active_game['name']}...")
                            if self.active_watcher:
                                self.active_watcher.close()
                            self.active_engine.shutdown()

                        self.active_game = detected
                        self.log("\n==========================================================")
                        self.log(f"[PRESHADER] >>> GAME DETECTED: {self.active_game['name']} (AppID: {self.active_game['appid']}) <<<")
                        self.log("==========================================================")

                        storage = BrainStorage(
                            vault_root=self.vault_root,
                            game_info=self.active_game,
                            external_mount=self.ext_mount,
                            max_parent_nodes=eng_cfg.get("max_parent_nodes", 60000),
                            enable_striping=ext_cfg.get("enable_cross_bus_striping", True) and not self.combat_shield,
                            max_stripe_file_bytes=ext_cfg.get("max_stripe_file_bytes", 8388608)
                        )
                        self.active_engine = RecogEngine(
                            self.config,
                            self.pinner,
                            storage,
                            velocity_tracker=self.velocity_tracker,
                            ram_governor=self.ram_governor
                        )
                        storage.save(self.active_engine.graph, self.get_combined_stats())

                        # Pre-warm shaders + precompiled caches for this game
                        warmed, _ = self.merger.prewarm_precompiled_caches(
                            self.pinner, safe_id=self.active_game["safe_id"], bypass_cooldown=True
                        )
                        for sdir in [
                            self.active_game.get("shadercache_dir", ""),
                            storage.shader_cache_dir,
                            os.path.join(self.vault_root, "global_shader_cache")
                        ]:
                            warmed += self.pinner.prewarm_directory(sdir)
                        if warmed > 0:
                            self.log(f"[PRESHADER] Pre-warmed {warmed} Vulkan/DXVK & precompiled cache files into RAM")

                        watch_dirs = []
                        for d in [
                            self.active_game.get("game_dir", ""),
                            self.active_game.get("workshop_dir", ""),
                            self.active_game.get("shadercache_dir", ""),
                            storage.shader_cache_dir
                        ]:
                            if d and os.path.isdir(d):
                                watch_dirs.append(d)

                        self.active_watcher = KernelInotifyWatcher(watch_dirs)
                        for wd in watch_dirs:
                            self.log(f"[PRESHADER] Watching Game Path: {wd}")
                        self.log(
                            f"[PRESHADER] Kernel Inotify Attached: {len(self.active_watcher.wd_to_path)} directories monitored."
                        )
                        self.telemetry.set_status("running", self.active_game["safe_id"], f"Active: {self.active_game['name']}")

                    elif not detected and self.active_game:
                        self.log(f"\n[PRESHADER] Game '{self.active_game['name']}' closed. Saving brain to disk...")
                        if self.active_watcher:
                            self.active_watcher.close()
                            self.active_watcher = None
                        if self.active_engine:
                            self.active_engine.shutdown()
                            self.active_engine = None
                        self.merger.sync_and_merge_vaults()
                        self.active_game = None
                        self.telemetry.set_status("running", "autonomous", "Standby: Precompiled caches warm")
                        self.log("[PRESHADER] Standby: Waiting for next Steam game launch...")

                if self.active_watcher and self.active_engine and not self.paused:
                    for filepath in self.active_watcher.read_events(timeout=0.25):
                        self.active_engine.process_event(filepath)
                else:
                    time.sleep(0.25)
        finally:
            if self.active_watcher:
                self.active_watcher.close()
                self.active_watcher = None
            if self.active_engine:
                self.active_engine.shutdown()
                self.active_engine = None
            self.velocity_tracker.shutdown()
            self.pinner.shutdown()
            self.telemetry.set_status("stopped", self.profile or "autonomous", "Daemon stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="Preshader Universal Autonomous Steam Daemon")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "..", "config.json"),
        help="Path to config.json"
    )
    parser.add_argument(
        "--vault-root",
        default=None,
        help="Active vault root directory"
    )
    parser.add_argument(
        "--ext-mount",
        default=None,
        help="Active external SSD mount point"
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional static profile name from config.json"
    )
    parser.add_argument(
        "--telemetry",
        default=None,
        help="Optional path to runtime telemetry JSON file"
    )
    parser.add_argument(
        "--disk-check",
        action="store_true",
        help="Run read-only disk SMART, bad-block, and vault integrity check"
    )
    parser.add_argument(
        "--disk-repair",
        action="store_true",
        help="Run disk health check and repair corrupted vault/cache blocks"
    )
    args = parser.parse_args()

    daemon = InProcessPreshaderDaemon(
        config_path=args.config,
        vault_root=args.vault_root,
        ext_mount=args.ext_mount,
        profile=args.profile,
        telemetry_path=args.telemetry
    )

    if args.disk_check or args.disk_repair:
        report = daemon.run_disk_health_check(repair=args.disk_repair)
        print(report["summary_text"], flush=True)
        return

    def handle_exit(signum, frame):
        daemon.running = False

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: daemon.reload_config())
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, lambda *_: daemon.purge_ram_cache())

    daemon.running = True
    daemon.run_loop()
    if daemon.telemetry.data.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
