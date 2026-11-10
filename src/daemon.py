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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pinner import RamPinner
from storage import BrainStorage
from engine import RecogEngine
from steam_detector import SteamGameDetector

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
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Preshader Universal Autonomous Steam Daemon")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "..", "config.json"),
        help="Path to config.json"
    )
    parser.add_argument(
        "--vault-root",
        default="/mnt/preshader/recogd-vault",
        help="Active vault root directory"
    )
    parser.add_argument(
        "--ext-mount",
        default="/mnt/preshader",
        help="Active external SSD mount point"
    )
    args = parser.parse_args()

    config = load_config(os.path.abspath(args.config))
    eng_cfg = config.get("engine", {})
    ext_cfg = config.get("external_drive", {})

    scan_interval = eng_cfg.get("process_scan_interval_seconds", 2.0)

    pinner = RamPinner(
        cooldown_sec=eng_cfg.get("ram_pin_cooldown_seconds", 5.0),
        max_size_bytes=eng_cfg.get("max_file_size_bytes", 16777216),
        external_mount=args.ext_mount
    )

    detector = SteamGameDetector(extra_mount=args.ext_mount)

    print("==========================================================", flush=True)
    print("   PRESHADER UNIVERSAL DAEMON ACTIVE (Ctrl+C to stop)     ", flush=True)
    print("==========================================================", flush=True)
    print(f"[PRESHADER] Vault Root:       {args.vault_root}", flush=True)
    print(f"[PRESHADER] Steam Libraries:  {len(detector.libraries)} discovered", flush=True)
    print(f"[PRESHADER] Installed Games:  {len(detector.manifests)} indexed", flush=True)
    print("[PRESHADER] Standby: Waiting for you to launch any Steam game...", flush=True)

    active_game = None
    active_engine = None
    active_watcher = None
    last_scan = 0.0
    running = True

    def handle_exit(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    try:
        while running:
            now = time.monotonic()
            if (now - last_scan) >= scan_interval:
                last_scan = now
                detected = detector.detect_running_game()

                # Case 1: New game launched
                if detected and (not active_game or detected["safe_id"] != active_game["safe_id"]):
                    if active_engine:
                        print(f"[PRESHADER] Closing previous session for {active_game['name']}...", flush=True)
                        active_watcher.close()
                        active_engine.shutdown()

                    active_game = detected
                    pinner.reset_stats()
                    print("\n==========================================================", flush=True)
                    print(f"[PRESHADER] >>> GAME DETECTED: {active_game['name']} (AppID: {active_game['appid']}) <<<", flush=True)
                    print("==========================================================", flush=True)

                    storage = BrainStorage(
                        vault_root=args.vault_root,
                        game_info=active_game,
                        external_mount=args.ext_mount,
                        max_parent_nodes=eng_cfg.get("max_parent_nodes", 60000),
                        enable_striping=ext_cfg.get("enable_cross_bus_striping", True),
                        max_stripe_file_bytes=ext_cfg.get("max_stripe_file_bytes", 8388608)
                    )
                    active_engine = RecogEngine(config, pinner, storage)
                    storage.save(active_engine.graph, pinner.stats)

                    # Pre-warm shaders for this game
                    warmed = 0
                    for sdir in [
                        active_game.get("shadercache_dir", ""),
                        storage.shader_cache_dir,
                        os.path.join(args.vault_root, "global_shader_cache")
                    ]:
                        warmed += pinner.prewarm_directory(sdir)
                    if warmed > 0:
                        print(f"[PRESHADER] Pre-warmed {warmed} Vulkan/DXVK shader files into RAM", flush=True)

                    watch_dirs = []
                    for d in [
                        active_game.get("game_dir", ""),
                        active_game.get("workshop_dir", ""),
                        active_game.get("shadercache_dir", ""),
                        storage.shader_cache_dir
                    ]:
                        if d and os.path.isdir(d):
                            watch_dirs.append(d)

                    active_watcher = KernelInotifyWatcher(watch_dirs)
                    for wd in watch_dirs:
                        print(f"[PRESHADER] Watching Game Path: {wd}", flush=True)
                    print(
                        f"[PRESHADER] Kernel Inotify Attached: {len(active_watcher.wd_to_path)} directories monitored.",
                        flush=True
                    )

                # Case 2: Active game exited
                elif not detected and active_game:
                    print(f"\n[PRESHADER] Game '{active_game['name']}' closed. Saving brain to disk...", flush=True)
                    if active_watcher:
                        active_watcher.close()
                        active_watcher = None
                    if active_engine:
                        active_engine.shutdown()
                        active_engine = None
                    active_game = None
                    print("[PRESHADER] Standby: Waiting for next Steam game launch...", flush=True)

            if active_watcher and active_engine:
                for filepath in active_watcher.read_events(timeout=0.25):
                    active_engine.process_event(filepath)
            else:
                time.sleep(0.5)

    except KeyboardInterrupt:
        pass
    finally:
        print("\n[PRESHADER] Ctrl+C received. Shutting down cleanly...", flush=True)
        if active_watcher:
            active_watcher.close()
        if active_engine:
            active_engine.shutdown()
        pinner.shutdown()
        print("[PRESHADER] All game brains saved. Goodbye!", flush=True)


if __name__ == "__main__":
    main()
