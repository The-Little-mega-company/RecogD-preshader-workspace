#!/usr/bin/env python3
import os
import sys
import time
import signal
import ctypes
import select
import struct
import argparse
from collections import deque

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from storage import BrainStorage
from pinner import AssetPinner

IN_ACCESS = 0x00000001
IN_OPEN = 0x00000020
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000

TRACKED_EXTENSIONS = {
    ".pbo", ".bsa", ".ba2", ".esm", ".esp", ".esl",
    ".pak", ".ucas", ".utoc", ".upk", ".uasset", ".umap",
    ".foz", ".dxvk-cache", ".bin", ".bundle", ".assets", ".resS", ".resource", ".wad", ".vpk"
}


class VelocityTracker:
    def __init__(self, window_sec=2.0):
        self.window_sec = window_sec
        self.events = deque()
        self.peak_velocity = 0.0
        self.max_hop_reached = 1

    def record_trigger(self, now):
        self.events.append(now)
        cutoff = now - self.window_sec
        while self.events and self.events[0] < cutoff:
            self.events.popleft()

    def get_velocity_and_hops(self, now):
        cutoff = now - self.window_sec
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        vel = len(self.events) / self.window_sec
        if vel > self.peak_velocity:
            self.peak_velocity = vel

        if vel >= 25.0:
            hops = 3
        elif vel >= 8.0:
            hops = 2
        else:
            hops = 1

        if hops > self.max_hop_reached:
            self.max_hop_reached = hops
        return round(vel, 1), hops


class InotifyMonitor:
    def __init__(self):
        self.libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int

        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        self.wd_to_path = {}

    def add_tree(self, root_path, max_dirs=2500):
        if self.fd < 0 or not os.path.isdir(root_path):
            return 0
        added = 0
        mask = IN_OPEN | IN_ACCESS
        for dirpath, dirnames, _ in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            wd = self.libc.inotify_add_watch(self.fd, dirpath.encode("utf-8", errors="ignore"), mask)
            if wd >= 0:
                self.wd_to_path[wd] = dirpath
                added += 1
            if added >= max_dirs:
                break
        return added

    def read_events(self, timeout=0.25):
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
            name = name_bytes.rstrip(b"\x00").decode("utf-8", errors="ignore")
            if name and wd in self.wd_to_path:
                full_path = os.path.join(self.wd_to_path[wd], name)
                events.append(full_path)
        return events

    def close(self):
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        self.wd_to_path.clear()


