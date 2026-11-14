#!/usr/bin/env python3
import os
import time
import threading
from collections import OrderedDict

from ram_governor import RamBudgetGovernor

POSIX_FADV_WILLNEED = getattr(os, "POSIX_FADV_WILLNEED", 3)
POSIX_FADV_DONTNEED = getattr(os, "POSIX_FADV_DONTNEED", 4)
LARGE_ASSET_PIN_BYTES = 32 * 1024 * 1024


class AssetPinner:
    def __init__(self, storage, max_file_mb=16, config=None, ram_governor=None):
        self.storage = storage
        # Strict 16MB file cap prevents massive DayZ .pbo or Skyrim .bsa files from choking RAM
        self.max_file_bytes = max_file_mb * 1024 * 1024
        self.ram_governor = ram_governor or RamBudgetGovernor(config=config)

        self.active_pins = OrderedDict()
        self.current_pinned_bytes = 0

        self.window_start = time.monotonic()
        self.bytes_pinned_in_window = 0
        self.max_pin_bps_normal = 24 * 1024 * 1024

        self.nvme_files = 0
        self.nvme_bytes = 0
        self.sata_files = 0
        self.sata_bytes = 0
        self.evicted_files = 0
        self.evicted_bytes = 0
        self.toggle_counter = 0
        self.paused = False
        self.lock = threading.Lock()

    @property
    def stats(self):
        return self.get_stats()

    def apply_live_config(self, cfg):
        with self.lock:
            self.ram_governor.update_limits(
                cfg.get("app_cache_cap_mb", cfg.get("max_cache_mb", 1536)),
                cfg.get("min_free_ram_mb", 4500),
                allow_over_configured_cap=cfg.get("app_cache_cap_mb") is not None
            )
            self.paused = bool(cfg.get("pause_engine", False))
        self.ram_governor.enforce_budget(self)

    def enforce_memory_safety(self):
        """Called every second by daemon loop to release RAM before game can crash."""
        return self.ram_governor.enforce_budget(self)

    def flush_all_pins(self):
        with self.lock:
            count = 0
            while self.active_pins:
                old_path, (old_size, _) = self.active_pins.popitem(last=False)
                self.current_pinned_bytes = max(0, self.current_pinned_bytes - old_size)
                self.evicted_files += 1
                self.evicted_bytes += old_size
                count += 1
                try:
                    if os.path.isfile(old_path):
                        fd = os.open(old_path, os.O_RDONLY | getattr(os, "O_NOATIME", 0))
                        try:
                            os.posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)
                        finally:
                            os.close(fd)
                except OSError:
                    pass
            return count

    def get_active_pinned_bytes(self):
        with self.lock:
            return self.current_pinned_bytes

    def trim_to_budget(self, budget_bytes):
        evicted_now = 0
        with self.lock:
            while self.active_pins and self.current_pinned_bytes > budget_bytes:
                old_path, (old_size, _) = self.active_pins.popitem(last=False)
                self.current_pinned_bytes = max(0, self.current_pinned_bytes - old_size)
                self.evicted_files += 1
                self.evicted_bytes += old_size
                evicted_now += 1
                try:
                    if os.path.isfile(old_path):
                        fd = os.open(old_path, os.O_RDONLY | getattr(os, "O_NOATIME", 0))
                        try:
                            os.posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)
                        finally:
                            os.close(fd)
                except OSError:
                    pass
        return evicted_now

    def request_pin(self, filepath, max_bytes_override=None, combat_burst_mode=False):
        return self.pin_file(
            filepath,
            combat_burst_mode=combat_burst_mode,
            max_bytes_override=max_bytes_override
        )

    def pin_file(self, filepath, combat_burst_mode=False, max_bytes_override=None):
        now = time.monotonic()
        self.ram_governor.enforce_budget(self)
        with self.lock:
            if self.paused:
                return False
            if (now - self.window_start) >= 1.0:
                self.window_start = now
                self.bytes_pinned_in_window = 0

            current_cap = (8 * 1024 * 1024) if combat_burst_mode else self.max_pin_bps_normal
            if self.bytes_pinned_in_window >= current_cap:
                return False

            self.toggle_counter += 1
            target_path = self.storage.resolve_dual_bus_path(filepath, self.toggle_counter)
            if target_path in self.active_pins:
                self.active_pins.move_to_end(target_path)
                return False

        try:
            if not os.path.isfile(target_path):
                return False
            st = os.stat(target_path)
            file_size = st.st_size
            if file_size <= 0:
                return False

            if file_size > self.max_file_bytes:
                pin_bytes = min(file_size, LARGE_ASSET_PIN_BYTES)
            else:
                effective_max_file = (
                    max_bytes_override
                    if max_bytes_override is not None
                    else self.max_file_bytes
                )
                if file_size > effective_max_file:
                    return False
                pin_bytes = file_size

            with self.lock:
                snapshot = self.ram_governor.evaluate(now=now)
                if (
                    snapshot["scale_factor"] <= 0.0
                    or (
                        snapshot["mem_available_bytes"] > 0
                        and snapshot["mem_available_bytes"] <= int(
                            self.ram_governor.min_free_reserve_mb * 1024 * 1024
                        )
                    )
                    or self.current_pinned_bytes + pin_bytes > snapshot["dynamic_budget_bytes"]
                ):
                    return False

            flags = os.O_RDONLY
            try:
                fd = os.open(target_path, flags | getattr(os, "O_NOATIME", 0))
            except PermissionError:
                fd = os.open(target_path, flags)

            try:
                os.posix_fadvise(fd, 0, pin_bytes, POSIX_FADV_WILLNEED)
            finally:
                os.close(fd)

            try:
                is_external = (
                    self.storage.external_online
                    and os.path.commonpath(
                        (target_path, self.storage.external_mount)
                    ) == self.storage.external_mount
                )
            except ValueError:
                is_external = False
            bus_type = "sata" if is_external else "nvme"

            with self.lock:
                self.active_pins[target_path] = (pin_bytes, bus_type)
                self.current_pinned_bytes += pin_bytes
                self.bytes_pinned_in_window += pin_bytes
                if is_external:
                    self.sata_files += 1
                    self.sata_bytes += pin_bytes
                else:
                    self.nvme_files += 1
                    self.nvme_bytes += pin_bytes

            return True
        except OSError:
            return False

    def get_stats(self):
        with self.lock:
            snapshot = self.ram_governor.evaluate()
            avail_mb = round(snapshot["mem_available_bytes"] / (1024 * 1024), 2)
            budget_mb = round(snapshot["dynamic_budget_bytes"] / (1024 * 1024), 2)
            active_mb = round(self.current_pinned_bytes / (1024 * 1024), 2)
            stats = {
                "total_pinned_files": self.nvme_files + self.sata_files,
                "total_pinned_mb": round((self.nvme_bytes + self.sata_bytes) / (1024 * 1024), 2),
                "nvme_files": self.nvme_files,
                "nvme_mb": round(self.nvme_bytes / (1024 * 1024), 2),
                "sata_files": self.sata_files,
                "sata_mb": round(self.sata_bytes / (1024 * 1024), 2),
                "active_ram_pinned_mb": active_mb,
                "ram_budget_ceiling_mb": budget_mb,
                "system_avail_ram_mb": avail_mb,
                "governor_evicted_files": self.evicted_files,
                "governor_evicted_mb": round(self.evicted_bytes / (1024 * 1024), 2)
            }
            stats.update(self.ram_governor.get_stats())
            return stats

    def shutdown(self):
        self.flush_all_pins()
