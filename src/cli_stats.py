#!/usr/bin/env python3
import os
import sys
import glob
import json
import argparse


def main():
    parser = argparse.ArgumentParser(description="Inspect Preshader Per-Game Telemetry")
    parser.add_argument(
        "--vault",
        default="/mnt/preshader/recogd-vault",
        help="Primary vault directory"
    )
    parser.add_argument(
        "--fallback-vault",
        default="~/.cache/recogd-fallback",
        help="Fallback internal NVMe vault directory"
    )
    args = parser.parse_args()

    search_roots = [
        os.path.expanduser(args.vault),
        os.path.expanduser(args.fallback_vault)
    ]
    # Also check /run/media/$USER/preshader if desktop mounted it
    user = os.environ.get("USER", "")
    if user:
        search_roots.insert(1, f"/run/media/{user}/preshader/recogd-vault")

    brain_files = []
    seen = set()
    for root in search_roots:
        pattern = os.path.join(root, "games", "*", "brain_index", "brain.json")
        for bf in sorted(glob.glob(pattern)):
            real = os.path.realpath(bf)
            if real not in seen:
                seen.add(real)
                brain_files.append(bf)

    if not brain_files:
        print("[STATS] No per-game brain databases found yet. Run 'preshader' and launch any Steam game!")
        sys.exit(0)

    print("==========================================================")
    print("      PRESHADER UNIVERSAL PER-GAME TELEMETRY REPORT       ")
    print("==========================================================")

    for bf in brain_files:
        try:
            with open(bf, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"Could not read {bf}: {e}")
            continue

        meta = data.get("metadata", {})
        graph = data.get("graph", {})
        stripe_map = data.get("stripe_map", {})
        stats = meta.get("session_stats", {})

        game_name = meta.get("game_name", "Unknown Game")
        appid = meta.get("appid", "?")
        total_mb = stats.get("total_pinned_bytes", 0) / (1024 * 1024)
        nvme_mb = stats.get("nvme_pinned_bytes", 0) / (1024 * 1024)
        ext_mb = stats.get("ext_pinned_bytes", 0) / (1024 * 1024)
        active_mb = stats.get("active_pinned_bytes", 0) / (1024 * 1024)
        evicted_mb = stats.get("evicted_bytes", 0) / (1024 * 1024)

        print(f"\n>>> GAME: {game_name} (AppID: {appid}) <<<")
        print(f"Brain Path:             {bf}")
        print(f"Total Trigger Nodes:    {len(graph)}")
        print(f"Total Learned Edges:    {meta.get('total_edges', 0)}")
        print(f"Cross-Bus Striped:      {len(stripe_map)} hot assets mirrored to External SSD")
        print(f"Last Session Pinned:    {stats.get('total_pinned_files', 0)} files ({total_mb:.2f} MB)")
        print(f"  ├─ Internal NVMe Bus: {stats.get('nvme_pinned_files', 0)} files ({nvme_mb:.2f} MB)")
        print(f"  └─ External SATA Bus: {stats.get('ext_pinned_files', 0)} files ({ext_mb:.2f} MB)")
        print(
            f"Predictive Velocity:    Peak {stats.get('peak_velocity_score', 0.0):.2f} "
            f"(State: {stats.get('velocity_state', 'IDLE')}, "
            f"Corner Lookahead Pins: {stats.get('lookahead_pins_total', 0)})"
        )
        print(
            f"RAM Budget Governor:    {stats.get('ram_governor_state', 'OPTIMAL')} "
            f"(Scale: {int(stats.get('ram_scale_factor', 1.0) * 100)}%, "
            f"Active Cache: {active_mb:.2f} MB, "
            f"Evicted: {stats.get('evicted_files', 0)} files / {evicted_mb:.2f} MB)"
        )

        ranked_parents = sorted(
            graph.items(),
            key=lambda item: sum(item[1].values()),
            reverse=True
        )[:5]

        if ranked_parents:
            print("Top 5 Asset Triggers:")
            for idx, (parent, children) in enumerate(ranked_parents, 1):
                total_hits = sum(children.values())
                top_child, top_count = max(children.items(), key=lambda x: x[1])
                print(
                    f"  {idx}. {os.path.basename(parent)} "
                    f"({len(children)} linked, {total_hits} hits) "
                    f"-> Top: {os.path.basename(top_child)} [{top_count}x]"
                )
        print("----------------------------------------------------------")


if __name__ == "__main__":
    main()
