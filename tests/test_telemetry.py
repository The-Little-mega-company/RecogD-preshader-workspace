import json
import os
import signal
import subprocess
import tempfile
import time
import unittest

from src.telemetry import RuntimeTelemetry


class RuntimeTelemetryTests(unittest.TestCase):
    def test_records_status_and_recent_preloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "runtime.json")
            telemetry = RuntimeTelemetry(path)
            telemetry.set_status("running", "dayz", "Watching assets.")

            for index in range(105):
                telemetry.record_preload(
                    f"/assets/asset-{index}.pbo",
                    1024 + index,
                    index % 2 == 0,
                    {"total_pinned_files": index + 1}
                )

            with open(path, "r", encoding="utf-8") as telemetry_file:
                data = json.load(telemetry_file)

            self.assertEqual(data["status"], "running")
            self.assertEqual(data["profile"], "dayz")
            self.assertEqual(data["stats"]["total_pinned_files"], 105)
            self.assertEqual(len(data["preloaded"]), 100)
            self.assertEqual(data["preloaded"][0]["name"], "asset-5.pbo")
            self.assertEqual(data["preloaded"][-1]["bus"], "External SSD")

    def test_restores_preload_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "runtime.json")
            initial = RuntimeTelemetry(path)
            initial.record_preload(
                "/assets/terrain.pbo",
                2048,
                False,
                {"total_pinned_files": 1}
            )

            restored = RuntimeTelemetry(path)

            self.assertEqual(restored.data["preloaded"][0]["name"], "terrain.pbo")


class PinnerTelemetryTests(unittest.TestCase):
    def test_records_successful_cache_hint(self):
        import threading

        from src.pinner import RamPinner

        with tempfile.TemporaryDirectory() as directory:
            asset_path = os.path.join(directory, "asset.pbo")
            with open(asset_path, "wb") as asset_file:
                asset_file.write(b"asset data")
            completed = threading.Event()
            events = []

            def record_preload(path, size, is_external, stats):
                events.append((path, size, is_external, stats))
                completed.set()

            pinner = RamPinner(preload_callback=record_preload)
            try:
                self.assertTrue(pinner.request_pin(asset_path))
                self.assertTrue(completed.wait(2))
            finally:
                pinner.shutdown()

            self.assertEqual(events[0][0], asset_path)
            self.assertEqual(events[0][1], len(b"asset data"))
            self.assertFalse(events[0][2])
            self.assertEqual(events[0][3]["total_pinned_files"], 1)


class DaemonLifecycleTests(unittest.TestCase):
    def test_daemon_reports_running_and_stopped(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as directory:
            watch_path = os.path.join(directory, "watch")
            os.makedirs(watch_path)
            config_path = os.path.join(directory, "config.json")
            telemetry_path = os.path.join(directory, "runtime.json")
            brain_path = os.path.join(directory, "brain.json")
            profile = {
                "watch_directories": [watch_path],
                "shader_vault_dirs": [],
                "brain_db_path": brain_path,
                "fallback_db_path": brain_path,
                "stripe_cache_dir": os.path.join(directory, "stripes")
            }
            config = {
                "engine": {"max_parent_nodes": 10},
                "external_drive": {
                    "mount_point": directory,
                    "enable_cross_bus_striping": False
                },
                "target_extensions": [".pbo"],
                "profiles": {"test": profile}
            }
            with open(config_path, "w", encoding="utf-8") as config_file:
                json.dump(config, config_file)

            process = subprocess.Popen(
                [
                    os.sys.executable,
                    os.path.join(project_root, "src", "daemon.py"),
                    "--profile", "test",
                    "--config", config_path,
                    "--telemetry", telemetry_path
                ],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        output = process.stdout.read() if process.stdout else ""
                        self.fail(f"Daemon exited before running:\n{output}")
                    try:
                        with open(telemetry_path, "r", encoding="utf-8") as telemetry_file:
                            status = json.load(telemetry_file).get("status")
                    except (FileNotFoundError, json.JSONDecodeError):
                        status = None
                    if status == "running":
                        break
                    time.sleep(0.05)
                self.assertEqual(status, "running")

                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                with open(telemetry_path, "r", encoding="utf-8") as telemetry_file:
                    final_status = json.load(telemetry_file).get("status")
                self.assertEqual(final_status, "stopped")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()


if __name__ == "__main__":
    unittest.main()
