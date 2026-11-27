#!/usr/bin/env python3
import os
import sys
import json
import glob

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config_ipc
from vault_merger import VaultCacheMerger


def find_vault_dirs():
    candidates = []
    seen_real = set()
    for pattern in (
        "/run/media/*/*/recogd-vault",
        "/var/run/media/*/*/recogd-vault",
        "/media/*/*/recogd-vault",
        "/mnt/*/recogd-vault",
        os.path.expanduser("~/.cache/recogd-fallback")
    ):
        for match in glob.glob(pattern):
            games_dir = os.path.join(match, "games")
            if os.path.isdir(games_dir):
                real_gdir = os.path.realpath(games_dir)
                if real_gdir not in seen_real:
                    seen_real.add(real_gdir)
                    candidates.append(games_dir)
    return candidates


def print_live_telemetry():
    state = config_ipc.read_live_state()
    cfg = config_ipc.read_live_config()

    online = bool(state.get("daemon_online", False))
    status_str = "ONLINE" if online else "OFFLINE"
    state_text = state.get("status_text", "STANDBY")
    vault_mode = state.get("vault_mode", "Auto")
    game_name = state.get("game_name", "None")
    appid = state.get("appid", "-")
    act_mb = float(state.get("active_ram_mb", 0.0))
    cap_mb = float(state.get("budget_ram_mb", cfg.get("max_cache_mb", 1536.0)))
    floor_mb = float(cfg.get("min_free_ram_mb", 4500))
    avail_mb = float(state.get("avail_ram_mb", 0.0))
    nvme_f = int(state.get("nvme_files", 0))
    nvme_mb = float(state.get("nvme_mb", 0.0))
    sata_f = int(state.get("sata_files", 0))
    sata_mb = float(state.get("sata_mb", 0.0))
    vel = float(state.get("velocity", 0.0))
    hops = int(state.get("hop_depth", cfg.get("max_hop_depth", 1)))
    shed = int(state.get("shed_writes", 0))

    print("==========================================================")
    print("        RECOGD PRESHADER LIVE TELEMETRY & RAM STATS       ")
    print("==========================================================")
    print(f"Daemon Status:          {status_str} ({state_text})")
    print(f"Vault Mode:             {vault_mode}")
    print(f"Active Game:            {game_name} (AppID: {appid})")
    print(f"Pinned Cache:           {act_mb:.2f} MB / {cap_mb:.0f} MB Cap")
    print(f"Protected Free Floor:   {floor_mb:.0f} MB  |  System Available RAM: {avail_mb:.0f} MB")
    print(f"Internal NVMe Pinned:   {nvme_f} files ({nvme_mb:.2f} MB)")
    print(f"External SSD Pinned:    {sata_f} files ({sata_mb:.2f} MB)")
    print(f"Velocity / Lookahead:   {vel:.1f} triggers/sec ({hops} hops)")
    print(f"Spike Shield Shed:      {shed} writes aborted")
    print("----------------------------------------------------------\n")


