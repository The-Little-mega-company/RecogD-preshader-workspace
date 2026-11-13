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
import config_ipc

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
        self.shield_threshold = 18.0
        self.max_allowed_hops = 2
        self.force_shield = False

    def update_limits(self, shield_threshold, max_allowed_hops, force_shield):
        self.shield_threshold = float(shield_threshold)
        self.max_allowed_hops = int(max_allowed_hops)
        self.force_shield = bool(force_shield)

    def record_trigger(self, now):
        self.events.append(now)
        cutoff = now - self.window_sec
        while self.events and self.events[0] < cutoff:
            self.events.popleft()

    def get_state(self, now):
        cutoff = now - self.window_sec
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        vel = len(self.events) / self.window_sec
        if vel > self.peak_velocity:
            self.peak_velocity = vel

        combat_burst = False
        if self.force_shield or vel >= self.shield_threshold:
            hops = 1
            combat_burst = True
        elif vel >= 12.0 and self.max_allowed_hops >= 3:
            hops = 3
        elif vel >= 5.0 and self.max_allowed_hops >= 2:
            hops = 2
        else:
            hops = 1

        if hops > self.max_hop_reached:
            self.max_hop_reached = hops
        return round(vel, 1), hops, combat_burst


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
                if pinner.pin_file(os.path.join(root, f), combat_burst_mode=False):
                    count += 1
    return count


def predict_and_pin(trigger_file, graph, pinner, storage, hop_depth=1, combat_burst=False):
    visited = {trigger_file}
    current_frontier = [trigger_file]
    pinned_now = 0
    min_edge_weight = 3 if combat_burst else 2

    for hop in range(1, hop_depth + 1):
        next_frontier = []
        fanout_limit = 2 if combat_burst else (4 if hop == 1 else 2)

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
                    if pinner.pin_file(child_path, combat_burst_mode=combat_burst):
                        pinned_now += 1
                    if not combat_burst and weight >= 4:
                        storage.consider_cross_bus_stripe(child_path)

        current_frontier = next_frontier
        if not current_frontier:
            break

    return pinned_now


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


