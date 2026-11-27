import os
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import config_ipc
from velocity import PredictiveVelocityTracker
from ram_governor import RamBudgetGovernor
from pinner import AssetPinner
from storage import BrainStorage
from engine import RecogEngine


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

        idle_prof = tracker.get_adaptive_profile(30, 0.75, 2, now=now)
        self.assertEqual(idle_prof["state"], "IDLE")
        self.assertEqual(idle_prof["lookahead_hops"], 1)
        self.assertEqual(idle_prof["max_preload"], 30)

        tracker.record_input_sample(mouse_delta=900.0, key_weight=2.0, now=now + 0.1)
        fast_prof = tracker.get_adaptive_profile(30, 0.75, 2, now=now + 0.2)

        self.assertGreaterEqual(fast_prof["velocity"], 0.75)
        self.assertEqual(fast_prof["state"], "VEHICLE_OR_RAPID_PAN")
        self.assertEqual(fast_prof["lookahead_hops"], 2)
        self.assertGreater(fast_prof["max_preload"], 30)
        self.assertEqual(fast_prof["min_confidence"], 1)

    def test_engine_fetches_second_hop_corner_assets_at_high_velocity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            asset_a = os.path.join(tmpdir, "road_entry.pbo")
            asset_b = os.path.join(tmpdir, "corner_building.pbo")
            asset_c = os.path.join(tmpdir, "hidden_interior.pbo")
            for p in (asset_a, asset_b, asset_c):
                with open(p, "wb") as f:
                    f.write(b"x" * 4096)

            config = {
                "engine": {
                    "time_window_seconds": 0.75,
                    "min_confidence_hits": 2,
                    "stripe_confidence_hits": 10,
                    "max_preload_children": 10,
                    "max_file_size_bytes": 1048576
                },
                "velocity": {
                    "enabled": True,
                    "sample_window_seconds": 1.0,
                    "mouse_pan_reference_px_per_sec": 1000.0,
                    "corner_lookahead_threshold": 0.5,
                    "max_lookahead_hops": 2,
                    "lookahead_children_limit": 8
                },
                "ram_governor": {"enabled": False},
                "target_extensions": [".pbo"]
            }

            storage = BrainStorage(
                vault_root=tmpdir,
                game_info={"appid": "221100", "name": "DayZ", "safe_id": "221100_DayZ"},
                external_mount=tmpdir,
                enable_striping=False
            )

            class _EngineAdapterPinner(AssetPinner):
                def get_active_pinned_bytes(self):
                    return self.current_pinned_bytes

                def trim_to_budget(self, _budget):
                    return 0, 0

            pinner = _EngineAdapterPinner(storage, max_file_mb=16)
            pinner.apply_live_config({"max_cache_mb": 1536, "min_free_ram_mb": 1024})
            tracker = PredictiveVelocityTracker(config=config, enable_evdev=False)
            governor = RamBudgetGovernor(config=config)
            engine = RecogEngine(
                config,
                pinner,
                storage,
                velocity_tracker=tracker,
                ram_governor=governor
            )

            engine.graph[asset_a][asset_b] = 3
            engine.graph[asset_b][asset_c] = 3

            try:
                tracker.record_input_sample(mouse_delta=1200.0, key_weight=3.0)
                engine.process_event(asset_a)

                self.assertIn(asset_b, pinner.active_pins)
                self.assertIn(asset_c, pinner.active_pins)
                self.assertGreaterEqual(engine.lookahead_pins_total, 1)
            finally:
                storage.shutdown()


