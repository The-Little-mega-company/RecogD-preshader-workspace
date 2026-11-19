#!/usr/bin/env python3
import os
import time
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor


class RamPinner:
    def __init__(
        self,
        cooldown_sec=5.0,
        max_size_bytes=16777216,
        external_mount="/mnt/preshader",
        preload_callback=None,
        ram_governor=None
    ):
        self.cooldown_sec = cooldown_sec
        self.max_size_bytes = max_size_bytes
        self.external_mount = os.path.abspath(os.path.expanduser(external_mount))
        self.preload_callback = preload_callback
        self.ram_governor = ram_governor
        self.cooldown_map = {}
        self.pinned_ledger = OrderedDict()  # filepath -> pinned_bytes (LRU order)
        self.active_pinned_bytes = 0
        self.lock = threading.Lock()

        self.nvme_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="RecogNVMe")
        self.ext_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="RecogExtSSD")
        self.reset_stats()

    def reset_stats(self):
        with self.lock:
            self.pinned_ledger.clear()
            self.active_pinned_bytes = 0
            self.stats = {
                "total_pinned_files": 0,
                "total_pinned_bytes": 0,
                "nvme_pinned_files": 0,
                "nvme_pinned_bytes": 0,
                "ext_pinned_files": 0,
                "ext_pinned_bytes": 0,
                "active_pinned_files": 0,
                "active_pinned_bytes": 0,
                "evicted_files": 0,
                "evicted_bytes": 0,
                "skipped_cooldown": 0,
                "skipped_ram_pressure": 0,
                "errors": 0
            }

    def get_active_pinned_bytes(self):
        with self.lock:
            return self.active_pinned_bytes

    def is_external_path(self, filepath):
        return os.path.abspath(filepath).startswith(self.external_mount)

    def _open_readonly(self, filepath):
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOATIME"):
            try:
                return os.open(filepath, flags | os.O_NOATIME)
            except PermissionError:
                return os.open(filepath, flags)
        return os.open(filepath, flags)

    def _evict_file_sync(self, filepath, length):
        try:
            if not os.path.isfile(filepath):
                return False
            fd = self._open_readonly(filepath)
            try:
                if hasattr(os, "POSIX_FADV_DONTNEED"):
                    os.posix_fadvise(fd, 0, length, os.POSIX_FADV_DONTNEED)
                return True
            finally:
                os.close(fd)
        except (OSError, PermissionError):
            return False

    def trim_to_budget(self, target_budget_bytes):
        """
        Gracefully releases oldest pre-pinned assets from the Linux page cache
        using POSIX_FADV_DONTNEED until active_pinned_bytes <= target_budget_bytes.
        """
        target_budget_bytes = max(0, int(target_budget_bytes))
        to_evict = []

        with self.lock:
            while self.pinned_ledger and self.active_pinned_bytes > target_budget_bytes:
                old_path, old_bytes = self.pinned_ledger.popitem(last=False)
                self.active_pinned_bytes = max(0, self.active_pinned_bytes - old_bytes)
                self.cooldown_map.pop(old_path, None)
                to_evict.append((old_path, old_bytes))
            self.stats["active_pinned_files"] = len(self.pinned_ledger)
            self.stats["active_pinned_bytes"] = self.active_pinned_bytes

        evicted_files = 0
        evicted_bytes = 0
        for old_path, old_bytes in to_evict:
            self._evict_file_sync(old_path, old_bytes)
            evicted_files += 1
            evicted_bytes += old_bytes

        if evicted_files > 0:
            with self.lock:
                self.stats["evicted_files"] += evicted_files
                self.stats["evicted_bytes"] += evicted_bytes

        return evicted_files, evicted_bytes

    def _pin_file_sync(self, filepath, is_external, max_bytes_cap):
        try:
            st = os.stat(filepath)
            if st.st_size == 0:
                return

            pin_length = min(st.st_size, max_bytes_cap)

            if self.ram_governor is not None:
                snap = self.ram_governor.evaluate()
                budget = snap["dynamic_budget_bytes"]
                if snap["scale_factor"] <= 0.0 or budget <= 0:
                    with self.lock:
                        self.stats["skipped_ram_pressure"] += 1
                    self.trim_to_budget(0)
                    return
                if (self.get_active_pinned_bytes() + pin_length) > budget:
                    target = max(0, budget - pin_length)
                    self.trim_to_budget(target)

            fd = self._open_readonly(filepath)
            try:
                if is_external and hasattr(os, "POSIX_FADV_SEQUENTIAL"):
                    os.posix_fadvise(fd, 0, pin_length, os.POSIX_FADV_SEQUENTIAL)
                if hasattr(os, "POSIX_FADV_WILLNEED"):
                    os.posix_fadvise(fd, 0, pin_length, os.POSIX_FADV_WILLNEED)

                callback_stats = None
                with self.lock:
                    prev_bytes = self.pinned_ledger.pop(filepath, 0)
                    self.pinned_ledger[filepath] = pin_length
                    self.active_pinned_bytes = max(0, self.active_pinned_bytes - prev_bytes) + pin_length

                    self.stats["total_pinned_files"] += 1
                    self.stats["total_pinned_bytes"] += pin_length
                    if is_external:
                        self.stats["ext_pinned_files"] += 1
                        self.stats["ext_pinned_bytes"] += pin_length
                    else:
                        self.stats["nvme_pinned_files"] += 1
                        self.stats["nvme_pinned_bytes"] += pin_length
                    self.stats["active_pinned_files"] = len(self.pinned_ledger)
                    self.stats["active_pinned_bytes"] = self.active_pinned_bytes
                    if self.preload_callback is not None:
                        callback_stats = dict(self.stats)

                if self.preload_callback is not None and callback_stats is not None:
                    self.preload_callback(filepath, pin_length, is_external, callback_stats)
            finally:
                os.close(fd)
        except (OSError, PermissionError):
            with self.lock:
                self.stats["errors"] += 1

    def request_pin(self, filepath, max_bytes_override=None):
        now = time.monotonic()
        max_bytes_cap = (
            min(self.max_size_bytes, int(max_bytes_override))
            if max_bytes_override is not None
            else self.max_size_bytes
        )

        if self.ram_governor is not None:
            _, scaled_file_bytes, snap = self.ram_governor.scale_preload_parameters(
                1, max_bytes_cap, now=now
            )
            if snap["scale_factor"] <= 0.0 or snap["dynamic_budget_bytes"] <= 0:
                with self.lock:
                    self.stats["skipped_ram_pressure"] += 1
                self.trim_to_budget(0)
                return False
            max_bytes_cap = min(max_bytes_cap, scaled_file_bytes)

        with self.lock:
            last_pin = self.cooldown_map.get(filepath, 0.0)
            if (now - last_pin) < self.cooldown_sec:
                self.stats["skipped_cooldown"] += 1
                return False

            self.cooldown_map[filepath] = now
            if len(self.cooldown_map) > 25000:
                cutoff = now - (self.cooldown_sec * 2)
                self.cooldown_map = {k: v for k, v in self.cooldown_map.items() if v > cutoff}

        if self.is_external_path(filepath):
            self.ext_executor.submit(self._pin_file_sync, filepath, True, max_bytes_cap)
        else:
            self.nvme_executor.submit(self._pin_file_sync, filepath, False, max_bytes_cap)
        return True

    def prewarm_directory(self, directory_path):
        expanded = os.path.expanduser(directory_path)
        if not expanded or not os.path.exists(expanded):
            return 0
        count = 0
        for root, _, files in os.walk(expanded):
            for fname in files:
                if fname.endswith((".foz", ".dxvk-cache", ".spv", ".bin", ".idx", ".cache")):
                    fpath = os.path.join(root, fname)
                    if self.request_pin(fpath):
                        count += 1
        return count

    def shutdown(self):
        self.nvme_executor.shutdown(wait=False)
        self.ext_executor.shutdown(wait=False)
