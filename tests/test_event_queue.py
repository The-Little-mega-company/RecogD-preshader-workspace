import os
import sys
import struct
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from daemon import IN_OPEN, IN_Q_OVERFLOW, InotifyMonitor
from event_queue import AssetEventQueue


class AssetEventQueueTests(unittest.TestCase):
    def test_batch_larger_than_previous_twelve_event_limit_is_accepted(self):
        processed = []
        event_queue = AssetEventQueue(processed.append, max_pending=32)
        paths = [f"/game/asset-{index}.pbo" for index in range(20)]
        try:
            self.assertEqual(event_queue.submit_batch(paths), len(paths))
        finally:
            event_queue.stop()

        self.assertEqual(processed, paths)

    def test_inotify_kernel_overflow_is_counted_without_losing_next_event(self):
        monitor = InotifyMonitor.__new__(InotifyMonitor)
        monitor.fd = 17
        monitor.wd_to_path = {4: "/game/map"}
        monitor.file_watch_descriptors = {4}
        monitor.directory_watch_descriptors = set()
        monitor.kernel_overflows = 0
        raw = (
            struct.pack("iIII", -1, IN_Q_OVERFLOW, 0, 0)
            + struct.pack("iIII", 4, IN_OPEN, 0, 0)
        )

        with patch("daemon.select.select", return_value=([monitor.fd], [], [])), patch(
            "daemon.os.read", return_value=raw
        ):
            self.assertEqual(monitor.read_events(timeout=0), ["/game/map"])

        self.assertEqual(monitor.kernel_overflows, 1)

    def test_fifo_coalescing_overflow_counts_and_graceful_drain(self):
        started = threading.Event()
        release = threading.Event()
        processed = []

        def process(path):
            processed.append(path)
            if path == "first":
                started.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("test worker was not released")

        event_queue = AssetEventQueue(process, max_pending=1)
        try:
            self.assertEqual(event_queue.submit_batch(["first"]), 1)
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(
                event_queue.submit_batch(["duplicate", "duplicate", "queued", "overflow"]),
                1
            )
            stats = event_queue.get_stats()
            self.assertEqual(stats["events_received"], 5)
            self.assertEqual(stats["events_coalesced"], 1)
            self.assertEqual(stats["events_dropped"], 2)
            self.assertEqual(stats["event_queue_depth"], 1)
        finally:
            release.set()
            event_queue.stop()

        self.assertEqual(processed, ["first", "duplicate"])
        self.assertEqual(event_queue.get_stats()["events_processed"], 2)


if __name__ == "__main__":
    unittest.main()
