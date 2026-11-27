#!/usr/bin/env python3
import glob
import json
import os
import pwd
import re
import shutil
import time
from collections import defaultdict


PRECOMPILED_EXTENSIONS = (
    ".foz", ".dxvk-cache", ".spv", ".bin", ".idx", ".cache",
    ".pak", ".esl", ".esm", ".esp", ".ba2", ".bsa", ".pbo",
    ".edds", ".paa", ".rvmat", ".ucas", ".utoc", ".vpk",
    ".jar", ".mca", ".dat"
)


def normalize_safe_id(safe_id):
    cleaned = re.sub(r"_+", "_", safe_id.strip()).strip("_")
    return cleaned


def is_placeholder_safe_id(safe_id):
    low = safe_id.lower()
    return "appid_" in low or low.endswith("_unknowngame")


def detect_preshader_mounts():
    """
    Finds the active external Preshader SSD mount and internal NVMe fallback vault.
    """
    user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    ext_candidates = [
        "/mnt/preshader",
        f"/run/media/{user}/preshader",
        f"/var/run/media/{user}/preshader",
        f"/media/{user}/preshader",
    ]
    for pattern in (
        "/mnt/*/recogd-vault",
        "/run/media/*/*/recogd-vault",
        "/var/run/media/*/*/recogd-vault",
        "/media/*/*/recogd-vault",
    ):
        for match in glob.glob(pattern):
            parent = os.path.dirname(match)
            if parent not in ext_candidates:
                ext_candidates.append(parent)

    ext_mount = "/mnt/preshader"
    for cand in ext_candidates:
        if os.path.isdir(cand) and (os.path.ismount(cand) or os.path.isdir(os.path.join(cand, "recogd-vault"))):
            ext_mount = os.path.realpath(cand)
            break

    ext_vault = os.path.join(ext_mount, "recogd-vault")
    nvme_vault = os.path.expanduser("~/.cache/recogd-fallback")
    return ext_mount, ext_vault, nvme_vault



