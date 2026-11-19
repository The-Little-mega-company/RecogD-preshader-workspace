import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from daemon import InProcessPreshaderDaemon
from disk_health import DiskHealthChecker, _derive_parent_block_device
from telemetry import RuntimeTelemetry


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class DiskHealthUtilityTests(unittest.TestCase):
    def test_derive_parent_block_device(self):
        self.assertEqual(_derive_parent_block_device("/dev/sda1"), "/dev/sda")
        self.assertEqual(_derive_parent_block_device("/dev/nvme0n1p2"), "/dev/nvme0n1")
        self.assertEqual(_derive_parent_block_device("/dev/mmcblk0p1"), "/dev/mmcblk0")
        self.assertEqual(_derive_parent_block_device("/dev/sdb"), "/dev/sdb")

    def test_healthy_disk_and_vault_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = os.path.join(tmpdir, "recogd-vault", "games", "221100_DayZ")
            os.makedirs(vault_dir, exist_ok=True)
            with open(os.path.join(vault_dir, "brain.json"), "w", encoding="utf-8") as f:
                json.dump({"nodes": 5}, f)
            with open(os.path.join(vault_dir, "shader.foz"), "wb") as f:
                f.write(b"VALID_CACHE_DATA" * 64)

            def fake_runner(cmd, **kwargs):
                exe = os.path.basename(cmd[0])
                if exe == "findmnt":
                    return _fake_completed(0, f"/dev/sdb1 ext4 {tmpdir} rw,noatime\n")
                if exe == "smartctl":
                    out = (
                        "SMART overall-health self-assessment test result: PASSED\n"
                        "194 Temperature_Celsius     0x0022   066   050   000    Old_age   Always       -       34\n"
                        "  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0\n"
                    )
                    return _fake_completed(0, out)
                if exe == "badblocks":
                    return _fake_completed(0, "")
                return _fake_completed(0, "")

            logs = []
            with mock.patch("disk_health._tool_path", side_effect=lambda name: f"/usr/bin/{name}"):
                checker = DiskHealthChecker(
                    ext_mount=tmpdir,
                    vault_paths=[os.path.join(tmpdir, "recogd-vault")],
                    log_callback=logs.append,
                    runner=fake_runner,
                )
                report = checker.run_check(repair=False)

            self.assertEqual(report["status"], "HEALTHY")
            self.assertEqual(report["mount"]["device"], "/dev/sdb1")
            self.assertEqual(report["mount"]["parent_device"], "/dev/sdb")
            self.assertEqual(report["smart"]["status"], "PASSED")
            self.assertEqual(report["smart"]["temperature_c"], 34)
            self.assertEqual(report["smart"]["reallocated_sectors"], 0)
            self.assertEqual(report["badblocks"]["status"], "CLEAN")
            self.assertEqual(report["vault_integrity"]["files_checked"], 2)
            self.assertEqual(len(report["vault_integrity"]["corrupted_files"]), 0)
            self.assertTrue(any("[DISK-HEALTH] HEALTHY" in line for line in logs))

    def test_permission_denied_degrades_gracefully(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_runner(cmd, **kwargs):
                exe = os.path.basename(cmd[0])
                if exe == "findmnt":
                    return _fake_completed(0, f"/dev/nvme0n1p1 ext4 {tmpdir} rw,relatime\n")
                if exe == "smartctl":
                    return _fake_completed(2, "", "Smartctl open device: /dev/nvme0n1 failed: Permission denied")
                if exe == "badblocks":
                    return _fake_completed(1, "", "badblocks: Permission denied while trying to determine device size")
                return _fake_completed(1, "", "Permission denied")

            with mock.patch("disk_health._tool_path", side_effect=lambda name: f"/usr/bin/{name}"):
                checker = DiskHealthChecker(ext_mount=tmpdir, runner=fake_runner)
                report = checker.run_check(repair=False)

            self.assertEqual(report["status"], "WARNING")
            self.assertEqual(report["smart"]["status"], "PERMISSION_DENIED")
            self.assertEqual(report["badblocks"]["status"], "PERMISSION_DENIED")
            self.assertGreaterEqual(len(report["warnings"]), 2)

    def test_degraded_smart_and_bad_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_runner(cmd, **kwargs):
                exe = os.path.basename(cmd[0])
                if exe == "findmnt":
                    return _fake_completed(0, f"/dev/sda1 ext4 {tmpdir} rw\n")
                if exe == "smartctl":
                    return _fake_completed(8, "SMART overall-health self-assessment test result: FAILED!\n")
                if exe == "badblocks":
                    return _fake_completed(0, "1024\n2048\n")
                return _fake_completed(0, "")

            with mock.patch("disk_health._tool_path", side_effect=lambda name: f"/usr/bin/{name}"):
                checker = DiskHealthChecker(ext_mount=tmpdir, runner=fake_runner)
                report = checker.run_check(repair=False)

            self.assertEqual(report["status"], "DEGRADED")
            self.assertFalse(report["smart"]["healthy"])
            self.assertEqual(report["badblocks"]["count"], 2)

    def test_corrupted_vault_detection_and_repair(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_dir = os.path.join(tmpdir, "recogd-vault")
            os.makedirs(vault_dir, exist_ok=True)
            bad_json = os.path.join(vault_dir, "broken_brain.json")
            zero_cache = os.path.join(vault_dir, "truncated.dxvk-cache")
            good_cache = os.path.join(vault_dir, "healthy.foz")

            with open(bad_json, "w", encoding="utf-8") as f:
                f.write("{corrupted json")
            with open(zero_cache, "wb") as f:
                pass
            with open(good_cache, "wb") as f:
                f.write(b"OK_SHADER_DATA")

            def fake_runner(cmd, **kwargs):
                exe = os.path.basename(cmd[0])
                if exe == "findmnt":
                    return _fake_completed(0, f"/dev/sdb1 ext4 {tmpdir} rw\n")
                if exe == "smartctl":
                    return _fake_completed(0, "SMART overall-health self-assessment test result: PASSED\n")
                if exe == "badblocks":
                    return _fake_completed(0, "")
                return _fake_completed(0, "")

            with mock.patch("disk_health._tool_path", side_effect=lambda name: f"/usr/bin/{name}"):
                checker = DiskHealthChecker(ext_mount=tmpdir, vault_paths=[vault_dir], runner=fake_runner)
                report = checker.run_check(repair=True)

            self.assertEqual(report["status"], "REPAIRED")
            self.assertEqual(len(report["vault_integrity"]["corrupted_files"]), 2)
            self.assertEqual(len(report["vault_integrity"]["repaired_files"]), 2)
            self.assertFalse(os.path.exists(bad_json))
            self.assertTrue(os.path.exists(f"{bad_json}.corrupt.bak"))
            self.assertFalse(os.path.exists(zero_cache))
            self.assertTrue(os.path.exists(good_cache))
            # Mounted RW partition must skip write-mode fsck for safety
            self.assertEqual(report["fsck"]["status"], "MOUNTED_SKIPPED")

    def test_unmounted_fsck_repair_runs_safely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            def fake_runner(cmd, **kwargs):
                exe = os.path.basename(cmd[0])
                if exe in ("fsck", "e2fsck"):
                    return _fake_completed(1, "FILE SYSTEM WAS MODIFIED")
                return _fake_completed(0, "")

            with mock.patch("disk_health._tool_path", side_effect=lambda name: f"/usr/bin/{name}"):
                checker = DiskHealthChecker(ext_mount=tmpdir, runner=fake_runner)
                res = checker._run_fsck_repair({
                    "device": "/dev/sdb1",
                    "fstype": "ext4",
                    "mounted": False,
                    "read_only": False,
                    "target": tmpdir,
                })

            self.assertEqual(res["status"], "REPAIRED")

    def test_telemetry_and_daemon_integration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            telemetry_path = os.path.join(tmpdir, "runtime.json")
            config_path = os.path.join(tmpdir, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump({}, f)

            telemetry = RuntimeTelemetry(telemetry_path)
            telemetry.record_disk_health({"status": "HEALTHY", "timestamp": "2026-11-19T00:00:00"})
            reloaded = RuntimeTelemetry(telemetry_path)
            self.assertEqual(reloaded.data["disk_health"]["status"], "HEALTHY")

            daemon = InProcessPreshaderDaemon(
                config_path=config_path,
                vault_root=tmpdir,
                ext_mount=tmpdir,
                telemetry_path=telemetry_path,
            )
            with mock.patch.object(
                daemon.disk_health_checker,
                "run_check",
                return_value={
                    "status": "HEALTHY",
                    "timestamp": "2026-11-19T02:55:00",
                    "summary_text": "OK REPORT",
                    "vault_integrity": {"repaired_files": []},
                },
            ):
                report = daemon.run_disk_health_check(repair=False)

            self.assertEqual(report["status"], "HEALTHY")
            stats = daemon.get_combined_stats()
            self.assertEqual(stats["disk_health_status"], "HEALTHY")


if __name__ == "__main__":
    unittest.main()
