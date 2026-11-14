import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from pinner import AssetPinner
from ram_governor import RamBudgetGovernor
from storage import BrainStorage
from velocity import PredictiveVelocityTracker


class PredictiveVelocityTests(unittest.TestCase):
    def test_scales_preload_and_enables_corner_lookahead_under_rapid_input(self):
        config = {
            "velocity": {
                "enabled": True,
                "sample_window_seconds": 1.0,
                "mouse_pan_reference_px_per_sec": 1000.0,
                "max_preload_multiplier": 2.5,
                "corner_lookahead_threshold": 0.5,
                "max_lookahead_hops": 2,
                "lookahead_children_limit": 10
            }
        }
        tracker = PredictiveVelocityTracker(config=config, enable_evdev=False)
        now = time.monotonic()
        try:
            idle = tracker.get_adaptive_profile(30, 0.75, 2, now=now)
            self.assertEqual(idle["state"], "IDLE")
            self.assertEqual(idle["lookahead_hops"], 1)
            self.assertEqual(idle["max_preload"], 30)

            tracker.record_input_sample(mouse_delta=900.0, key_weight=2.0, now=now + 0.1)
            rapid = tracker.get_adaptive_profile(30, 0.75, 2, now=now + 0.2)
            self.assertGreaterEqual(rapid["velocity"], 0.75)
            self.assertEqual(rapid["state"], "VEHICLE_OR_RAPID_PAN")
            self.assertEqual(rapid["lookahead_hops"], 2)
            self.assertGreater(rapid["max_preload"], 30)
            self.assertEqual(rapid["min_confidence"], 1)
        finally:
            tracker.shutdown()


class RamBudgetGovernorTests(unittest.TestCase):
    def test_protected_free_ram_floor_allows_1500_mb(self):
        config = {
            "ram_governor": {"min_free_reserve_mb": 1500},
            "min_free_ram_mb": 1500
        }
        governor = RamBudgetGovernor(config)
        pinner = AssetPinner(object(), config=config, ram_governor=governor)
        try:
            with patch.object(governor, "enforce_budget"):
                pinner.apply_live_config(config)
                self.assertEqual(governor.min_free_reserve_mb, 1500)
                pinner.apply_live_config({
                    **config,
                    "min_free_ram_mb": 1200
                })
                self.assertEqual(governor.min_free_reserve_mb, 1500)
        finally:
            pinner.shutdown()

    def _write_proc_files(self, directory, total_mb, available_mb, psi_some=0.0, psi_full=0.0):
        meminfo = os.path.join(directory, "meminfo")
        pressure = os.path.join(directory, "memory_psi")
        with open(meminfo, "w", encoding="utf-8") as file:
            file.write(f"MemTotal: {int(total_mb * 1024)} kB\n")
            file.write(f"MemAvailable: {int(available_mb * 1024)} kB\n")
            file.write("SwapTotal: 2097152 kB\nSwapFree: 1048576 kB\n")
        with open(pressure, "w", encoding="utf-8") as file:
            file.write(f"some avg10={psi_some:.2f} avg60=0.00 avg300=0.00 total=0\n")
            file.write(f"full avg10={psi_full:.2f} avg60=0.00 avg300=0.00 total=0\n")
        return meminfo, pressure

    def test_governor_evicts_pins_when_memory_and_psi_are_critical(self):
        with tempfile.TemporaryDirectory() as directory:
            meminfo, pressure = self._write_proc_files(directory, 16384, 8192)
            config = {
                "ram_governor": {
                    "enabled": True,
                    "max_pinned_budget_mb": 512,
                    "min_free_reserve_mb": 1024,
                    "soft_available_ratio": 0.30,
                    "hard_available_ratio": 0.12,
                    "poll_interval_seconds": 0.0
                },
                "max_cache_mb": 512,
                "min_free_ram_mb": 1024
            }
            governor = RamBudgetGovernor(config, meminfo_path=meminfo, psi_path=pressure)
            self.assertEqual(governor.evaluate(force=True)["scale_factor"], 1.0)
            self.assertEqual(governor.min_free_reserve_mb, 1500)

            game = {"appid": "1", "name": "Test", "safe_id": "1_Test"}
            storage = BrainStorage(
                directory,
                game,
                external_mount=os.path.join(directory, "external"),
                enable_striping=False
            )
            pinner = AssetPinner(storage, ram_governor=governor, config=config)
            try:
                for index in range(4):
                    asset = os.path.join(directory, f"asset-{index}.pbo")
                    with open(asset, "wb") as file:
                        file.write(b"x" * (64 * 1024))
                    self.assertTrue(pinner.request_pin(asset))
                self.assertEqual(pinner.get_active_pinned_bytes(), 4 * 64 * 1024)

                meminfo, pressure = self._write_proc_files(
                    directory, 16384, 900, psi_some=22.0, psi_full=5.0
                )
                self.assertEqual(governor.enforce_budget(pinner), 4)
                self.assertEqual(pinner.get_active_pinned_bytes(), 0)
                self.assertEqual(pinner.get_stats()["governor_evicted_files"], 4)
            finally:
                pinner.shutdown()
                storage.shutdown()

    def test_per_app_cache_cap_can_override_governor_ceiling(self):
        config = {
            "ram_governor": {"max_pinned_budget_mb": 512},
            "max_cache_mb": 2048,
            "min_free_ram_mb": 1536
        }
        governor = RamBudgetGovernor(config)
        pinner = AssetPinner(object(), config=config, ram_governor=governor)
        try:
            with patch.object(governor, "enforce_budget"):
                pinner.apply_live_config(config)
                self.assertEqual(governor.max_budget_mb, 512)

                pinner.apply_live_config({
                    **config,
                    "app_cache_cap_mb": 4096
                })
                self.assertEqual(governor.max_budget_mb, 4096)

                pinner.apply_live_config(config)
                self.assertEqual(governor.max_budget_mb, 512)
        finally:
            pinner.shutdown()


if __name__ == "__main__":
    unittest.main()