class VaultCacheMerger:
    """
    Unifies the external Preshader SSD vault (/run/media/$USER/preshader/recogd-vault)
    and internal NVMe vault (~/.cache/recogd-fallback) into one cohesive cache index,
    deduplicating by AppID and removing stale fallback/duplicate directories.
    """

    def __init__(self, ext_vault=None, nvme_vault=None):
        ext_mount, default_ext_vault, default_nvme_vault = detect_preshader_mounts()
        self.ext_mount = ext_mount
        self.ext_vault = ext_vault or default_ext_vault
        self.nvme_vault = nvme_vault or default_nvme_vault

    def active_vault_roots(self):
        roots = []
        seen_real = set()
        for r in (self.ext_vault, self.nvme_vault):
            if r and os.path.isdir(r):
                real_r = os.path.realpath(r)
                if real_r not in seen_real:
                    seen_real.add(real_r)
                    roots.append(r)
        return roots

    def primary_vault_root(self):
        if os.path.isdir(self.ext_vault) and os.access(self.ext_vault, os.W_OK):
            return self.ext_vault
        os.makedirs(self.nvme_vault, exist_ok=True)
        return self.nvme_vault

    def _merge_directory_contents(self, src_dir, dst_dir):
        if not os.path.isdir(src_dir):
            return
        os.makedirs(dst_dir, exist_ok=True)
        for item in os.listdir(src_dir):
            s_item = os.path.join(src_dir, item)
            d_item = os.path.join(dst_dir, item)
            if os.path.isdir(s_item):
                self._merge_directory_contents(s_item, d_item)
            elif os.path.isfile(s_item):
                if not os.path.exists(d_item):
                    try:
                        shutil.copy2(s_item, d_item)
                    except OSError:
                        pass

    def sync_and_merge_vaults(self):
        """
        Merges learned Markov graphs across both vaults, consolidates duplicate AppID
        directories (such as double-underscore or appid_XXX folders), and removes
        redundant fallback directories once safely merged onto the External SSD.
        """
        os.makedirs(os.path.join(self.nvme_vault, "games"), exist_ok=True)
        ext_writable = os.path.isdir(self.ext_mount) and os.access(self.ext_mount, os.W_OK)
        if ext_writable:
            os.makedirs(os.path.join(self.ext_vault, "games"), exist_ok=True)
            os.makedirs(os.path.join(self.ext_vault, "global_shader_cache"), exist_ok=True)

        target_primary_vault = self.ext_vault if ext_writable else self.nvme_vault

        # 1. Fold legacy ~/.cache/recogd-fallback/brain_index/dayz_brain.json into 221100_DayZ
        legacy_dayz = os.path.join(self.nvme_vault, "brain_index", "dayz_brain.json")
        if os.path.isfile(legacy_dayz):
            dayz_db = os.path.join(target_primary_vault, "games", "221100_DayZ", "brain_index", "brain.json")
            if self._merge_two_brain_files(
                legacy_dayz,
                dayz_db,
                default_meta={"game_name": "DayZ", "appid": "221100", "safe_id": "221100_DayZ"}
            ):
                try:
                    os.remove(legacy_dayz)
                except OSError:
                    pass

        # 2. Group all discovered game directories across all roots by AppID
        appid_groups = defaultdict(list)
        for root in self.active_vault_roots():
            games_dir = os.path.join(root, "games")
            if not os.path.isdir(games_dir):
                continue
            for entry in os.listdir(games_dir):
                if entry == "custom_SteamLinuxRuntime_4":
                    continue
                full_dir = os.path.join(games_dir, entry)
                if not os.path.isdir(full_dir):
                    continue
                appid = entry.split("_", 1)[0]
                appid_groups[appid].append((root, entry, full_dir))

        merged_count = 0

        # 3. For each AppID, pick the canonical safe_id and merge all variants into primary vault
        for appid, folders in appid_groups.items():
            canonical_safe_id = None
            for _, entry, _ in folders:
                norm = normalize_safe_id(entry)
                if not is_placeholder_safe_id(norm):
                    canonical_safe_id = norm
                    break
            if not canonical_safe_id:
                canonical_safe_id = normalize_safe_id(folders[0][1])

            canonical_dir = os.path.join(target_primary_vault, "games", canonical_safe_id)
            canonical_db = os.path.join(canonical_dir, "brain_index", "brain.json")

            for root, entry, full_dir in folders:
                if os.path.realpath(full_dir) == os.path.realpath(canonical_dir):
                    continue

                src_db = os.path.join(full_dir, "brain_index", "brain.json")
                if os.path.isfile(src_db):
                    self._merge_two_brain_files(src_db, canonical_db)
                    merged_count += 1

                for sub in ("hot_stripe_cache", "shader_cache"):
                    self._merge_directory_contents(
                        os.path.join(full_dir, sub),
                        os.path.join(canonical_dir, sub)
                    )

                # Remove the duplicate or fallback folder after merging into canonical
                try:
                    shutil.rmtree(full_dir)
                except OSError:
                    pass

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
        src_meta = src_data.get("metadata", {})
        dst_meta = dst_data.get("metadata", {})
        meta.update(src_meta)
        meta.update(dst_meta)

        # Keep clean game name if one of the brains had a placeholder name
        src_name = src_meta.get("game_name", "")
        dst_name = dst_meta.get("game_name", "")
        if dst_name.startswith("appid_") and src_name and not src_name.startswith("appid_"):
            meta["game_name"] = src_name
        elif src_name.startswith("appid_") and dst_name and not dst_name.startswith("appid_"):
            meta["game_name"] = dst_name

        if "safe_id" in meta:
            meta["safe_id"] = normalize_safe_id(meta["safe_id"])

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
        Scans active vaults after merging duplicates and returns one entry per AppID.
        """
        self.sync_and_merge_vaults()
        entries_by_appid = {}

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

                appid = safe_id.split("_", 1)[0]
                entry = entries_by_appid.setdefault(appid, {
                    "safe_id": normalize_safe_id(safe_id),
                    "game_name": normalize_safe_id(safe_id).split("_", 1)[-1].replace("_", " "),
                    "appid": appid,
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

                db_path = os.path.join(g_vault, "brain_index", "brain.json")
                if os.path.isfile(db_path):
                    try:
                        with open(db_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        meta = data.get("metadata", {})
                        graph = data.get("graph", {})
                        stripes = data.get("stripe_map", {})
                        stats = meta.get("session_stats", {})

                        if len(graph) >= entry["nodes"] or not entry["brain_path"]:
                            entry["brain_path"] = db_path
                            gname = meta.get("game_name", entry["game_name"])
                            if gname and not gname.startswith("appid_"):
                                entry["game_name"] = gname
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
            entries_by_appid.values(),
            key=lambda e: (len(e["cache_files"]), e["nodes"]),
            reverse=True
        )

    def prewarm_precompiled_caches(self, pinner, safe_id=None, bypass_cooldown=True):
        entries = self.discover_precompiled_caches()
        files_pinned = 0
        bytes_queued = 0

        for entry in entries:
            if safe_id and entry["safe_id"] != safe_id and entry["appid"] != str(safe_id):
                continue
            for fpath in entry["cache_files"]:
                if bypass_cooldown and hasattr(pinner, "cooldown_map"):
                    with pinner.lock:
                        pinner.cooldown_map.pop(fpath, None)
                if pinner.request_pin(fpath):
                    files_pinned += 1
                    try:
                        max_b = getattr(pinner, "max_file_bytes", getattr(pinner, "max_size_bytes", 16777216))
                        bytes_queued += min(os.path.getsize(fpath), max_b)
                    except OSError:
                        pass

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
                                if bypass_cooldown and hasattr(pinner, "cooldown_map"):
                                    with pinner.lock:
                                        pinner.cooldown_map.pop(candidate, None)
                                if pinner.request_pin(candidate):
                                    files_pinned += 1
                                    try:
                                        max_b = getattr(pinner, "max_file_bytes", getattr(pinner, "max_size_bytes", 16777216))
                                        bytes_queued += min(os.path.getsize(candidate), max_b)
                                    except OSError:
                                        pass
                except (OSError, json.JSONDecodeError):
                    pass

        for root in self.active_vault_roots():
            for gdir in ("global_shader_cache", "dxvk_state_cache", "mesa_shader_cache"):
                full_gdir = os.path.join(root, gdir)
                if os.path.isdir(full_gdir) and hasattr(pinner, "prewarm_directory"):
                    files_pinned += pinner.prewarm_directory(full_gdir)

        return files_pinned, bytes_queued
