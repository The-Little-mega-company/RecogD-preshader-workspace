#!/usr/bin/env python3
import os
import re
import time
import threading


class RamBudgetGovernor:
    """
    Dynamic memory-pressure listener for Linux.
    Monitors /proc/meminfo and /proc/pressure/memory (PSI) to gracefully scale back
    pre-pinned asset volumes and evict cached pages via POSIX_FADV_DONTNEED when
    background apps or browser windows demand RAM, preventing swap thrashing.
    """

    def __init__(
        self,
        config=None,
        meminfo_path="/proc/meminfo",
        psi_path="/proc/pressure/memory"
    ):
        self.meminfo_path = meminfo_path
        self.psi_path = psi_path

        self.lock = threading.Lock()
        self._last_poll_time = 0.0
        self._last_swap_free_bytes = None
        self.throttle_events = 0
        self.eviction_cycles = 0

        self.apply_config(config)
        self.snapshot = {
            "state": "OPTIMAL",
            "scale_factor": 1.0,
            "dynamic_budget_bytes": int(self.max_budget_mb * 1024 * 1024),
            "mem_total_bytes": 0,
            "mem_available_bytes": 0,
            "avail_ratio": 1.0,
            "swap_free_bytes": 0,
            "swap_delta_bytes": 0,
            "psi_some_avg10": 0.0,
            "psi_full_avg10": 0.0
        }
        self.evaluate(force=True)

    def apply_config(self, config):
        cfg = (config or {}).get("ram_governor", {}) if isinstance(config, dict) else {}
        with self.lock:
            self.enabled = bool(cfg.get("enabled", True))
            self.max_budget_mb = float(cfg.get("max_pinned_budget_mb", 2048.0))
            self.min_free_reserve_mb = float(cfg.get("min_free_reserve_mb", 1536.0))
            self.soft_watermark_ratio = float(cfg.get("soft_available_ratio", 0.28))
            self.hard_watermark_ratio = float(cfg.get("hard_available_ratio", 0.12))
            self.usable_headroom_fraction = float(cfg.get("usable_headroom_fraction", 0.35))
            self.psi_soft_threshold = float(cfg.get("psi_some_avg10_soft", 5.0))
            self.psi_hard_threshold = float(cfg.get("psi_some_avg10_hard", 18.0))
            self.swap_spike_threshold_mb = float(cfg.get("swap_spike_threshold_mb", 32.0))
            self.poll_interval_sec = float(cfg.get("poll_interval_seconds", 0.5))

    def _read_meminfo(self):
        info = {}
        try:
            with open(self.meminfo_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":", 1)
                    if len(parts) != 2:
                        continue
                    key = parts[0].strip()
                    val_parts = parts[1].strip().split()
                    if val_parts and val_parts[0].isdigit():
                        # /proc/meminfo reports values in kB
                        info[key] = int(val_parts[0]) * 1024
        except OSError:
            pass
        return info

    def _read_psi(self):
        some_avg10 = 0.0
        full_avg10 = 0.0
        try:
            if os.path.exists(self.psi_path):
                with open(self.psi_path, "r", encoding="utf-8") as f:
                    for line in f:
                        m = re.search(r"avg10=([0-9.]+)", line)
                        if not m:
                            continue
                        val = float(m.group(1))
                        if line.startswith("some"):
                            some_avg10 = val
                        elif line.startswith("full"):
                            full_avg10 = val
        except (OSError, ValueError):
            pass
        return some_avg10, full_avg10

    def evaluate(self, now=None, force=False):
        if now is None:
            now = time.monotonic()

        with self.lock:
            if not force and (now - self._last_poll_time) < self.poll_interval_sec:
                return dict(self.snapshot)
            self._last_poll_time = now

            max_budget_bytes = int(self.max_budget_mb * 1024 * 1024)
            if not self.enabled:
                self.snapshot["state"] = "DISABLED"
                self.snapshot["scale_factor"] = 1.0
                self.snapshot["dynamic_budget_bytes"] = max_budget_bytes
                return dict(self.snapshot)

            mem = self._read_meminfo()
            psi_some, psi_full = self._read_psi()

            total = mem.get("MemTotal", 0)
            available = mem.get("MemAvailable", 0)
            if available == 0 and total > 0:
                available = mem.get("MemFree", 0) + mem.get("Cached", 0) + mem.get("Buffers", 0)

            swap_free = mem.get("SwapFree", 0)
            swap_delta = 0
            if self._last_swap_free_bytes is not None and swap_free < self._last_swap_free_bytes:
                # Positive swap_delta means SwapFree shrank (pages were swapped out)
                swap_delta = self._last_swap_free_bytes - swap_free
            self._last_swap_free_bytes = swap_free

            if total <= 0:
                return dict(self.snapshot)

            avail_ratio = available / float(total)
            reserve_bytes = int(self.min_free_reserve_mb * 1024 * 1024)
            swap_spike_bytes = int(self.swap_spike_threshold_mb * 1024 * 1024)

            # Calculate usable headroom above safety reserve
            headroom_bytes = max(0, available - reserve_bytes)
            headroom_budget = int(headroom_bytes * self.usable_headroom_fraction)

            # Determine memory pressure scale factor in [0.0, 1.0]
            if (
                available <= reserve_bytes
                or avail_ratio <= self.hard_watermark_ratio
                or psi_some >= self.psi_hard_threshold
                or psi_full >= 5.0
                or swap_delta >= swap_spike_bytes
            ):
                state = "CRITICAL_THRASH_GUARD"
                scale_factor = 0.0
                dynamic_budget = 0
                self.throttle_events += 1
            else:
                # Ratio-based scale
                if avail_ratio >= self.soft_watermark_ratio:
                    ratio_scale = 1.0
                else:
                    span = max(0.01, self.soft_watermark_ratio - self.hard_watermark_ratio)
                    ratio_scale = max(0.0, min(1.0, (avail_ratio - self.hard_watermark_ratio) / span))

                # PSI-based scale
                if psi_some <= self.psi_soft_threshold:
                    psi_scale = 1.0
                else:
                    psi_span = max(1.0, self.psi_hard_threshold - self.psi_soft_threshold)
                    psi_scale = max(0.0, min(1.0, 1.0 - ((psi_some - self.psi_soft_threshold) / psi_span)))

                # Swap activity penalty
                swap_scale = 1.0
                if swap_delta > 0:
                    swap_scale = max(0.1, 1.0 - (swap_delta / float(swap_spike_bytes)))

                scale_factor = round(min(ratio_scale, psi_scale, swap_scale), 3)
                raw_budget = min(max_budget_bytes, headroom_budget)
                dynamic_budget = int(raw_budget * scale_factor)

                if scale_factor >= 0.85:
                    state = "OPTIMAL"
                elif scale_factor >= 0.45:
                    state = "MODERATE"
                else:
                    state = "HIGH_PRESSURE"
                    self.throttle_events += 1

            self.snapshot = {
                "state": state,
                "scale_factor": scale_factor,
                "dynamic_budget_bytes": dynamic_budget,
                "mem_total_bytes": total,
                "mem_available_bytes": available,
                "avail_ratio": round(avail_ratio, 4),
                "swap_free_bytes": swap_free,
                "swap_delta_bytes": swap_delta,
                "psi_some_avg10": round(psi_some, 2),
                "psi_full_avg10": round(psi_full, 2)
            }
            return dict(self.snapshot)

    def scale_preload_parameters(self, requested_children, requested_max_file_bytes, now=None):
        """
        Gracefully scales back pre-pinned asset counts and max per-file size
        when system memory tightens.
        """
        snap = self.evaluate(now=now)
        scale = snap["scale_factor"]
        if scale <= 0.0:
            return 0, 0, snap

        scaled_children = max(1, int(round(requested_children * scale)))
        # Keep file size cap full when scale >= 0.5, scale down for tight memory
        size_scale = 1.0 if scale >= 0.5 else max(0.25, scale * 2.0)
        scaled_file_bytes = max(65536, int(requested_max_file_bytes * size_scale))
        return scaled_children, scaled_file_bytes, snap

    def enforce_budget(self, pinner, now=None):
        """
        Checks current memory pressure and instructs the RamPinner to release
        cached pages back to the kernel if active pinned bytes exceed the dynamic budget.
        """
        snap = self.evaluate(now=now)
        if pinner is None:
            return 0
        budget = snap["dynamic_budget_bytes"]
        active = pinner.get_active_pinned_bytes()
        if active > budget:
            evicted_files, _ = pinner.trim_to_budget(budget)
            if evicted_files > 0:
                with self.lock:
                    self.eviction_cycles += 1
            return evicted_files
        return 0

    def get_stats(self):
        with self.lock:
            snap = dict(self.snapshot)
            return {
                "ram_governor_state": snap["state"],
                "ram_scale_factor": snap["scale_factor"],
                "ram_dynamic_budget_mb": round(snap["dynamic_budget_bytes"] / (1024 * 1024), 2),
                "ram_available_mb": round(snap["mem_available_bytes"] / (1024 * 1024), 2),
                "ram_available_pct": round(snap["avail_ratio"] * 100.0, 1),
                "psi_some_avg10": snap["psi_some_avg10"],
                "ram_throttle_events": self.throttle_events,
                "ram_eviction_cycles": self.eviction_cycles
            }
