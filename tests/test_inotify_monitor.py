import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "src"
    )
)

from daemon import InotifyMonitor


class InotifyMonitorTests(unittest.TestCase):
    def test_existing_asset_file_open_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            asset = os.path.join(root, "mod.jar")
            with open(asset, "wb") as file:
                file.write(b"asset")

            monitor = InotifyMonitor(asset_filter=lambda path: path.endswith(".jar"))
            if monitor.fd < 0:
                self.skipTest("inotify is unavailable")
            try:
                monitor.add_tree(root)
                with open(asset, "rb") as file:
                    file.read()

                events = monitor.read_events(timeout=0.5)

                self.assertIn(asset, events)
            finally:
                monitor.close()

    def test_new_asset_file_is_watched_after_directory_create_event(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = InotifyMonitor(asset_filter=lambda path: path.endswith(".jar"))
            if monitor.fd < 0:
                self.skipTest("inotify is unavailable")
            try:
                monitor.add_tree(root)
                asset = os.path.join(root, "new-mod.jar")
                with open(asset, "wb") as file:
                    file.write(b"asset")

                monitor.read_events(timeout=0.5)
                with open(asset, "rb") as file:
                    file.read()

                self.assertIn(asset, monitor.read_events(timeout=0.5))
            finally:
                monitor.close()


if __name__ == "__main__":
    unittest.main()
