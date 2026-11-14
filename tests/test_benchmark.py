import os
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import benchmark
import config_ipc


class BenchmarkTests(unittest.TestCase):
    def test_comparison_table_includes_combined_read_and_write_bandwidth(self):
        output = StringIO()
        report = benchmark.BenchmarkReport(
            nvme=benchmark.DriveResult("NVMe", read_mbps=2000, write_mbps=1000),
            external=benchmark.DriveResult("External SSD", read_mbps=400, write_mbps=50)
        )
        with patch("sys.stdout", output):
            benchmark.print_report(report)

        text = output.getvalue()
        self.assertIn("Drive", text)
        self.assertIn("Read", text)
        self.assertIn("Write", text)
        self.assertIn("Combined Dual-Bus bandwidth: Read 2400.0 MB/s | Write 1050.0 MB/s", text)

    def test_drive_benchmark_uses_direct_io_and_removes_test_file(self):
        with tempfile.TemporaryDirectory() as root:
            result = benchmark.benchmark_drive(
                "Test drive",
                root,
                duration_seconds=0.02,
                chunk_size=1024 * 1024
            )

            if result.error and "Invalid argument" in result.error:
                self.skipTest(f"test filesystem does not support O_DIRECT: {result.error}")
            self.assertTrue(result.successful, result.error)
            self.assertGreater(result.read_mbps, 0)
            self.assertGreater(result.write_mbps, 0)
            self.assertEqual(os.listdir(root), [])

    def test_external_mount_detection_rejects_autofs_and_accepts_real_mount(self):
        with tempfile.TemporaryDirectory() as root:
            mount = os.path.join(root, "external")
            os.makedirs(mount)
            with patch.object(benchmark, "_read_mount_entries") as read_mounts:
                read_mounts.return_value = [benchmark.MountEntry(mount, "autofs")]
                with patch.dict(os.environ, {"PRESHADER_EXT_MOUNT": mount}):
                    self.assertEqual(benchmark._find_external_mount(), "")

                read_mounts.return_value = [benchmark.MountEntry(mount, "ext4")]
                with patch.dict(os.environ, {"PRESHADER_EXT_MOUNT": mount}):
                    self.assertEqual(benchmark._find_external_mount(), mount)

    def test_benchmark_persists_write_cap_only_when_both_drives_succeed(self):
        with tempfile.TemporaryDirectory() as root:
            live_path = os.path.join(root, "live.json")
            persistent_path = os.path.join(root, "config.json")
            legacy_path = os.path.join(root, "legacy.json")
            nvme = benchmark.DriveResult("NVMe", read_mbps=2000, write_mbps=1000)
            external = benchmark.DriveResult("External SSD", read_mbps=400, write_mbps=50)
            with (
                patch.object(config_ipc, "LIVE_CONFIG_PATH", live_path),
                patch.object(config_ipc, "PERSISTENT_CONFIG_PATH", persistent_path),
                patch.object(config_ipc, "LEGACY_CONFIG_PATH", legacy_path),
                patch.object(benchmark, "benchmark_drive", side_effect=[nvme, external]),
                patch.object(benchmark, "_find_external_mount", return_value="/ssd")
            ):
                report = benchmark.run_benchmark()
                self.assertEqual(report.calibrated_write_mbps, 40.0)
                self.assertEqual(config_ipc.read_persistent_config()["max_write_mbps"], 40.0)

                with patch.object(benchmark, "benchmark_drive", side_effect=[nvme]):
                    with patch.object(benchmark, "_find_external_mount", return_value=""):
                        report = benchmark.run_benchmark()
                self.assertEqual(report.calibrated_write_mbps, 0.0)
                self.assertEqual(config_ipc.read_persistent_config()["max_write_mbps"], 40.0)


if __name__ == "__main__":
    unittest.main()
