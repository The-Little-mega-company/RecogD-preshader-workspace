import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from src.pinner import AssetPinner
from src.storage import BrainStorage
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
        with tempfile.TemporaryDirectory() as directory:
            asset_path = os.path.join(directory, "asset.pbo")
            with open(asset_path, "wb") as asset_file:
                asset_file.write(b"asset data")

            storage = BrainStorage(
                vault_root=directory,
                game_info={"appid": "221100", "name": "DayZ", "safe_id": "221100_DayZ"},
                external_mount=directory,
                enable_striping=False
            )
            pinner = AssetPinner(storage, max_file_mb=16)
            pinner.apply_live_config({"max_cache_mb": 1536, "min_free_ram_mb": 1024})
            try:
                self.assertTrue(pinner.request_pin(asset_path))
                stats = pinner.get_stats()
                self.assertEqual(stats["total_pinned_files"], 1)
            finally:
                storage.shutdown()


class DaemonLifecycleTests(unittest.TestCase):
    def test_daemon_reports_running_and_stopped(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as directory:
            live_cfg_path = os.path.join(directory, "live_config.json")
            live_state_path = os.path.join(directory, "live_state.json")
            user_cfg_path = os.path.join(directory, "user_config.json")
            env = dict(os.environ)
            env["RECOGD_LIVE_CONFIG_PATH"] = live_cfg_path
            env["RECOGD_LIVE_STATE_PATH"] = live_state_path
            env["RECOGD_USER_CONFIG_PATH"] = user_cfg_path

            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    os.path.join(project_root, "src", "daemon.py"),
                    "--vault-root", directory,
                    "--ext-mount", directory
                ],
                cwd=project_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            try:
                deadline = time.monotonic() + 5.0
                daemon_online = False
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        output = process.stdout.read() if process.stdout else ""
                        self.fail(f"Daemon exited before running:\n{output}")
                    try:
                        with open(live_state_path, "r", encoding="utf-8") as state_file:
                            daemon_online = bool(json.load(state_file).get("daemon_online"))
                    except (FileNotFoundError, json.JSONDecodeError):
                        daemon_online = False
                    if daemon_online:
                        break
                    time.sleep(0.05)

                self.assertTrue(daemon_online)

                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=5), 0)
                output = process.stdout.read() if process.stdout else ""
                self.assertIn("PRESHADER UNIVERSAL DAEMON ACTIVE", output)
                with open(live_state_path, "r", encoding="utf-8") as state_file:
                    final_online = bool(json.load(state_file).get("daemon_online"))
                self.assertFalse(final_online)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()


if __name__ == "__main__":
    unittest.main()
