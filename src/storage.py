#!/usr/bin/env python3
import os
import json
import time
import shutil
import hashlib
import threading
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor


class BrainStorage:
    def __init__(
        self,
        vault_root,
        game_info,
        external_mount="/mnt/preshader",
        max_parent_nodes=25000,
        max_children_per_node=12,
        enable_striping=True,
        max_stripe_file_bytes=16777216
    ):
        self.game_info = game_info
        self.safe_id = game_info["safe_id"]
        self.external_mount = os.path.abspath(os.path.expanduser(external_mount))
        self.external_online = os.path.ismount(self.external_mount) or os.access(self.external_mount, os.W_OK)

        self.game_vault_dir = os.path.join(os.path.expanduser(vault_root), "games", self.safe_id)
        self.brain_dir = os.path.join(self.game_vault_dir, "brain_index")
        self.stripe_cache_dir = os.path.join(self.game_vault_dir, "hot_stripe_cache")
        self.shader_cache_dir = os.path.join(self.game_vault_dir, "shader_cache")
        self.db_path = os.path.join(self.brain_dir, "brain.json")

        os.makedirs(self.brain_dir, exist_ok=True)
        os.makedirs(self.stripe_cache_dir, exist_ok=True)
        os.makedirs(self.shader_cache_dir, exist_ok=True)

        self.max_parent_nodes = max_parent_nodes
        self.max_children_per_node = max_children_per_node
        self.enable_striping = enable_striping and self.external_online
        self.max_stripe_file_bytes = max_stripe_file_bytes

        self.stripe_map = {}
        self.pending_stripes = set()
        self.last_stripe_time = 0.0
        self.stripe_cooldown = 0.08

        self.lock = threading.Lock()
        self.mirror_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RecogStripeThrottle")

    def load(self):
        graph = defaultdict(lambda: defaultdict(int))
        if not os.path.exists(self.db_path):
            return graph

        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            raw_edges = payload.get("graph", {})
            if isinstance(raw_edges, Mapping):
                for parent, children in raw_edges.items():
                    if isinstance(children, Mapping):
                        sorted_children = sorted(children.items(), key=lambda x: int(x[1]), reverse=True)[:self.max_children_per_node]
                        for child, count in sorted_children:
                            graph[parent][child] = int(count)

            stripe_data = payload.get("stripe_map", {})
            if isinstance(stripe_data, Mapping):
                for orig, mirrored in stripe_data.items():
                    if os.path.exists(mirrored):
                        self.stripe_map[orig] = mirrored
        except Exception:
            pass
        return graph

    def resolve_dual_bus_path(self, filepath, toggle_index):
        if not self.enable_striping:
            return filepath
        with self.lock:
            mirrored = self.stripe_map.get(filepath)
        if mirrored and (toggle_index % 2 == 1) and os.path.exists(mirrored):
            return mirrored
        return filepath

    def consider_cross_bus_stripe(self, filepath):
        if not self.enable_striping:
            return
        abs_path = os.path.abspath(filepath)
        if abs_path.startswith(self.external_mount):
            return

        now = time.time()
        with self.lock:
            if (now - self.last_stripe_time) < self.stripe_cooldown:
                return
            if abs_path in self.stripe_map or abs_path in self.pending_stripes:
                return
            self.pending_stripes.add(abs_path)
            self.last_stripe_time = now

        self.mirror_executor.submit(self._mirror_asset_worker, abs_path)

    def _mirror_asset_worker(self, abs_path):
        try:
            if not os.path.isfile(abs_path):
                return
            st = os.stat(abs_path)
            if st.st_size == 0 or st.st_size > self.max_stripe_file_bytes:
                return

            path_hash = hashlib.blake2b(abs_path.encode("utf-8"), digest_size=8).hexdigest()
            dest_name = f"{path_hash}_{os.path.basename(abs_path)}"
            dest_path = os.path.join(self.stripe_cache_dir, dest_name)

            if not os.path.exists(dest_path):
                tmp_dest = f"{dest_path}.tmp"
                with open(abs_path, "rb") as fsrc, open(tmp_dest, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst, length=65536)
                os.replace(tmp_dest, dest_path)

            with self.lock:
                self.stripe_map[abs_path] = dest_path
        except OSError:
            pass
        finally:
            with self.lock:
                self.pending_stripes.discard(abs_path)

    def prune_graph(self, graph):
        pruned = defaultdict(lambda: defaultdict(int))
        scored_parents = sorted(
            graph.items(),
            key=lambda item: sum(item[1].values()),
            reverse=True
        )[:self.max_parent_nodes]

        for parent, children in scored_parents:
            if isinstance(children, Mapping):
                top_children = sorted(children.items(), key=lambda x: x[1], reverse=True)[:self.max_children_per_node]
                for child, count in top_children:
                    if count > 1 or len(top_children) <= 4:
                        pruned[parent][child] = count
        return pruned

    def save(self, graph, pinner_stats=None):
        os.makedirs(self.brain_dir, exist_ok=True)
        pruned_graph = self.prune_graph(graph)

        with self.lock:
            active_stripes = dict(self.stripe_map)

        payload = {
            "metadata": {
                "game_name": self.game_info["name"],
                "appid": self.game_info["appid"],
                "safe_id": self.safe_id,
                "updated_at_epoch": int(time.time()),
                "total_parent_nodes": len(pruned_graph),
                "total_edges": sum(len(c) for c in pruned_graph.values() if isinstance(c, Mapping)),
                "total_striped_assets": len(active_stripes),
                "session_stats": pinner_stats or {}
            },
            "stripe_map": active_stripes,
            "graph": {k: dict(v) for k, v in pruned_graph.items() if isinstance(v, Mapping)}
        }

        tmp_path = f"{self.db_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.db_path)
        except Exception:
            pass

    def shutdown(self):
        self.mirror_executor.shutdown(wait=False)
