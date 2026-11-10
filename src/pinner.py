#!/usr/bin/env python3
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor


class RamPinner:
    def __init__(self, cooldown_sec=5.0, max_size_bytes=16777216, external_mount="/mnt/preshader"):
        self.cooldown_sec = cooldown_sec
        self.max_size_bytes = max_size_bytes
        self.external_mount = os.path.abspath(os.path.expanduser(external_mount))
        self.cooldown_map = {}
        self.lock = threading.Lock()

        self.nvme_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="RecogNVMe")
        self.ext_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="RecogExtSSD")
        self.reset_stats()

    def reset_stats(self):
        with self.lock:
            self.stats = {
                "total_pinned_files": 0,
                "total_pinned_bytes": 0,
                "nvme_pinned_files": 0,
                "nvme_pinned_bytes": 0,
                "ext_pinned_files": 0,
                "ext_pinned_bytes": 0,
                "skipped_cooldown": 0,
                "errors": 0
            }

    def is_external_path(self, filepath):
        return os.path.abspath(filepath).startswith(self.external_mount)

    def _pin_file_sync(self, filepath, is_external):
        try:
            st = os.stat(filepath)
            if st.st_size == 0:
                return

            pin_length = min(st.st_size, self.max_size_bytes)
            flags = os.O_RDONLY | os.O_NONBLOCK
            if hasattr(os, "O_NOATIME"):
                try:
                    fd = os.open(filepath, flags | os.O_NOATIME)
                except PermissionError:
                    fd = os.open(filepath, flags)
            else:
                fd = os.open(filepath, flags)

            try:
                if is_external and hasattr(os, "POSIX_FADV_SEQUENTIAL"):
                    os.posix_fadvise(fd, 0, pin_length, os.POSIX_FADV_SEQUENTIAL)
                os.posix_fadvise(fd, 0, pin_length, os.POSIX_FADV_WILLNEED)

                with self.lock:
                    self.stats["total_pinned_files"] += 1
                    self.stats["total_pinned_bytes"] += pin_length
                    if is_external:
                        self.stats["ext_pinned_files"] += 1
                        self.stats["ext_pinned_bytes"] += pin_length
                    else:
                        self.stats["nvme_pinned_files"] += 1
                        self.stats["nvme_pinned_bytes"] += pin_length
            finally:
                os.close(fd)
        except (OSError, PermissionError):
            with self.lock:
                self.stats["errors"] += 1

    def request_pin(self, filepath):
        now = time.monotonic()
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
            self.ext_executor.submit(self._pin_file_sync, filepath, True)
        else:
            self.nvme_executor.submit(self._pin_file_sync, filepath, False)
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
