#!/usr/bin/env python3
import heapq
import os
import time
from collections import deque

from velocity import PredictiveVelocityTracker
from ram_governor import RamBudgetGovernor

DEFAULT_EXTENSIONS = {
    ".pbo", ".ebo", ".bsa", ".ba2", ".esm", ".esp", ".esl",
    ".pak", ".foz", ".bin", ".upk", ".umap", ".uasset", ".ucas", ".utoc",
    ".vpk", ".bundle", ".assets", ".resS", ".resource",
    ".jar", ".mca", ".mcr", ".mcc", ".dat", ".nbt", ".zip", ".ogg", ".wav", ".bank",
    ".png", ".json", ".mcmeta"
}


def _top_ranked_entries(entries, limit):
    if limit <= 0:
        return []
    if len(entries) <= max(256, limit * 4):
        return sorted(entries.items(), key=lambda item: item[1], reverse=True)[:limit]
    return heapq.nlargest(limit, entries.items(), key=lambda item: item[1])


class RecogEngine:
    def __init__(self, config, pinner, storage, velocity_tracker=None, ram_governor=None):
        self.apply_config(config)
        self.pinner = pinner
        self.storage = storage
        self.velocity_tracker = velocity_tracker or PredictiveVelocityTracker(
            config=config, enable_evdev=False
        )
        self.velocity_tracker.max_lookahead_hops = self.max_hop_depth
        self.ram_governor = ram_governor or getattr(pinner, "ram_governor", None) or RamBudgetGovernor(
            config=config
        )
        self.pinner.ram_governor = self.ram_governor

        self.graph = self.storage.load()
        self.recent_events = deque()
        self.last_save_time = time.monotonic()
        self.last_pinned_file = ""
        self.last_profile = {
            "velocity": 0.0,
            "state": "IDLE",
            "lookahead_hops": 1,
            "combat_burst": False
        }
        self.boost_triggers_fired = 0
        self.lookahead_pins_total = 0

    def apply_config(self, config):
        config = config or {}
        eng_cfg = (config or {}).get("engine", {})
        self.time_window = float(eng_cfg.get("time_window_seconds", 0.75))
        self.min_confidence = int(eng_cfg.get("min_confidence_hits", 2))
        self.stripe_confidence = int(eng_cfg.get("stripe_confidence_hits", 4))
        self.max_preload = int(eng_cfg.get("max_preload_children", 35))
        self.max_file_size_bytes = int(eng_cfg.get("max_file_size_bytes", 16777216))
        self.autosave_interval = float(eng_cfg.get("autosave_interval_seconds", 180))
        cfg_exts = {
            ext.lower().strip() for ext in (config or {}).get("target_extensions", [])
        }
        self.allowed_exts = (cfg_exts | DEFAULT_EXTENSIONS) if cfg_exts else set(DEFAULT_EXTENSIONS)
        self.max_hop_depth = max(1, min(2, int(config.get("max_hop_depth", 2))))
        self.shield_velocity_threshold = float(config.get("shield_velocity_threshold", 18.0))
        self.force_combat_shield = bool(config.get("force_combat_shield", False))

    def is_valid_target(self, filepath):
        _, ext = os.path.splitext(filepath)
        if self.allowed_exts and ext.lower() not in self.allowed_exts:
            game_info = getattr(self.storage, "game_info", {})
            path_parts = os.path.normpath(filepath).lower().split(os.sep)
            has_prism_asset_object = any(
                path_parts[index:index + 2] == ["assets", "objects"]
                for index in range(len(path_parts) - 1)
            )
            if (
                ext
                or game_info.get("name", "").lower() != "prismlauncher"
                or not has_prism_asset_object
            ):
                return False
        return os.path.isfile(filepath)

    def apply_live_config(self, config):
        config = config or {}
        self.max_hop_depth = max(1, min(2, int(config.get("max_hop_depth", self.max_hop_depth))))
        self.velocity_tracker.max_lookahead_hops = self.max_hop_depth
        self.shield_velocity_threshold = float(
            config.get("shield_velocity_threshold", self.shield_velocity_threshold)
        )
        self.force_combat_shield = bool(config.get("force_combat_shield", False))
        self.pinner.apply_live_config(config)

    def get_runtime_stats(self):
        pinner_stats = self.pinner.get_stats() if hasattr(self.pinner, "get_stats") else getattr(self.pinner, "stats", {})
        combined = dict(pinner_stats)
        combined["boost_triggers_fired"] = self.boost_triggers_fired
        combined["lookahead_pins_total"] = self.lookahead_pins_total
        combined["last_pinned_file"] = self.last_pinned_file
        combined["max_hop_depth_used"] = self.max_hop_depth
        if self.velocity_tracker is not None and hasattr(self.velocity_tracker, "get_stats"):
            combined.update(self.velocity_tracker.get_stats())
        if self.ram_governor is not None and hasattr(self.ram_governor, "get_stats"):
            combined.update(self.ram_governor.get_stats())
        return combined

    def process_event(self, filepath):
        if not self.is_valid_target(filepath):
            return self.last_profile

        filepath = self.storage.normalize_asset_path(filepath)

        now = time.monotonic()

        # 1. Update real-time input & asset traversal velocity
        self.velocity_tracker.record_asset_event(filepath, now=now)
        v_profile = self.velocity_tracker.get_adaptive_profile(
            base_max_preload=self.max_preload,
            base_time_window=self.time_window,
            base_min_confidence=self.min_confidence,
            now=now
        )

        # 2. Query RAM Budget Governor to scale or throttle pre-fetching under memory pressure
        effective_max_preload, effective_max_bytes, ram_snap = (
            self.ram_governor.scale_preload_parameters(
                v_profile["max_preload"],
                self.max_file_size_bytes,
                now=now
            )
        )
        self.ram_governor.enforce_budget(self.pinner, now=now)

        effective_min_confidence = v_profile["min_confidence"]
        effective_time_window = v_profile["time_window"]
        event_rate = self.velocity_tracker.get_asset_event_rate(now=now)
        combat_burst = self.force_combat_shield or event_rate >= self.shield_velocity_threshold
        lookahead_hops = min(self.max_hop_depth, v_profile["lookahead_hops"])
        if combat_burst:
            lookahead_hops = 1
            effective_min_confidence = max(3, effective_min_confidence)

        if filepath in self.graph and effective_max_preload > 0:
            children_map = self.graph[filepath]
            ranked_children = _top_ranked_entries(children_map, effective_max_preload)
            pinned_now = 0
            lookahead_pinned_now = 0
            pinned_set = {filepath}
            first_hop_candidates = []

            for child_path, hit_count in ranked_children:
                if hit_count >= effective_min_confidence and child_path != filepath:
                    if self.pinner.request_pin(
                        child_path,
                        max_bytes_override=effective_max_bytes,
                        combat_burst_mode=combat_burst
                    ):
                        pinned_now += 1
                        self.last_pinned_file = os.path.basename(child_path)
                    pinned_set.add(child_path)
                    first_hop_candidates.append((child_path, hit_count))

                    is_frequent_bsa = (
                        os.path.splitext(child_path)[1].lower() == ".bsa"
                        and hit_count >= 4
                    )
                    if not combat_burst and (
                        hit_count >= self.stripe_confidence or is_frequent_bsa
                    ):
                        self.storage.consider_cross_bus_stripe(child_path, weight=hit_count)

            # 3. Predictive Velocity Multi-Hop Corner Lookahead (A -> B -> C)
            if (
                lookahead_hops >= 2
                and v_profile["lookahead_limit"] > 0
                and ram_snap["scale_factor"] >= 0.45
                and first_hop_candidates
            ):
                scaled_lookahead_limit = max(
                    1, int(round(v_profile["lookahead_limit"] * ram_snap["scale_factor"]))
                )
                second_hop_scores = {}
                for child_path, parent_hits in first_hop_candidates[:8]:
                    if child_path in self.graph:
                        for grandchild_path, gc_hits in self.graph[child_path].items():
                            if (
                                gc_hits >= effective_min_confidence
                                and grandchild_path not in pinned_set
                            ):
                                score = parent_hits * gc_hits
                                if score > second_hop_scores.get(grandchild_path, 0):
                                    second_hop_scores[grandchild_path] = score

                ranked_grandchildren = _top_ranked_entries(
                    second_hop_scores, scaled_lookahead_limit
                )
                for gc_path, _ in ranked_grandchildren:
                    if self.pinner.request_pin(
                        gc_path,
                        max_bytes_override=effective_max_bytes,
                        combat_burst_mode=False
                    ):
                        pinned_now += 1
                        lookahead_pinned_now += 1
                        pinned_set.add(gc_path)
                        self.last_pinned_file = os.path.basename(gc_path)

                if lookahead_pinned_now > 0:
                    self.lookahead_pins_total += lookahead_pinned_now
                    self.velocity_tracker.record_corner_boost(1)

            if pinned_now > 0:
                self.boost_triggers_fired += 1

        while self.recent_events and (now - self.recent_events[0][0]) > effective_time_window:
            self.recent_events.popleft()

        seen_parents = set()
        for _, parent_path in self.recent_events:
            if parent_path != filepath and parent_path not in seen_parents:
                self.graph[parent_path][filepath] += 1
                seen_parents.add(parent_path)

        self.recent_events.append((now, filepath))
        self.last_profile = {
            "velocity": v_profile["velocity"],
            "state": v_profile["state"],
            "lookahead_hops": lookahead_hops,
            "combat_burst": combat_burst,
            "event_rate": round(event_rate, 1)
        }

        if (
            (now - self.last_save_time) >= self.autosave_interval
            and self.storage.is_safe_to_autosave()
        ):
            if self.storage.save(self.graph, self.get_runtime_stats()):
                self.last_save_time = now
        return self.last_profile

    def shutdown(self):
        saved = self.storage.save(self.graph, self.get_runtime_stats())
        self.velocity_tracker.shutdown()
        self.pinner.shutdown()
        self.storage.shutdown()
        return saved
