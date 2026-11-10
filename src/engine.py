#!/usr/bin/env python3
import os
import time
from collections import deque


class RecogEngine:
    def __init__(self, config, pinner, storage):
        eng_cfg = config.get("engine", {})
        self.time_window = eng_cfg.get("time_window_seconds", 0.75)
        self.min_confidence = eng_cfg.get("min_confidence_hits", 2)
        self.stripe_confidence = eng_cfg.get("stripe_confidence_hits", 4)
        self.max_preload = eng_cfg.get("max_preload_children", 35)
        self.autosave_interval = eng_cfg.get("autosave_interval_seconds", 180)

        self.allowed_exts = set(
            ext.lower().strip() for ext in config.get("target_extensions", [])
        )

        self.pinner = pinner
        self.storage = storage
        self.graph = self.storage.load()
        self.recent_events = deque()
        self.last_save_time = time.monotonic()
        self.boost_triggers_fired = 0

    def is_valid_target(self, filepath):
        _, ext = os.path.splitext(filepath)
        if self.allowed_exts and ext.lower() not in self.allowed_exts:
            return False
        return os.path.isfile(filepath)

    def process_event(self, filepath):
        if not self.is_valid_target(filepath):
            return

        now = time.monotonic()

        if filepath in self.graph:
            children_map = self.graph[filepath]
            ranked_children = sorted(
                children_map.items(),
                key=lambda x: x[1],
                reverse=True
            )
            pinned_now = 0
            for idx, (child_path, hit_count) in enumerate(ranked_children[:self.max_preload]):
                if hit_count >= self.min_confidence and child_path != filepath:
                    target_path = self.storage.resolve_dual_bus_path(child_path, idx)
                    if self.pinner.request_pin(target_path):
                        pinned_now += 1

                    if hit_count >= self.stripe_confidence:
                        self.storage.consider_cross_bus_stripe(child_path)

            if pinned_now > 0:
                self.boost_triggers_fired += 1
                print(
                    f"[BOOST #{self.boost_triggers_fired}] "
                    f"{os.path.basename(filepath)} -> Pre-pinned {pinned_now} assets to RAM",
                    flush=True
                )

        while self.recent_events and (now - self.recent_events[0][0]) > self.time_window:
            self.recent_events.popleft()

        seen_parents = set()
        for _, parent_path in self.recent_events:
            if parent_path != filepath and parent_path not in seen_parents:
                self.graph[parent_path][filepath] += 1
                seen_parents.add(parent_path)

        self.recent_events.append((now, filepath))

        if (now - self.last_save_time) >= self.autosave_interval:
            self.storage.save(self.graph, self.pinner.stats)
            self.last_save_time = now

    def shutdown(self):
        self.storage.shutdown()
        self.storage.save(self.graph, self.pinner.stats)