def main():
    print_live_telemetry()

    # Automatically merge fallback and duplicate AppID folders before reporting
    try:
        merger = VaultCacheMerger()
        merger.sync_and_merge_vaults()
    except Exception:
        pass

    games_dirs = find_vault_dirs()
    brains_by_appid = {}
    seen_paths = set()

    for gdir in games_dirs:
        for entry in sorted(os.listdir(gdir)):
            db_path = os.path.join(gdir, entry, "brain_index", "brain.json")
            real_db = os.path.realpath(db_path)
            if not os.path.isfile(db_path) or real_db in seen_paths:
                continue
            seen_paths.add(real_db)

            try:
                with open(db_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[WARN] Could not read {db_path}: {e}")
                continue

            meta = data.get("metadata", {})
            graph = data.get("graph", {})
            appid = str(meta.get("appid") or entry.split("_", 1)[0])
            node_count = len(graph)
            is_external = not real_db.startswith(os.path.expanduser("~/.cache"))

            # Keep the primary external vault brain (or the one with the most learned nodes) per AppID
            existing = brains_by_appid.get(appid)
            if existing is None or (is_external, node_count) > (existing[0], existing[1]):
                brains_by_appid[appid] = (is_external, node_count, db_path, data)

    if not brains_by_appid:
        print("[STATS] No per-game brain databases found yet. Run 'preshader' and launch any Steam game!")
        return

    print("==========================================================")
    print("      PRESHADER PER-GAME LEARNED BRAIN DATABASES          ")
    print("==========================================================\n")

    for appid, (_, _, db_path, data) in sorted(brains_by_appid.items(), key=lambda x: x[0]):
        meta = data.get("metadata", {})
        graph = data.get("graph", {})
        stripes = data.get("stripe_map", {})
        sstats = meta.get("session_stats", {})

        gname = meta.get("game_name", "Unknown")
        total_nodes = len(graph)
        total_edges = sum(len(c) for c in graph.values() if isinstance(c, dict))

        print(f">>> GAME: {gname} (AppID: {appid}) <<<")
        print(f"Brain Path:             {db_path}")
        print(f"Total Trigger Nodes:    {total_nodes}")
        print(f"Total Learned Edges:    {total_edges}")
        print(f"Cross-Bus Striped:      {len(stripes)} hot assets mirrored to External SSD")

        tot_files = sstats.get("total_pinned_files", 0)
        tot_mb = sstats.get("total_pinned_mb", 0.0)
        nvme_f = sstats.get("nvme_files", 0)
        nvme_mb = sstats.get("nvme_mb", 0.0)
        sata_f = sstats.get("sata_files", 0)
        sata_mb = sstats.get("sata_mb", 0.0)

        print(f"Last Session Pinned:    {tot_files} files ({tot_mb:.2f} MB)")
        print(f"  ├─ Internal NVMe Bus: {nvme_f} files ({nvme_mb:.2f} MB)")
        print(f"  └─ External SATA Bus: {sata_f} files ({sata_mb:.2f} MB)")

        peak_vel = sstats.get("peak_velocity_per_sec", 0.0)
        max_hop = sstats.get("max_hop_depth_used", 1)
        active_ram = sstats.get("active_ram_pinned_mb", 0.0)
        budget_ram = sstats.get("ram_budget_ceiling_mb", 1536.0)
        avail_ram = sstats.get("system_avail_ram_mb", 0.0)
        evicted_f = sstats.get("governor_evicted_files", 0)
        evicted_mb = sstats.get("governor_evicted_mb", 0.0)
        shed_writes = sstats.get("burst_writes_shed", 0)

        print(f"Predictive Velocity:    Peak {peak_vel:.1f} triggers/sec (Max Hop Depth: {max_hop} hops)")
        print(f"RAM Budget Governor:    {active_ram:.2f} MB active / {budget_ram:.0f} MB cap (Sys Free: {avail_ram:.0f} MB)")
        print(f"  ├─ OOM Guard Evicted: {evicted_f} files released ({evicted_mb:.2f} MB freed via DONTNEED)")
        print(f"  └─ Spike Shield Shed: {shed_writes} heavy writes aborted to protect FPS")

        if graph:
            print("Top 5 Reinforced Asset Triggers:")
            scored = []
            for parent, children in graph.items():
                if isinstance(children, dict) and children:
                    total_hits = sum(children.values())
                    top_child, top_count = max(children.items(), key=lambda x: x[1])
                    scored.append((parent, len(children), total_hits, top_child, top_count))
            scored.sort(key=lambda x: (x[4], x[2]), reverse=True)
            for idx, (p, deg, hits, tc, tcnt) in enumerate(scored[:5], 1):
                print(f"  {idx}. {os.path.basename(p)} ({deg} linked, {hits} hits) -> Top: {os.path.basename(tc)} [{tcnt}x]")
        print("----------------------------------------------------------\n")


if __name__ == "__main__":
    main()
