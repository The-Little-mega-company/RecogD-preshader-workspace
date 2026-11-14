import json
import os
import sys
import subprocess
import tempfile
import unittest

sys_path_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if sys_path_src not in sys.path:
    sys.path.insert(0, sys_path_src)

from pinner import AssetPinner
from storage import BrainStorage
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
    def test_tracks_successful_cache_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            asset_path = os.path.join(directory, "asset.pbo")
            with open(asset_path, "wb") as asset_file:
                asset_file.write(b"asset data")
            storage = BrainStorage(
                directory,
                {"appid": "1", "name": "Test", "safe_id": "1_Test"},
                external_mount=os.path.join(directory, "external"),
                enable_striping=False
            )
            pinner = AssetPinner(
                storage,
                config={
                    "max_cache_mb": 512,
                    "min_free_ram_mb": 64,
                    "ram_governor": {"enabled": False, "max_pinned_budget_mb": 512}
                }
            )
            try:
                self.assertTrue(pinner.request_pin(asset_path))
                self.assertEqual(pinner.get_active_pinned_bytes(), len(b"asset data"))
                self.assertEqual(pinner.get_stats()["total_pinned_files"], 1)
            finally:
                pinner.shutdown()
                storage.shutdown()


class DaemonLifecycleTests(unittest.TestCase):
    def test_daemon_cli_accepts_required_vault_root(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    os.path.join(project_root, "src", "daemon.py"),
                    "--vault-root", directory,
                    "--ext-mount", directory,
                    "--help"
                ],
                cwd=project_root,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("--vault-root VAULT_ROOT", result.stdout)


if __name__ == "__main__":
    unittest.main()
