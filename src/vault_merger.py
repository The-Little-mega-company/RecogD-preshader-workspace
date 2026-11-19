#!/usr/bin/env python3
import glob
import json
import os
import shutil
import time
from collections import defaultdict


PRECOMPILED_EXTENSIONS = (
    ".foz", ".dxvk-cache", ".spv", ".bin", ".idx", ".cache",
    ".pak", ".esl", ".esm", ".esp", ".ba2", ".bsa", ".pbo",
    ".edds", ".paa", ".rvmat", ".ucas", ".utoc", ".vpk"
)


def detect_preshader_mounts():
    """
    Finds the active external Preshader SSD mount and internal NVMe fallback vault.
    """
    user = os.environ.get("USER", "kessia")
    ext_candidates = [
        f"/run/media/{user}/preshader",
        f"/var/run/media/{user}/preshader",
        f"/media/{user}/preshader",
        "/mnt/preshader"
    ]
    ext_mount = "/run/media/kessia/preshader"
    for cand in ext_candidates:
        if os.path.isdir(cand) and (os.path.ismount(cand) or os.path.isdir(os.path.join(cand, "recogd-vault"))):
            ext_mount = cand
            break

    ext_vault = os.path.join(ext_mount, "recogd-vault")
    nvme_vault = os.path.expanduser("~/.cache/recogd-fallback")
    return ext_mount, ext_vault, nvme_vault


