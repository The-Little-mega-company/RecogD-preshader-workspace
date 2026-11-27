#!/usr/bin/env python3
import os
import time
import threading
from collections import OrderedDict

POSIX_FADV_WILLNEED = getattr(os, "POSIX_FADV_WILLNEED", 3)
POSIX_FADV_DONTNEED = getattr(os, "POSIX_FADV_DONTNEED", 4)


class RamBudgetGovernor:
    def __init__(self, max_cache_mb=1536, min_free_mb=4500):
        self.max_budget_bytes = int(max_cache_mb * 1024 * 1024)
        self.min_free_bytes = int(min_free_mb * 1024 * 1024)
        self.last_check = 0.0
        self.cached_avail = 8 * 1024 * 1024 * 1024

    def update_limits(self, max_cache_mb, min_free_mb):
        self.max_budget_bytes = int(max(256, min(4096, int(max_cache_mb))) * 1024 * 1024)
        self.min_free_bytes = int(max(1024, min(8192, int(min_free_mb))) * 1024 * 1024)

    def _read_meminfo_bytes(self, key):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith(key + ":"):
                        parts = line.split()
                        return int(parts[1]) * 1024
        except Exception:
            pass
        return 0

    def get_available_ram(self):
        now = time.monotonic()
        if (now - self.last_check) > 0.8:
            avail = self._read_meminfo_bytes("MemAvailable")
            if avail > 0:
                self.cached_avail = avail
            self.last_check = now
        return self.cached_avail

    def check_pressure(self, current_pinned_bytes):
        avail = self.get_available_ram()
        under_pressure = (avail < self.min_free_bytes) or (current_pinned_bytes > self.max_budget_bytes)
        return under_pressure, avail


class AssetPinner:
    def __init__(self, storage, max_file_mb=16):
        self.storage = storage
        # Strict 16MB file cap prevents massive DayZ .pbo or Skyrim .bsa files from choking RAM
        self.max_file_bytes = max_file_mb * 1024 * 1024
        self.governor = RamBudgetGovernor(max_cache_mb=1536, min_free_mb=4500)

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
            self.governor.update_limits(
                cfg.get("max_cache_mb", 1536),
                cfg.get("min_free_ram_mb", 4500)
            )
            self.paused = bool(cfg.get("pause_engine", False))
            self._evict_oldest_until_safe(needed_bytes=0)

    def enforce_memory_safety(self):
        """Called every second by daemon loop to release RAM before game can crash."""
        with self.lock:
            return self._evict_oldest_until_safe(needed_bytes=0)

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

    def _evict_oldest_until_safe(self, needed_bytes=0):
        evicted_now = 0
        while self.active_pins:
            under_pressure, _ = self.governor.check_pressure(self.current_pinned_bytes + needed_bytes)
            if not under_pressure:
                break

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
        with self.lock:
            if self.paused:
                return False
            if (now - self.window_start) >= 1.0:
                self.window_start = now
                self.bytes_pinned_in_window = 0

            current_cap = (8 * 1024 * 1024) if combat_burst_mode else self.max_pin_bps_normal
            if self.bytes_pinned_in_window >= current_cap:
                return False

            if filepath in self.active_pins:
                self.active_pins.move_to_end(filepath)
                return False

            self.toggle_counter += 1
            target_path = self.storage.resolve_dual_bus_path(filepath, self.toggle_counter)

        try:
            if not os.path.isfile(target_path):
                return False
            st = os.stat(target_path)
            effective_max_file = max_bytes_override if max_bytes_override is not None else self.max_file_bytes
            if st.st_size <= 0 or st.st_size > effective_max_file:
                return False

            if combat_burst_mode and st.st_size > (6 * 1024 * 1024):
                return False

            with self.lock:
                self._evict_oldest_until_safe(needed_bytes=st.st_size)
                under_pressure, _ = self.governor.check_pressure(self.current_pinned_bytes + st.st_size)
                if under_pressure:
                    return False

            flags = os.O_RDONLY
            try:
                fd = os.open(target_path, flags | getattr(os, "O_NOATIME", 0))
            except PermissionError:
                fd = os.open(target_path, flags)

            try:
                os.posix_fadvise(fd, 0, st.st_size, POSIX_FADV_WILLNEED)
            finally:
                os.close(fd)

            is_external = (
                self.storage.external_online
                and target_path.startswith(self.storage.external_mount)
            )
            bus_type = "sata" if is_external else "nvme"

            with self.lock:
                self.active_pins[target_path] = (st.st_size, bus_type)
                self.current_pinned_bytes += st.st_size
                self.bytes_pinned_in_window += st.st_size
                if is_external:
                    self.sata_files += 1
                    self.sata_bytes += st.st_size
                else:
                    self.nvme_files += 1
                    self.nvme_bytes += st.st_size

            return True
        except OSError:
            return False

    def get_stats(self):
        with self.lock:
            avail_mb = round(self.governor.get_available_ram() / (1024 * 1024), 2)
            budget_mb = round(self.governor.max_budget_bytes / (1024 * 1024), 2)
            active_mb = round(self.current_pinned_bytes / (1024 * 1024), 2)
            return {
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
