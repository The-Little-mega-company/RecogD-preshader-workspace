#!/usr/bin/env python3
"""Benchmark RecogEngine event handling without disk pinning or system load noise."""

import argparse
from contextlib import nullcontext
import math
import os
import statistics
import sys
import time
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

import engine
from engine import RecogEngine


class BenchmarkStorage:
    def __init__(self, graph):
        self.graph = graph

    def load(self):
        return self.graph

    @staticmethod
    def normalize_asset_path(filepath):
        return os.path.realpath(filepath)

    @staticmethod
    def is_safe_to_autosave():
        return False

    @staticmethod
    def consider_cross_bus_stripe(_filepath, weight=0):
        pass

    @staticmethod
    def shutdown():
        pass


class BenchmarkPinner:
    ram_governor = None
    paused = False

    @staticmethod
    def request_pin(_filepath, max_bytes_override=None, combat_burst_mode=False):
        return False

    @staticmethod
    def apply_live_config(_config):
        pass

    @staticmethod
    def shutdown():
        pass


class BenchmarkGovernor:
    @staticmethod
    def scale_preload_parameters(requested_children, max_file_bytes, now=None):
        return requested_children, max_file_bytes, {"scale_factor": 1.0}

    @staticmethod
    def enforce_budget(_pinner, now=None):
        return 0

    @staticmethod
    def get_stats():
        return {}


def percentile(samples, fraction):
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def full_sort_top_entries(entries, limit):
    return sorted(entries.items(), key=lambda item: item[1], reverse=True)[:limit]


def benchmark(fanout, top_k, iterations, warmup, legacy_ranking=False):
    trigger = os.path.abspath("/synthetic/trigger.pbo")
    graph = {
        trigger: {
            f"/synthetic/assets/{index:05}.pbo": (index % 17) + 1
            for index in range(fanout)
        }
    }
    config = {
        "engine": {
            "min_confidence_hits": 1,
            "stripe_confidence_hits": fanout + 1,
            "max_preload_children": top_k,
            "autosave_interval_seconds": float("inf")
        },
        "velocity": {
            "enabled": True,
            "sample_window_seconds": 1.0,
            "max_lookahead_hops": 1
        },
        "max_hop_depth": 1,
        "target_extensions": [".pbo"]
    }
    ranking_patch = (
        patch.object(engine, "_top_ranked_entries", full_sort_top_entries)
        if legacy_ranking else nullcontext()
    )
    with ranking_patch:
        runtime = RecogEngine(
            config,
            BenchmarkPinner(),
            BenchmarkStorage(graph),
            ram_governor=BenchmarkGovernor()
        )
        runtime.is_valid_target = lambda _filepath: True

        try:
            for _ in range(warmup):
                runtime.process_event(trigger)
            samples = []
            started_all = time.perf_counter()
            for _ in range(iterations):
                started = time.perf_counter_ns()
                runtime.process_event(trigger)
                samples.append((time.perf_counter_ns() - started) / 1_000_000)
            elapsed = time.perf_counter() - started_all
            return (
                statistics.median(samples),
                percentile(samples, 0.95),
                iterations / elapsed
            )
        finally:
            runtime.velocity_tracker.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fanouts", nargs="+", type=int, default=[8, 128, 2048, 8192])
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    args = parser.parse_args()
    if args.iterations < 1 or args.warmup < 0 or args.top_k < 1:
        parser.error("iterations and top-k must be positive; warmup cannot be negative")

    print(
        "fanout  full_p50_ms  full_p95_ms  top_k_p50_ms  top_k_p95_ms  "
        "median_speedup  top_k_events/s"
    )
    for fanout in args.fanouts:
        if fanout < 1:
            parser.error("fanouts must be positive")
        full_sort, full_sort_p95, _ = benchmark(
            fanout, args.top_k, args.iterations, args.warmup, legacy_ranking=True
        )
        top_k, top_k_p95, throughput = benchmark(
            fanout, args.top_k, args.iterations, args.warmup
        )
        speedup = full_sort / top_k if top_k else 0.0
        print(
            f"{fanout:6d}  {full_sort:11.3f}  {full_sort_p95:11.3f}  "
            f"{top_k:12.3f}  {top_k_p95:11.3f}  {speedup:14.2f}x  "
            f"{throughput:14.1f}"
        )


if __name__ == "__main__":
    main()