def main():
    parser = argparse.ArgumentParser(description="RecogD Preshader Universal Daemon")
    parser.add_argument("--vault-root", required=True)
    parser.add_argument("--ext-mount", default="/mnt/preshader")
    args = parser.parse_args()

    cfg = config_ipc.init_live_config()
    last_flush_gen = cfg.get("flush_generation", 0)

    is_dual_bus = not os.path.abspath(args.vault_root).startswith(os.path.expanduser("~/.cache"))
    vault_label = f"Dual-Bus SSD ({args.ext_mount})" if is_dual_bus else "NVMe Fallback (~/.cache)"

    libs, games = discover_steam_games()
    print("==========================================================")
    print("   PRESHADER UNIVERSAL DAEMON ACTIVE (Ctrl+C to stop)     ")
    print("==========================================================")
    print(f"[PRESHADER] Vault Mode:       {vault_label}")
    print(f"[PRESHADER] OOM Protection:   16MB Max File Cap + 4500MB Free RAM Guard")
    print(f"[PRESHADER] Spike Shield:     ACTIVE (Dynamic I/O Rate-Limiter)")
    print(f"[PRESHADER] Steam Libraries:  {len(libs)} discovered | {len(games)} games indexed")
    print("[PRESHADER] Standby: Waiting for you to launch any Steam game...")

    running = True

    def sig_handler(signum, frame):
        nonlocal running
        running = False
        print("\n[PRESHADER] Shutting down cleanly...")

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    DELTA_T_WINDOW_SEC = 0.50

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
            "avail_ram_mb": avail_mb
        })

        active_game = find_running_game(games)
        if not active_game:
            time.sleep(1.0)
            continue

        print("\n==========================================================")
        print(f"[PRESHADER] >>> GAME DETECTED: {active_game['name']} (AppID: {active_game['appid']}) <<<")
        print("==========================================================")

        storage = BrainStorage(args.vault_root, active_game, external_mount=args.ext_mount)
        graph = storage.load()
        pinner = AssetPinner(storage, max_file_mb=16)
        pinner.apply_live_config(cfg)
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

        recent_triggers = deque(maxlen=6)
        boost_count = 0
        last_save = time.monotonic()
        last_proc_check = time.monotonic()
        last_ipc_sync = 0.0

        while running:
            now_mono = time.monotonic()

            # 1-Second Proactive RAM Safety & GUI Sync Loop
            if (now_mono - last_ipc_sync) >= 0.5:
                cfg = config_ipc.read_live_config()
                if cfg.get("shutdown_requested", False):
                    running = False
                    break

                pinner.apply_live_config(cfg)
                pinner.enforce_memory_safety()

                storage.sensor.update_limits(
                    cfg.get("min_free_ram_mb", 4500),
                    cfg.get("shield_velocity_threshold", 18.0),
                    cfg.get("max_write_mbps", 6.0),
                    cfg.get("force_combat_shield", False)
                )
                velocity.update_limits(
                    cfg.get("shield_velocity_threshold", 18.0),
                    cfg.get("max_hop_depth", 2),
                    cfg.get("force_combat_shield", False)
                )

                if cfg.get("flush_generation", 0) > last_flush_gen:
                    last_flush_gen = cfg["flush_generation"]
                    flushed = pinner.flush_all_pins()
                    print(f"[GUI-COMMAND] Purged {flushed} pinned files from RAM via DONTNEED!")

                current_vel, hop_depth, combat_burst = velocity.get_state(now_mono)
                storage.update_live_velocity(current_vel)
                pstats = pinner.get_stats()

                if cfg.get("pause_engine"):
                    st_text = "PAUSED BY USER"
                elif combat_burst:
                    st_text = "COMBAT SHIELD ACTIVE (FPS PROTECTED)"
                else:
                    st_text = "ACTIVE & PRE-PINNING"

                config_ipc.write_live_state({
                    "daemon_online": True,
                    "status_text": st_text,
                    "vault_mode": vault_label,
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
                    "io_stall_pct": round(storage.sensor.io_stall_avg10, 2)
                })
                last_ipc_sync = now_mono

            if (now_mono - last_proc_check) > 3.0:
                if not find_running_game({active_game["appid"]: active_game}):
                    break
                last_proc_check = now_mono

            events = monitor.read_events(timeout=0.20)
            if events:
                event_time = time.monotonic()
                unique_batch = list(dict.fromkeys(events))

                if len(unique_batch) > 12:
                    for _ in range(min(25, len(unique_batch))):
                        velocity.record_trigger(event_time)
                    unique_batch = unique_batch[:12]

                for fpath in unique_batch:
                    _, ext = os.path.splitext(fpath)
                    if ext.lower() not in TRACKED_EXTENSIONS:
                        continue

                    velocity.record_trigger(event_time)
                    current_vel, hop_depth, combat_burst = velocity.get_state(event_time)
                    storage.update_live_velocity(current_vel)

                    if current_vel < 40.0:
                        for prev_path, prev_time in recent_triggers:
                            if prev_path != fpath and (event_time - prev_time) <= DELTA_T_WINDOW_SEC:
                                graph[prev_path][fpath] += 1
                        recent_triggers.append((fpath, event_time))

                    pinned = predict_and_pin(fpath, graph, pinner, storage, hop_depth=hop_depth, combat_burst=combat_burst)
                    if pinned > 0:
                        boost_count += 1
                        if boost_count <= 20 or boost_count % 25 == 0:
                            stats = pinner.get_stats()
                            mode_tag = "SHIELD" if combat_burst else f"Hops:{hop_depth}"
                            print(
                                f"[BOOST #{boost_count}] {os.path.basename(fpath)} -> "
                                f"+{pinned} ({mode_tag} | Vel:{current_vel}/s | "
                                f"Cache:{stats['active_ram_pinned_mb']}MB | SysFree:{stats['system_avail_ram_mb']}MB)"
                            )

            if (now_mono - last_save) > 90.0 and storage.is_safe_to_autosave():
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

    config_ipc.write_live_state({"daemon_online": False, "status_text": "OFFLINE"})
    print("[PRESHADER] All game brains saved. Goodbye!")


if __name__ == "__main__":
    main()