def discover_steam_games():
    roots = [
        os.path.expanduser("~/.local/share/Steam"),
        os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.local/share/Steam")
    ]
    libraries = set()
    for r in roots:
        if os.path.isdir(r):
            libraries.add(os.path.abspath(r))
            vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
            if os.path.isfile(vdf):
                try:
                    with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if '"path"' in line:
                                parts = line.split('"')
                                if len(parts) >= 4 and os.path.isdir(parts[3]):
                                    libraries.add(os.path.abspath(parts[3]))
                except Exception:
                    pass

    games = {}
    for lib in libraries:
        steamapps = os.path.join(lib, "steamapps")
        if not os.path.isdir(steamapps):
            continue
        for fname in os.listdir(steamapps):
            if fname.startswith("appmanifest_") and fname.endswith(".acf"):
                acf_path = os.path.join(steamapps, fname)
                appid, name, installdir = None, None, None
                try:
                    with open(acf_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if '"appid"' in line:
                                appid = line.split('"')[3]
                            elif '"name"' in line:
                                name = line.split('"')[3]
                            elif '"installdir"' in line:
                                installdir = line.split('"')[3]
                except Exception:
                    continue

                if appid and name and installdir:
                    if appid in {"228980", "1070560", "1391110", "1628350", "1493710"} or "Proton" in name or "Steam Linux Runtime" in name:
                        continue
                    game_dir = os.path.join(steamapps, "common", installdir)
                    if os.path.isdir(game_dir):
                        safe_name = "".join(c if c.isalnum() else "_" for c in name).strip("_")
                        safe_id = f"{appid}_{safe_name}"
                        games[appid] = {
                            "appid": appid,
                            "name": name,
                            "safe_id": safe_id,
                            "install_dir": game_dir,
                            "workshop_dir": os.path.join(steamapps, "workshop", "content", appid),
                            "shader_dir": os.path.join(steamapps, "shadercache", appid)
                        }
    return list(libraries), games


def find_running_game(games):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                env_data = f.read().decode("utf-8", errors="ignore")
            for token in env_data.split("\x00"):
                if token.startswith("SteamAppId=") or token.startswith("SteamGameId="):
                    appid = token.split("=", 1)[1].strip()
                    if appid in games:
                        return games[appid]
        except Exception:
            pass

        try:
            exe_link = os.readlink(f"/proc/{pid}/exe")
            for g in games.values():
                if exe_link.startswith(g["install_dir"]):
                    return g
        except Exception:
            pass
    return None


def prewarm_shaders(shader_dir, pinner):
    count = 0
    if not os.path.isdir(shader_dir):
        return 0
    for root, _, files in os.walk(shader_dir):
        for f in files:
            if f.endswith((".foz", ".dxvk-cache", ".bin")):
                if pinner.pin_file(os.path.join(root, f)):
                    count += 1
    return count


def predict_and_pin(trigger_file, graph, pinner, storage, hop_depth=1, min_edge_weight=2):
    visited = {trigger_file}
    current_frontier = [trigger_file]
    pinned_now = 0

    for hop in range(1, hop_depth + 1):
        next_frontier = []
        fanout_limit = 6 if hop == 1 else (3 if hop == 2 else 2)

        for node in current_frontier:
            if node not in graph:
                continue
            children = graph[node]
            candidates = sorted(
                [(child, weight) for child, weight in children.items() if weight >= min_edge_weight],
                key=lambda x: x[1],
                reverse=True
            )[:fanout_limit]

            for child_path, weight in candidates:
                if child_path not in visited:
                    visited.add(child_path)
                    next_frontier.append(child_path)
                    if pinner.pin_file(child_path):
                        pinned_now += 1
                    if weight >= 4:
                        storage.consider_cross_bus_stripe(child_path)

        current_frontier = next_frontier
        if not current_frontier:
            break

    return pinned_now


def main():
    parser = argparse.ArgumentParser(description="RecogD Preshader Universal Daemon")
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--ext-mount", default="/mnt/preshader")
    args = parser.parse_args()

    libs, games = discover_steam_games()
    print("==========================================================")
    print("   PRESHADER UNIVERSAL DAEMON ACTIVE (Ctrl+C to stop)     ")
    print("==========================================================")
    print(f"[PRESHADER] Vault Root:       {args.vault_root}")
    print(f"[PRESHADER] Features:         Predictive Velocity (1-3 Hops) + RAM Budget Governor")
    print(f"[PRESHADER] Delta-T Filter:   500ms Strict Temporal Window (Anti-Staircase)")
    print(f"[PRESHADER] Steam Libraries:  {len(libs)} discovered")
    print(f"[PRESHADER] Installed Games:  {len(games)} indexed")
    print("[PRESHADER] Standby: Waiting for you to launch any Steam game...")

    running = True

    def sig_handler(signum, frame):
        nonlocal running
        running = False
        print("\n[PRESHADER] Ctrl+C received. Shutting down cleanly...")

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    DELTA_T_WINDOW_SEC = 0.50

    while running:
        active_game = find_running_game(games)
        if not active_game:
            time.sleep(1.5)
            continue

        print("\n==========================================================")
        print(f"[PRESHADER] >>> GAME DETECTED: {active_game['name']} (AppID: {active_game['appid']}) <<<")
        print("==========================================================")

        storage = BrainStorage(args.vault_root, active_game, external_mount=args.ext_mount)
        graph = storage.load()
        pinner = AssetPinner(storage)
        velocity = VelocityTracker(window_sec=2.0)

        print(f"[PRESHADER-STORAGE] Loaded '{active_game['name']}' brain: {len(graph)} triggers, {len(storage.stripe_map)} striped assets.")

        warmed = prewarm_shaders(active_game["shader_dir"], pinner)
        print(f"[PRESHADER] Pre-warmed {warmed} Vulkan/DXVK shader files into RAM")

        monitor = InotifyMonitor()
        watch_dirs = 0
        for p in [active_game["install_dir"], active_game["workshop_dir"], active_game["shader_dir"], storage.shader_cache_dir]:
            if os.path.isdir(p):
                print(f"[PRESHADER] Watching Game Path: {p}")
                watch_dirs += monitor.add_tree(p)

        print(f"[PRESHADER] Kernel Inotify Attached: {watch_dirs} directories monitored.")

        recent_triggers = deque(maxlen=8)
        boost_count = 0
        last_save = time.monotonic()
        last_proc_check = time.monotonic()

        while running:
            now_mono = time.monotonic()
            if (now_mono - last_proc_check) > 3.0:
                if not find_running_game({active_game["appid"]: active_game}):
                    break
                last_proc_check = now_mono

            events = monitor.read_events(timeout=0.20)
            if events:
                event_time = time.monotonic()
                unique_batch = list(dict.fromkeys(events))

                for fpath in unique_batch:
                    _, ext = os.path.splitext(fpath)
                    if ext.lower() not in TRACKED_EXTENSIONS:
                        continue

                    for prev_path, prev_time in recent_triggers:
                        if prev_path != fpath and (event_time - prev_time) <= DELTA_T_WINDOW_SEC:
                            graph[prev_path][fpath] += 1

                    recent_triggers.append((fpath, event_time))

                    velocity.record_trigger(event_time)
                    current_vel, hop_depth = velocity.get_velocity_and_hops(event_time)

                    pinned = predict_and_pin(fpath, graph, pinner, storage, hop_depth=hop_depth, min_edge_weight=2)
                    if pinned > 0:
                        boost_count += 1
                        if boost_count <= 25 or boost_count % 25 == 0:
                            stats = pinner.get_stats()
                            print(
                                f"[BOOST #{boost_count}] {os.path.basename(fpath)} -> "
                                f"+{pinned} assets (Vel: {current_vel}/s | Hops: {hop_depth} | "
                                f"RAM Pinned: {stats['active_ram_pinned_mb']}MB / Avail: {stats['system_avail_ram_mb']}MB)"
                            )

            if (now_mono - last_save) > 45.0:
                stats = pinner.get_stats()
                stats["peak_velocity_per_sec"] = round(velocity.peak_velocity, 1)
                stats["max_hop_depth_used"] = velocity.max_hop_reached
                storage.save(graph, stats)
                last_save = now_mono

        monitor.close()
        final_stats = pinner.get_stats()
        final_stats["peak_velocity_per_sec"] = round(velocity.peak_velocity, 1)
        final_stats["max_hop_depth_used"] = velocity.max_hop_reached
        print(f"\n[PRESHADER] Game '{active_game['name']}' closed. Saving brain to disk...")
        storage.save(graph, final_stats)
        storage.shutdown()
        print(f"[PRESHADER-STORAGE] Saved '{active_game['name']}' brain ({len(graph)} nodes, {len(storage.stripe_map)} striped) -> {storage.db_path}")
        print("[PRESHADER] Standby: Waiting for next Steam game launch...")

    print("[PRESHADER] All game brains saved. Goodbye!")


if __name__ == "__main__":
    main()
