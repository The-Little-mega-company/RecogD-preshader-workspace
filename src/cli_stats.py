#!/usr/bin/env python3
import os
import json


def find_vault_dirs():
    user = os.environ.get("USER", "kessiathecreator")
    candidates = [
        f"/run/media/{user}/preshader/recogd-vault",
        f"/var/run/media/{user}/preshader/recogd-vault",
        f"/media/{user}/preshader/recogd-vault",
        "/mnt/preshader/recogd-vault",
        os.path.expanduser("~/.cache/recogd-fallback")
    ]
    found = []
    for c in candidates:
        games_dir = os.path.join(c, "games")
        if os.path.isdir(games_dir):
            found.append(games_dir)
    return found


def main():
    games_dirs = find_vault_dirs()
    brains = []
    seen_paths = set()

    for gdir in games_dirs:
        for entry in sorted(os.listdir(gdir)):
            db_path = os.path.join(gdir, entry, "brain_index", "brain.json")
            real_db = os.path.realpath(db_path)
            if os.path.isfile(db_path) and real_db not in seen_paths:
                seen_paths.add(real_db)
                brains.append(db_path)

    if not brains:
        print("[STATS] No per-game brain databases found yet. Run 'preshader' and launch any Steam game!")
        return

    print("==========================================================")
    print("      PRESHADER UNIVERSAL PER-GAME TELEMETRY REPORT       ")
    print("==========================================================\n")

    for db_path in brains:
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[WARN] Could not read {db_path}: {e}")
            continue

        meta = data.get("metadata", {})
        graph = data.get("graph", {})
        stripes = data.get("stripe_map", {})
        sstats = meta.get("session_stats", {})

        gname = meta.get("game_name", "Unknown")
        appid = meta.get("appid", "Unknown")
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
        budget_ram = sstats.get("ram_budget_ceiling_mb", 0.0)
        avail_ram = sstats.get("system_avail_ram_mb", 0.0)
        evicted_f = sstats.get("governor_evicted_files", 0)
        evicted_mb = sstats.get("governor_evicted_mb", 0.0)

        print(f"Predictive Velocity:    Peak {peak_vel:.1f} triggers/sec (Max Hop Depth: {max_hop} hops)")
        print(f"RAM Budget Governor:    {active_ram:.2f} MB active / {budget_ram:.2f} MB cap (Sys Avail: {avail_ram:.2f} MB)")
        print(f"  └─ Pressure Evictions:{evicted_f} files released ({evicted_mb:.2f} MB freed via DONTNEED)")

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
