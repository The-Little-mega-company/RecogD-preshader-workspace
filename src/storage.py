#!/usr/bin/env python3
import os
import json
import time
import hashlib
import threading
from collections import deque, defaultdict
from collections.abc import Mapping


class SystemLoadSensor:
    def __init__(self, min_free_ram_mb=4500):
        self.min_free_bytes = min_free_ram_mb * 1024 * 1024
        self.shield_vel_threshold = 18.0
        self.max_write_bps = 6 * 1024 * 1024
        self.force_shield = False

        self.last_poll = 0.0
        self.avail_ram_bytes = 8 * 1024 * 1024 * 1024
        self.io_stall_avg10 = 0.0
        self.cpu_stall_avg10 = 0.0

    def update_limits(self, min_free_mb, shield_vel, max_write_mbps, force_shield):
        self.min_free_bytes = int(min_free_mb * 1024 * 1024)
        self.shield_vel_threshold = float(shield_vel)
        self.max_write_bps = int(max_write_mbps * 1024 * 1024)
        self.force_shield = bool(force_shield)

    def poll(self):
        now = time.monotonic()
        if (now - self.last_poll) < 1.0:
            return
        self.last_poll = now

        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        self.avail_ram_bytes = int(line.split()[1]) * 1024
                        break
        except Exception:
            pass

        try:
            if os.path.exists("/proc/pressure/io"):
                with open("/proc/pressure/io", "r", encoding="utf-8") as f:
                    first = f.readline()
                    for token in first.split():
                        if token.startswith("avg10="):
                            self.io_stall_avg10 = float(token.split("=")[1])
                            break
        except Exception:
            pass

        try:
            if os.path.exists("/proc/pressure/cpu"):
                with open("/proc/pressure/cpu", "r", encoding="utf-8") as f:
                    first = f.readline()
                    for token in first.split():
                        if token.startswith("avg10="):
                            self.cpu_stall_avg10 = float(token.split("=")[1])
                            break
        except Exception:
            pass

    def get_allowed_write_bps(self, current_velocity=0.0):
        self.poll()
        if (
            self.force_shield
            or current_velocity >= self.shield_vel_threshold
            or self.io_stall_avg10 >= 2.0
            or self.cpu_stall_avg10 >= 12.0
            or self.avail_ram_bytes < self.min_free_bytes
            or self.max_write_bps <= 0
        ):
            return 0
        return self.max_write_bps


class BrainStorage:
    def __init__(
        self,
        vault_root,
        game_info,
        external_mount="/mnt/preshader",
        max_parent_nodes=12000,
        max_children_per_node=8,
        enable_striping=True,
        max_stripe_file_bytes=8388608
    ):
        self.game_info = game_info
        self.safe_id = game_info["safe_id"]
        self.vault_root = os.path.abspath(os.path.expanduser(vault_root))
        self.external_mount = os.path.abspath(os.path.expanduser(external_mount))

        # Only stripe if vault_root is genuinely on the external SSD (never stripe into ~/.cache!)
        self.external_online = (
            os.path.isdir(self.external_mount)
            and self.vault_root.startswith(self.external_mount)
            and not self.vault_root.startswith(os.path.expanduser("~/.cache"))
        )

        self.game_vault_dir = os.path.join(self.vault_root, "games", self.safe_id)
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

        self.sensor = SystemLoadSensor(min_free_ram_mb=4500)
        self.current_velocity = 0.0

        self.stripe_map = {}
        self.stripe_queue = deque(maxlen=2)
        self.queued_set = set()
        self.dropped_burst_writes = 0

        self.lock = threading.Lock()
        self.running = True
        self.worker_thread = threading.Thread(target=self._stripe_worker_loop, name="RecogSafeWriter", daemon=True)
        self.worker_thread.start()

    def update_live_velocity(self, vel):
        self.current_velocity = vel

    def is_safe_to_autosave(self):
        return self.sensor.get_allowed_write_bps(self.current_velocity) > 0

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
                        sorted_children = sorted(
                            children.items(), key=lambda x: int(x[1]), reverse=True
                        )[:self.max_children_per_node]
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

        if self.sensor.get_allowed_write_bps(self.current_velocity) == 0:
            with self.lock:
                self.dropped_burst_writes += 1
            return

        with self.lock:
            if abs_path in self.stripe_map or abs_path in self.queued_set:
                return
            if len(self.stripe_queue) >= 2:
                self.dropped_burst_writes += 1
                return
            self.stripe_queue.append(abs_path)
            self.queued_set.add(abs_path)

    def _stripe_worker_loop(self):
        try:
            os.nice(15)
        except Exception:
            pass

        while self.running:
            abs_path = None
            with self.lock:
                if self.stripe_queue:
                    abs_path = self.stripe_queue.popleft()

            if not abs_path:
                time.sleep(0.25)
                continue

            try:
                self._mirror_file_rate_limited(abs_path)
            finally:
                with self.lock:
                    self.queued_set.discard(abs_path)

    def _mirror_file_rate_limited(self, abs_path):
        if not os.path.isfile(abs_path):
            return
        try:
            st = os.stat(abs_path)
            if st.st_size <= 0 or st.st_size > self.max_stripe_file_bytes:
                return

            path_hash = hashlib.blake2b(abs_path.encode("utf-8"), digest_size=8).hexdigest()
            dest_name = f"{path_hash}_{os.path.basename(abs_path)}"
            dest_path = os.path.join(self.stripe_cache_dir, dest_name)

            if os.path.exists(dest_path):
                with self.lock:
                    self.stripe_map[abs_path] = dest_path
                return

            tmp_dest = f"{dest_path}.tmp"
            chunk_size = 16384

            with open(abs_path, "rb") as fsrc, open(tmp_dest, "wb") as fdst:
                while self.running:
                    allowed_bps = self.sensor.get_allowed_write_bps(self.current_velocity)
                    if allowed_bps == 0:
                        fdst.close()
                        try:
                            os.remove(tmp_dest)
                        except OSError:
                            pass
                        with self.lock:
                            self.dropped_burst_writes += 1
                        return

                    t0 = time.monotonic()
                    buf = fsrc.read(chunk_size)
                    if not buf:
                        break
                    fdst.write(buf)

                    target_duration = len(buf) / float(allowed_bps)
                    elapsed = time.monotonic() - t0
                    if target_duration > elapsed:
                        time.sleep(min(0.2, target_duration - elapsed))

            os.replace(tmp_dest, dest_path)
            with self.lock:
                self.stripe_map[abs_path] = dest_path
        except OSError:
            pass

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
            dropped = self.dropped_burst_writes

        stats_out = dict(pinner_stats or {})
        stats_out["burst_writes_shed"] = dropped
        stats_out["io_stall_avg10"] = round(self.sensor.io_stall_avg10, 2)

        payload = {
            "metadata": {
                "game_name": self.game_info["name"],
                "appid": self.game_info["appid"],
                "safe_id": self.safe_id,
                "updated_at_epoch": int(time.time()),
                "total_parent_nodes": len(pruned_graph),
                "total_edges": sum(len(c) for c in pruned_graph.values() if isinstance(c, Mapping)),
                "total_striped_assets": len(active_stripes),
                "session_stats": stats_out
            },
            "stripe_map": active_stripes,
            "graph": {k: dict(v) for k, v in pruned_graph.items() if isinstance(v, Mapping)}
        }

        tmp_path = f"{self.db_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp_path, self.db_path)
        except Exception:
            pass

    def shutdown(self):
        self.running = False