class VaultCacheMerger:
    """
    Unifies the external Preshader SSD vault (/run/media/$USER/preshader/recogd-vault)
    and internal NVMe vault (~/.cache/recogd-fallback) into one cohesive cache index,
    and pre-pins precompiled shader/asset caches (.foz, .pak, .bin, .esl, .esm) into RAM.
    """

    def __init__(self, ext_vault=None, nvme_vault=None):
        ext_mount, default_ext_vault, default_nvme_vault = detect_preshader_mounts()
        self.ext_mount = ext_mount
        self.ext_vault = ext_vault or default_ext_vault
        self.nvme_vault = nvme_vault or default_nvme_vault

    def active_vault_roots(self):
        roots = []
        for r in (self.ext_vault, self.nvme_vault):
            if r and os.path.isdir(r) and r not in roots:
                roots.append(r)
        return roots

    def primary_vault_root(self):
        if os.path.isdir(self.ext_vault) and os.access(self.ext_vault, os.W_OK):
            return self.ext_vault
        os.makedirs(self.nvme_vault, exist_ok=True)
        return self.nvme_vault

    def sync_and_merge_vaults(self):
        """
        Merges learned Markov graphs (brain.json + legacy dayz_brain.json) and
        indexes precompiled caches across both the External SSD and Internal NVMe vaults.
        """
        os.makedirs(os.path.join(self.nvme_vault, "games"), exist_ok=True)
        if os.path.isdir(self.ext_mount) and os.access(self.ext_mount, os.W_OK):
            os.makedirs(os.path.join(self.ext_vault, "games"), exist_ok=True)
            os.makedirs(os.path.join(self.ext_vault, "global_shader_cache"), exist_ok=True)

        # 1. Fold legacy ~/.cache/recogd-fallback/brain_index/dayz_brain.json into 221100_DayZ
        legacy_dayz = os.path.join(self.nvme_vault, "brain_index", "dayz_brain.json")
        target_dayz_vault = (
            self.ext_vault
            if os.path.isdir(os.path.join(self.ext_vault, "games"))
            else self.nvme_vault
        )
        if os.path.isfile(legacy_dayz):
            dayz_db = os.path.join(target_dayz_vault, "games", "221100_DayZ", "brain_index", "brain.json")
            self._merge_two_brain_files(
                legacy_dayz,
                dayz_db,
                default_meta={"game_name": "DayZ", "appid": "221100", "safe_id": "221100_DayZ"}
            )

        # 2. Discover all game safe_ids across both vaults and merge their brain.json graphs
        game_ids = set()
        for root in self.active_vault_roots():
            games_dir = os.path.join(root, "games")
            if os.path.isdir(games_dir):
                for entry in os.listdir(games_dir):
                    if entry == "custom_SteamLinuxRuntime_4":
                        continue
                    if os.path.isdir(os.path.join(games_dir, entry)):
                        game_ids.add(entry)

        merged_count = 0
        for safe_id in sorted(game_ids):
            ext_db = os.path.join(self.ext_vault, "games", safe_id, "brain_index", "brain.json")
            nvme_db = os.path.join(self.nvme_vault, "games", safe_id, "brain_index", "brain.json")
            if os.path.isfile(ext_db) and os.path.isfile(nvme_db):
                self._merge_two_brain_files(nvme_db, ext_db)
                merged_count += 1
            elif os.path.isfile(nvme_db) and os.path.isdir(self.ext_vault) and os.access(self.ext_vault, os.W_OK):
                os.makedirs(os.path.dirname(ext_db), exist_ok=True)
                shutil.copy2(nvme_db, ext_db)
                merged_count += 1

        return merged_count

    def _merge_two_brain_files(self, src_db, dst_db, default_meta=None):
        try:
            with open(src_db, "r", encoding="utf-8") as f:
                src_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return False

        dst_data = {}
        if os.path.isfile(dst_db):
            try:
                with open(dst_db, "r", encoding="utf-8") as f:
                    dst_data = json.load(f)
            except (OSError, json.JSONDecodeError):
                dst_data = {}

        merged_graph = defaultdict(lambda: defaultdict(int))
        for payload in (dst_data, src_data):
            raw_g = payload.get("graph", {})
            if isinstance(raw_g, dict):
                for parent, children in raw_g.items():
                    if isinstance(children, dict):
                        for child, hits in children.items():
                            merged_graph[parent][child] = max(
                                merged_graph[parent][child], int(hits)
                            )

        merged_stripes = {}
        for payload in (src_data, dst_data):
            s_map = payload.get("stripe_map", {})
            if isinstance(s_map, dict):
                for orig, mirrored in s_map.items():
                    if os.path.exists(mirrored):
                        merged_stripes[orig] = mirrored

        meta = dict(default_meta or {})
        meta.update(src_data.get("metadata", {}))
        meta.update(dst_data.get("metadata", {}))
        meta["updated_at_epoch"] = int(time.time())
        meta["total_parent_nodes"] = len(merged_graph)
        meta["total_edges"] = sum(len(c) for c in merged_graph.values())
        meta["total_striped_assets"] = len(merged_stripes)

        out_payload = {
            "metadata": meta,
            "stripe_map": merged_stripes,
            "graph": {k: dict(v) for k, v in merged_graph.items()}
        }
        os.makedirs(os.path.dirname(dst_db), exist_ok=True)
        tmp = f"{dst_db}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out_payload, f)
            os.replace(tmp, dst_db)
            return True
        except OSError:
            return False

    def discover_precompiled_caches(self):
        """
        Scans /run/media/$USER/preshader/recogd-vault and ~/.cache/recogd-fallback
        for all per-game brain indexes and precompiled cache files (.foz, .pak, .bin, .esl, .esm).
        Returns a list of game cache entries sorted by activity/size.
        """
        entries_by_id = {}

        for root in self.active_vault_roots():
            games_dir = os.path.join(root, "games")
            if not os.path.isdir(games_dir):
                continue

            for safe_id in sorted(os.listdir(games_dir)):
                if safe_id == "custom_SteamLinuxRuntime_4":
                    continue
                g_vault = os.path.join(games_dir, safe_id)
                if not os.path.isdir(g_vault):
                    continue

                entry = entries_by_id.setdefault(safe_id, {
                    "safe_id": safe_id,
                    "game_name": safe_id.split("_", 1)[-1].replace("_", " "),
                    "appid": safe_id.split("_", 1)[0],
                    "brain_path": "",
                    "nodes": 0,
                    "edges": 0,
                    "striped": 0,
                    "cache_files": [],
                    "cache_bytes": 0,
                    "session_pinned_bytes": 0,
                    "top_chain": "Ready for pre-warming",
                    "graph_preview": {}
                })

                # Read brain.json if present
                db_path = os.path.join(g_vault, "brain_index", "brain.json")
                if os.path.isfile(db_path):
                    try:
                        with open(db_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        meta = data.get("metadata", {})
                        graph = data.get("graph", {})
                        stripes = data.get("stripe_map", {})
                        stats = meta.get("session_stats", {})

                        if len(graph) >= entry["nodes"]:
                            entry["brain_path"] = db_path
                            entry["game_name"] = meta.get("game_name", entry["game_name"])
                            entry["appid"] = str(meta.get("appid", entry["appid"]))
                            entry["nodes"] = len(graph)
                            entry["edges"] = int(
                                meta.get("total_edges")
                                or sum(len(c) for c in graph.values() if isinstance(c, dict))
                            )
                            entry["striped"] = max(entry["striped"], len(stripes))
                            pinned_b = int(
                                stats.get("total_pinned_bytes")
                                or float(stats.get("total_pinned_mb", 0.0)) * 1024 * 1024
                            )
                            entry["session_pinned_bytes"] = max(entry["session_pinned_bytes"], pinned_b)
                            if graph:
                                top_p, top_children = max(
                                    graph.items(),
                                    key=lambda x: sum(x[1].values()) if isinstance(x[1], dict) else 0
                                )
                                if isinstance(top_children, dict) and top_children:
                                    top_c, hits = max(top_children.items(), key=lambda x: x[1])
                                    entry["top_chain"] = f"{os.path.basename(top_p)} → {os.path.basename(top_c)} ({hits}x)"
                    except (OSError, json.JSONDecodeError):
                        pass

                # Collect precompiled files in hot_stripe_cache and shader_cache
                seen_files = set(entry["cache_files"])
                for sub in ("hot_stripe_cache", "shader_cache"):
                    sdir = os.path.join(g_vault, sub)
                    if not os.path.isdir(sdir):
                        continue
                    for r, _, files in os.walk(sdir):
                        for fname in files:
                            if fname.endswith(".tmp") or fname.endswith(".json"):
                                continue
                            fpath = os.path.join(r, fname)
                            if fpath not in seen_files and os.path.isfile(fpath):
                                try:
                                    sz = os.path.getsize(fpath)
                                except OSError:
                                    sz = 0
                                if sz > 0:
                                    seen_files.add(fpath)
                                    entry["cache_files"].append(fpath)
                                    entry["cache_bytes"] += sz

                entry["striped"] = max(entry["striped"], len(entry["cache_files"]))
                if entry["cache_files"] and entry["top_chain"] == "Ready for pre-warming":
                    sample_name = os.path.basename(entry["cache_files"][0]).split("_", 1)[-1]
                    entry["top_chain"] = f"Precompiled: {sample_name} (+{len(entry['cache_files'])} cached)"

        return sorted(
            entries_by_id.values(),
            key=lambda e: (len(e["cache_files"]), e["nodes"]),
            reverse=True
        )

    def prewarm_precompiled_caches(self, pinner, safe_id=None, bypass_cooldown=True):
        """
        Pre-pins precompiled caches (.foz, .pak, .bin, .esl, .esm) from
        /run/media/$USER/preshader/recogd-vault into RAM using RamPinner.
        """
        entries = self.discover_precompiled_caches()
        files_pinned = 0
        bytes_queued = 0

        for entry in entries:
            if safe_id and entry["safe_id"] != safe_id and entry["appid"] != str(safe_id):
                continue
            for fpath in entry["cache_files"]:
                if bypass_cooldown:
                    with pinner.lock:
                        pinner.cooldown_map.pop(fpath, None)
                if pinner.request_pin(fpath):
                    files_pinned += 1
                    try:
                        bytes_queued += min(os.path.getsize(fpath), pinner.max_size_bytes)
                    except OSError:
                        pass

            # If this game also has a brain.json with existing target files on disk, pin top triggers
            if entry["brain_path"] and os.path.isfile(entry["brain_path"]):
                try:
                    with open(entry["brain_path"], "r", encoding="utf-8") as f:
                        bdata = json.load(f)
                    graph = bdata.get("graph", {})
                    ranked_parents = sorted(
                        graph.items(),
                        key=lambda item: sum(item[1].values()) if isinstance(item[1], dict) else 0,
                        reverse=True
                    )[:8]
                    for parent_path, children in ranked_parents:
                        for candidate in [parent_path] + list(children.keys())[:3]:
                            if os.path.isfile(candidate):
                                if bypass_cooldown:
                                    with pinner.lock:
                                        pinner.cooldown_map.pop(candidate, None)
                                if pinner.request_pin(candidate):
                                    files_pinned += 1
                                    try:
                                        bytes_queued += min(os.path.getsize(candidate), pinner.max_size_bytes)
                                    except OSError:
                                        pass
                except (OSError, json.JSONDecodeError):
                    pass

        # Also prewarm global shader cache directories across both vaults
        for root in self.active_vault_roots():
            for gdir in ("global_shader_cache", "dxvk_state_cache", "mesa_shader_cache"):
                full_gdir = os.path.join(root, gdir)
                if os.path.isdir(full_gdir):
                    files_pinned += pinner.prewarm_directory(full_gdir)

        return files_pinned, bytes_queued
