#!/usr/bin/env python3
import os
import time
from collections import deque

from velocity import PredictiveVelocityTracker
from ram_governor import RamBudgetGovernor


class RecogEngine:
    def __init__(self, config, pinner, storage, velocity_tracker=None, ram_governor=None):
        self.apply_config(config)
        self.pinner = pinner
        self.storage = storage
        self.velocity_tracker = velocity_tracker or PredictiveVelocityTracker(
            config=config, enable_evdev=False
        )
        self.ram_governor = ram_governor or getattr(pinner, "ram_governor", None) or RamBudgetGovernor(
            config=config
        )
        if getattr(self.pinner, "ram_governor", None) is None:
            self.pinner.ram_governor = self.ram_governor

        self.graph = self.storage.load()
        self.recent_events = deque()
        self.last_save_time = time.monotonic()
        self.boost_triggers_fired = 0
        self.lookahead_pins_total = 0

    def apply_config(self, config):
        eng_cfg = (config or {}).get("engine", {})
        self.time_window = float(eng_cfg.get("time_window_seconds", 0.75))
        self.min_confidence = int(eng_cfg.get("min_confidence_hits", 2))
        self.stripe_confidence = int(eng_cfg.get("stripe_confidence_hits", 4))
        self.max_preload = int(eng_cfg.get("max_preload_children", 35))
        self.max_file_size_bytes = int(eng_cfg.get("max_file_size_bytes", 16777216))
        self.autosave_interval = float(eng_cfg.get("autosave_interval_seconds", 180))
        self.allowed_exts = set(
            ext.lower().strip() for ext in (config or {}).get("target_extensions", [])
        )

    def is_valid_target(self, filepath):
        _, ext = os.path.splitext(filepath)
        if self.allowed_exts and ext.lower() not in self.allowed_exts:
            return False
        return os.path.isfile(filepath)

    def get_runtime_stats(self):
        combined = dict(self.pinner.stats)
        combined["boost_triggers_fired"] = self.boost_triggers_fired
        combined["lookahead_pins_total"] = self.lookahead_pins_total
        if self.velocity_tracker is not None:
            combined.update(self.velocity_tracker.get_stats())
        if self.ram_governor is not None:
            combined.update(self.ram_governor.get_stats())
        return combined

    def process_event(self, filepath):
        if not self.is_valid_target(filepath):
            return

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

        if filepath in self.graph and effective_max_preload > 0:
            children_map = self.graph[filepath]
            ranked_children = sorted(
                children_map.items(),
                key=lambda x: x[1],
                reverse=True
            )
            pinned_now = 0
            lookahead_pinned_now = 0
            pinned_set = {filepath}
            first_hop_candidates = []

            for idx, (child_path, hit_count) in enumerate(ranked_children[:effective_max_preload]):
                if hit_count >= effective_min_confidence and child_path != filepath:
                    target_path = self.storage.resolve_dual_bus_path(child_path, idx)
                    if self.pinner.request_pin(target_path, max_bytes_override=effective_max_bytes):
                        pinned_now += 1
                    pinned_set.add(child_path)
                    first_hop_candidates.append((child_path, hit_count))

                    if hit_count >= self.stripe_confidence:
                        self.storage.consider_cross_bus_stripe(child_path)

            # 3. Predictive Velocity Multi-Hop Corner Lookahead (A -> B -> C)
            if (
                v_profile["lookahead_hops"] >= 2
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

                ranked_grandchildren = sorted(
                    second_hop_scores.items(),
                    key=lambda x: x[1],
                    reverse=True
                )
                for l_idx, (gc_path, _) in enumerate(ranked_grandchildren[:scaled_lookahead_limit]):
                    target_gc = self.storage.resolve_dual_bus_path(gc_path, l_idx)
                    if self.pinner.request_pin(target_gc, max_bytes_override=effective_max_bytes):
                        pinned_now += 1
                        lookahead_pinned_now += 1
                        pinned_set.add(gc_path)

                if lookahead_pinned_now > 0:
                    self.lookahead_pins_total += lookahead_pinned_now
                    self.velocity_tracker.record_corner_boost(1)

            if pinned_now > 0:
                self.boost_triggers_fired += 1
                vel_tag = (
                    f" | Vel:{v_profile['velocity']:.2f} ({v_profile['state']}, +{lookahead_pinned_now} corner-lookahead)"
                    if lookahead_pinned_now > 0 or v_profile["velocity"] >= 0.25
                    else ""
                )
                ram_tag = (
                    f" | RAM:{ram_snap['state']}({int(ram_snap['scale_factor'] * 100)}%)"
                    if ram_snap["scale_factor"] < 1.0
                    else ""
                )
                print(
                    f"[BOOST #{self.boost_triggers_fired}] "
                    f"{os.path.basename(filepath)} -> Pre-pinned {pinned_now} assets to RAM"
                    f"{vel_tag}{ram_tag}",
                    flush=True
                )

        while self.recent_events and (now - self.recent_events[0][0]) > effective_time_window:
            self.recent_events.popleft()

        seen_parents = set()
        for _, parent_path in self.recent_events:
            if parent_path != filepath and parent_path not in seen_parents:
                self.graph[parent_path][filepath] += 1
                seen_parents.add(parent_path)

        self.recent_events.append((now, filepath))

        if (now - self.last_save_time) >= self.autosave_interval:
            self.storage.save(self.graph, self.get_runtime_stats())
            self.last_save_time = now

    def shutdown(self):
        self.storage.shutdown()
        self.storage.save(self.graph, self.get_runtime_stats())