class RamBudgetGovernorTests(unittest.TestCase):
    def _write_proc_files(self, tmpdir, total_mb, avail_mb, swap_free_mb=1024, psi_some=0.0, psi_full=0.0):
        meminfo_path = os.path.join(tmpdir, "meminfo")
        psi_path = os.path.join(tmpdir, "memory_psi")
        with open(meminfo_path, "w", encoding="utf-8") as f:
            f.write(f"MemTotal:       {int(total_mb * 1024)} kB\n")
            f.write(f"MemAvailable:   {int(avail_mb * 1024)} kB\n")
            f.write("SwapTotal:      2097152 kB\n")
            f.write(f"SwapFree:       {int(swap_free_mb * 1024)} kB\n")
        with open(psi_path, "w", encoding="utf-8") as f:
            f.write(f"some avg10={psi_some:.2f} avg60=0.00 avg300=0.00 total=0\n")
            f.write(f"full avg10={psi_full:.2f} avg60=0.00 avg300=0.00 total=0\n")
        return meminfo_path, psi_path

    def test_scales_back_and_evicts_under_memory_pressure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mem_path, psi_path = self._write_proc_files(tmpdir, total_mb=16384, avail_mb=8192)
            config = {
                "ram_governor": {
                    "enabled": True,
                    "max_pinned_budget_mb": 512.0,
                    "min_free_reserve_mb": 1024.0,
                    "soft_available_ratio": 0.30,
                    "hard_available_ratio": 0.12,
                    "poll_interval_seconds": 0.0
                }
            }
            governor = RamBudgetGovernor(config=config, meminfo_path=mem_path, psi_path=psi_path)
            snap_opt = governor.evaluate(force=True)
            self.assertEqual(snap_opt["state"], "OPTIMAL")
            self.assertEqual(snap_opt["scale_factor"], 1.0)

            self._write_proc_files(tmpdir, total_mb=16384, avail_mb=900, swap_free_mb=950, psi_some=22.0)
            snap_crit = governor.evaluate(force=True)
            self.assertEqual(snap_crit["state"], "CRITICAL_THRASH_GUARD")
            self.assertEqual(snap_crit["scale_factor"], 0.0)
            self.assertEqual(snap_crit["dynamic_budget_bytes"], 0)

            storage = BrainStorage(
                vault_root=tmpdir,
                game_info={"appid": "221100", "name": "DayZ", "safe_id": "221100_DayZ"},
                external_mount=tmpdir,
                enable_striping=False
            )

            class _EngineAdapterPinner(AssetPinner):
                def get_active_pinned_bytes(self):
                    return self.current_pinned_bytes

                def trim_to_budget(self, _budget):
                    return 0, 0

            pinner = _EngineAdapterPinner(storage, max_file_mb=16)
            pinner.apply_live_config({"max_cache_mb": 1536, "min_free_ram_mb": 1024})
            try:
                for idx in range(4):
                    p = os.path.join(tmpdir, f"chunk_{idx}.pbo")
                    with open(p, "wb") as f:
                        f.write(b"A" * (64 * 1024))
                    self.assertTrue(pinner.request_pin(p))

                self.assertEqual(pinner.current_pinned_bytes, 4 * 64 * 1024)
                flushed = pinner.flush_all_pins()
                self.assertEqual(flushed, 4)
                self.assertEqual(pinner.current_pinned_bytes, 0)
                self.assertEqual(pinner.evicted_files, 4)
            finally:
                storage.shutdown()


class ConfigIpcTests(unittest.TestCase):
    def test_update_live_config_and_request_gui_show(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            live_cfg = os.path.join(tmpdir, "live_config.json")
            user_cfg = os.path.join(tmpdir, "user_config.json")
            with patch.object(config_ipc, "LIVE_CONFIG_PATH", live_cfg), \
                 patch.object(config_ipc, "USER_CONFIG_PATH", user_cfg), \
                 patch.object(config_ipc, "WORKSPACE_DIR", tmpdir):
                cfg = config_ipc.update_live_config({"max_cache_mb": 1024}, save_persistent=True)
                self.assertEqual(cfg["max_cache_mb"], 1024)
                self.assertTrue(os.path.isfile(user_cfg))

                updated = config_ipc.request_gui_show()
                self.assertTrue(bool(updated.get("show_gui_request")))


if __name__ == "__main__":
    unittest.main()
