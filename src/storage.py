#!/usr/bin/env python3
import os
import json
import time
import hashlib
import threading
import math
from collections import deque, defaultdict
from collections.abc import Mapping, MutableMapping

POSIX_FADV_WILLNEED = getattr(os, "POSIX_FADV_WILLNEED", 3)
PORTABLE_PATH_TOKENS = ("@GAME/", "@WORKSHOP/", "@SHADERCACHE/")


def is_portable_asset_path(filepath):
    path = os.fsdecode(os.fspath(filepath)).replace("\\", "/")
    return any(path.startswith(token) for token in PORTABLE_PATH_TOKENS)


def normalize_asset_path(filepath):
    raw_path = os.fsdecode(os.fspath(filepath))
    if is_portable_asset_path(raw_path):
        return raw_path.replace("\\", "/")
    path = os.path.realpath(os.path.expanduser(raw_path))
    home = os.path.realpath(os.path.expanduser("~"))
    parts = os.path.normpath(path).split(os.sep)
    if len(parts) > 3 and parts[1] == "home" and parts[2] != os.path.basename(home):
        rebased_path = os.path.join(home, *parts[3:])
        if os.path.exists(rebased_path):
            return os.path.realpath(rebased_path)
    return path


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
        max_stripe_file_bytes=8388608,
        edge_decay_half_life_days=30.0,
        require_external_mount=False
    ):
        self.game_info = game_info
        self.safe_id = game_info["safe_id"]
        path_roots = (
            ("@GAME/", game_info.get("install_dir") or game_info.get("game_dir", "")),
            ("@WORKSHOP/", game_info.get("workshop_dir", "")),
            ("@SHADERCACHE/", game_info.get("shader_dir") or game_info.get("shadercache_dir", ""))
        )
        self.asset_path_roots = {
            token: os.path.realpath(os.path.expanduser(path))
            for token, path in path_roots
            if path
        }
        self.vault_root = os.path.realpath(os.path.expanduser(vault_root))
        self.external_mount = os.path.realpath(os.path.expanduser(external_mount))
        self.requires_external_mount = bool(require_external_mount)

        # Only stripe if vault_root is genuinely on the external SSD (never stripe into ~/.cache!)
        try:
            self.vault_on_external = (
                os.path.commonpath((self.vault_root, self.external_mount))
                == self.external_mount
            )
        except ValueError:
            self.vault_on_external = False

        self.external_mount_device = None
        self.external_mount_info = None
        if self.requires_external_mount:
            try:
                mount_info = self._get_external_mount_info()
                if mount_info and mount_info[0] != "autofs":
                    self.external_mount_info = mount_info[:3]
                    self.external_mount_device = os.stat(self.external_mount).st_dev
            except OSError:
                pass

        self.external_online = (
            os.path.isdir(self.external_mount)
            and self.vault_on_external
            and (not self.requires_external_mount or self.is_external_mount_available())
        )

        self.game_vault_dir = os.path.join(self.vault_root, "games", self.safe_id)
        self.brain_dir = os.path.join(self.game_vault_dir, "brain_index")
        self.stripe_cache_dir = os.path.join(self.game_vault_dir, "hot_stripe_cache")
        self.shader_cache_dir = os.path.join(self.game_vault_dir, "shader_cache")
        self.db_path = os.path.join(self.brain_dir, "brain.json")

        if self.is_external_mount_available():
            try:
                os.makedirs(self.brain_dir, exist_ok=True)
                os.makedirs(self.stripe_cache_dir, exist_ok=True)
                os.makedirs(self.shader_cache_dir, exist_ok=True)
            except OSError as exc:
                if not self.requires_external_mount:
                    raise
                self.external_online = False
                print(f"[PRESHADER-STORAGE] External vault unavailable: {exc}")

        self.max_parent_nodes = max_parent_nodes
        self.max_children_per_node = max_children_per_node
        self.enable_striping = (
            enable_striping
            and self.vault_on_external
            and (self.requires_external_mount or self.external_online)
        )
        self.max_stripe_file_bytes = max_stripe_file_bytes
        self.edge_decay_half_life_seconds = max(1.0, float(edge_decay_half_life_days) * 86400.0)
        self.last_decay_epoch = time.time()

        self.sensor = SystemLoadSensor(min_free_ram_mb=4500)
        self.current_velocity = 0.0

        self.stripe_map = {}
        self.stripe_queue = deque(maxlen=2)
        self.queued_set = set()
        self.read_ahead_queue = deque()
        self.read_ahead_set = set()
        self.dropped_burst_writes = 0

        self.lock = threading.Lock()
        self.running = True
        self.worker_thread = threading.Thread(target=self._stripe_worker_loop, name="RecogSafeWriter", daemon=True)
        self.worker_thread.start()

    def update_live_velocity(self, vel):
        self.current_velocity = vel

    def normalize_asset_path(self, filepath):
        return normalize_asset_path(filepath)

    def resolve_asset_path(self, filepath):
        path = os.fsdecode(os.fspath(filepath))
        for token, root in self.asset_path_roots.items():
            if path.startswith(token):
                relative_path = path[len(token):].replace("/", os.sep)
                return os.path.realpath(os.path.join(root, relative_path))
        return normalize_asset_path(path)

    def portable_asset_path(self, filepath):
        path = self.resolve_asset_path(filepath)
        for token, root in sorted(
            self.asset_path_roots.items(),
            key=lambda item: len(item[1]),
            reverse=True
        ):
            try:
                if os.path.commonpath((path, root)) != root:
                    continue
            except ValueError:
                continue
            relative_path = os.path.relpath(path, root)
            suffix = "" if relative_path == "." else relative_path.replace(os.sep, "/")
            return f"{token}{suffix}"
        return path

    def _get_external_mount_info(self):
        mount_path = (
            self.external_mount
            .replace("\\", "\\134")
            .replace(" ", "\\040")
            .replace("\t", "\\011")
            .replace("\n", "\\012")
        )
        try:
            with open("/proc/self/mountinfo", "r", encoding="utf-8") as mountinfo:
                for line in mountinfo:
                    fields = line.split()
                    if len(fields) < 10 or fields[4] != mount_path or "-" not in fields:
                        continue
                    separator = fields.index("-")
                    if separator + 2 >= len(fields):
                        continue
                    return (
                        fields[separator + 1],
                        fields[separator + 2],
                        fields[2],
                        "ro" in fields[5].split(",")
                    )
        except OSError:
            return None
        return None

    def is_external_mount_available(self):
        if not self.requires_external_mount:
            return True
        if not self.vault_on_external:
            self.external_online = False
            return False
        try:
            mount_info = self._get_external_mount_info()
            if not mount_info or mount_info[0] == "autofs" or mount_info[3]:
                self.external_online = False
                return False
            if self.external_mount_info is None:
                self.external_mount_info = mount_info[:3]
            if mount_info[:3] != self.external_mount_info:
                self.external_online = False
                return False
            mount_stat = os.stat(self.external_mount)
            if self.external_mount_device is None:
                self.external_mount_device = mount_stat.st_dev
            if mount_stat.st_dev != self.external_mount_device:
                self.external_online = False
                return False
        except OSError:
            self.external_online = False
            return False
        self.external_online = True
        return True

    def is_safe_to_autosave(self):
        return (
            self.is_external_mount_available()
            and self.sensor.get_allowed_write_bps(self.current_velocity) > 0
        )

    def load(self):
        graph = defaultdict(lambda: defaultdict(int))
        if not self.is_external_mount_available():
            return graph
        if not os.path.exists(self.db_path):
            return graph

        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            metadata = payload.get("metadata", {})
            self.last_decay_epoch = float(
                metadata.get(
                    "graph_weight_decay_at_epoch",
                    metadata.get("updated_at_epoch", os.path.getmtime(self.db_path))
                )
            )
            raw_edges = payload.get("graph", {})
            if isinstance(raw_edges, Mapping):
                for parent, children in raw_edges.items():
                    if isinstance(children, Mapping):
                        normalized_parent = self.resolve_asset_path(parent)
                        for child, count in children.items():
                            normalized_child = self.resolve_asset_path(child)
                            graph[normalized_parent][normalized_child] = max(
                                graph[normalized_parent][normalized_child], int(count)
                            )

            stripe_data = payload.get("stripe_map", {})
            if isinstance(stripe_data, Mapping):
                for orig, mirrored in stripe_data.items():
                    if os.path.exists(mirrored):
                        self.stripe_map[self.resolve_asset_path(orig)] = os.path.realpath(mirrored)
        except Exception:
            pass
        self._decay_graph_weights(graph, time.time())
        return self.prune_graph(graph)

    def resolve_dual_bus_path(self, filepath, toggle_index):
        filepath = normalize_asset_path(filepath)
        if not self.enable_striping or not self.is_external_mount_available():
            return filepath
        with self.lock:
            mirrored = self.stripe_map.get(filepath)
        if mirrored and (toggle_index % 2 == 1) and os.path.exists(mirrored):
            return mirrored
        return filepath

    def consider_cross_bus_stripe(self, filepath, weight=0):
        abs_path = normalize_asset_path(filepath)
        if os.path.splitext(abs_path)[1].lower() == ".bsa" and weight >= 4:
            with self.lock:
                if abs_path not in self.read_ahead_set and len(self.read_ahead_queue) < 32:
                    self.read_ahead_queue.append(abs_path)
                    self.read_ahead_set.add(abs_path)

        if not self.enable_striping or not self.is_external_mount_available():
            return
        try:
            if os.path.commonpath((abs_path, self.external_mount)) == self.external_mount:
                return
        except ValueError:
            pass

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
            read_ahead = False
            with self.lock:
                if self.read_ahead_queue:
                    abs_path = self.read_ahead_queue.popleft()
                    read_ahead = True
                elif self.stripe_queue:
                    abs_path = self.stripe_queue.popleft()

            if not abs_path:
                time.sleep(0.25)
                continue

            if read_ahead:
                try:
                    self._read_ahead_asset_header(abs_path)
                finally:
                    with self.lock:
                        self.read_ahead_set.discard(abs_path)
            else:
                try:
                    self._mirror_file_rate_limited(abs_path)
                finally:
                    with self.lock:
                        self.queued_set.discard(abs_path)

    def _read_ahead_asset_header(self, abs_path):
        if not os.path.isfile(abs_path):
            return
        try:
            pin_bytes = min(os.path.getsize(abs_path), 32 * 1024 * 1024)
            if pin_bytes <= 0:
                return
            try:
                fd = os.open(abs_path, os.O_RDONLY | getattr(os, "O_NOATIME", 0))
            except PermissionError:
                fd = os.open(abs_path, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, pin_bytes, POSIX_FADV_WILLNEED)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _mirror_file_rate_limited(self, abs_path):
        if not self.is_external_mount_available() or not os.path.isfile(abs_path):
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
                    if not self.is_external_mount_available():
                        return
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

            if not self.is_external_mount_available():
                return
            os.replace(tmp_dest, dest_path)
            with self.lock:
                self.stripe_map[abs_path] = dest_path
        except OSError:
            pass

    def prune_graph(self, graph):
        pruned = defaultdict(lambda: defaultdict(int))
        eligible_parents = []
        for parent, children in graph.items():
            if isinstance(children, Mapping):
                eligible_children = [
                    (child, int(count))
                    for child, count in children.items()
                    if int(count) >= 2
                ]
                if eligible_children:
                    eligible_parents.append((parent, eligible_children))

        scored_parents = sorted(
            eligible_parents,
            key=lambda item: sum(count for _, count in item[1]),
            reverse=True
        )[:self.max_parent_nodes]
        for parent, children in scored_parents:
            top_children = sorted(
                children, key=lambda item: item[1], reverse=True
            )[:self.max_children_per_node]
            for child, count in top_children:
                pruned[parent][child] = count
        return pruned

    def _decay_graph_weights(self, graph, now_epoch):
        elapsed = max(0.0, now_epoch - self.last_decay_epoch)
        if elapsed <= 0.0:
            return
        factor = math.pow(0.5, elapsed / self.edge_decay_half_life_seconds)
        for parent in list(graph):
            children = graph[parent]
            for child in list(children):
                decayed_weight = int(round(int(children[child]) * factor))
                if decayed_weight < 2:
                    del children[child]
                else:
                    children[child] = decayed_weight
            if not children:
                del graph[parent]
        self.last_decay_epoch = now_epoch

    def save(self, graph, pinner_stats=None):
        if not self.is_external_mount_available():
            return False
        try:
            os.makedirs(self.brain_dir, exist_ok=True)
        except OSError as exc:
            print(f"[PRESHADER-STORAGE] Brain directory unavailable: {exc}")
            return False
        now_epoch = time.time()
        normalized_graph = defaultdict(lambda: defaultdict(int))
        for parent, children in graph.items():
            if isinstance(children, Mapping):
                normalized_parent = self.resolve_asset_path(parent)
                for child, count in children.items():
                    normalized_child = self.resolve_asset_path(child)
                    normalized_graph[normalized_parent][normalized_child] = max(
                        normalized_graph[normalized_parent][normalized_child], int(count)
                    )
        self._decay_graph_weights(normalized_graph, now_epoch)
        if isinstance(graph, MutableMapping):
            graph.clear()
            graph.update(normalized_graph)
            graph_for_save = graph
        else:
            graph_for_save = normalized_graph
        pruned_graph = self.prune_graph(graph_for_save)
        if isinstance(graph, MutableMapping):
            graph.clear()
            graph.update(pruned_graph)

        with self.lock:
            active_stripes = dict(self.stripe_map)
            dropped = self.dropped_burst_writes

        stats_out = dict(pinner_stats or {})
        stats_out["burst_writes_shed"] = dropped
        stats_out["io_stall_avg10"] = round(self.sensor.io_stall_avg10, 2)

        portable_graph = {
            self.portable_asset_path(parent): {
                self.portable_asset_path(child): count
                for child, count in children.items()
            }
            for parent, children in pruned_graph.items()
            if isinstance(children, Mapping)
        }
        portable_stripes = {
            self.portable_asset_path(original): mirrored
            for original, mirrored in active_stripes.items()
        }
        payload = {
            "metadata": {
                "game_name": self.game_info["name"],
                "appid": self.game_info["appid"],
                "safe_id": self.safe_id,
                "updated_at_epoch": int(now_epoch),
                "path_format": "portable-v1",
                "graph_weight_decay_at_epoch": now_epoch,
                "total_parent_nodes": len(pruned_graph),
                "total_edges": sum(len(c) for c in pruned_graph.values() if isinstance(c, Mapping)),
                "total_striped_assets": len(active_stripes),
                "session_stats": stats_out
            },
            "stripe_map": portable_stripes,
            "graph": portable_graph
        }

        tmp_path = f"{self.db_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            if not self.is_external_mount_available():
                return False
            os.replace(tmp_path, self.db_path)
            return True
        except OSError as exc:
            print(f"[PRESHADER-STORAGE] Brain save failed: {exc}")
            return False

    def shutdown(self):
        self.running = False
        self.worker_thread.join(timeout=1.0)
