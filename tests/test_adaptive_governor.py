import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from velocity import PredictiveVelocityTracker
from ram_governor import RamBudgetGovernor
from pinner import RamPinner
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

        # Baseline idle profile
        idle_prof = tracker.get_adaptive_profile(30, 0.75, 2, now=now)
        self.assertEqual(idle_prof["state"], "IDLE")
        self.assertEqual(idle_prof["lookahead_hops"], 1)
        self.assertEqual(idle_prof["max_preload"], 30)

        # Simulate rapid camera panning / vehicle sprint
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

            pinned = []
            pinner = RamPinner(
                cooldown_sec=0.0,
                preload_callback=lambda path, sz, ext, st: pinned.append(path)
            )
            storage = BrainStorage(
                vault_root=tmpdir,
                game_info={"appid": "221100", "name": "DayZ", "safe_id": "221100_DayZ"},
                external_mount=tmpdir,
                enable_striping=False
            )
            tracker = PredictiveVelocityTracker(config=config, enable_evdev=False)
            governor = RamBudgetGovernor(config=config)
            engine = RecogEngine(
                config,
                pinner,
                storage,
                velocity_tracker=tracker,
                ram_governor=governor
            )

            # Pre-populate 2-hop chain: road_entry -> corner_building -> hidden_interior
            engine.graph[asset_a][asset_b] = 3
            engine.graph[asset_b][asset_c] = 3

            try:
                # Inject high-velocity camera pan / driving input
                tracker.record_input_sample(mouse_delta=1200.0, key_weight=3.0)
                engine.process_event(asset_a)

                deadline = time.monotonic() + 2.0
                while len(pinned) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)

                self.assertIn(asset_b, pinned)
                self.assertIn(asset_c, pinned)
                self.assertGreaterEqual(engine.lookahead_pins_total, 1)
            finally:
                pinner.shutdown()
                storage.shutdown()


class RamBudgetGovernorTests(unittest.TestCase):
    def _write_proc_files(self, tmpdir, total_mb, avail_mb, swap_free_mb=1024, psi_some=0.0, psi_full=0.0):
        meminfo_path = os.path.join(tmpdir, "meminfo")
        psi_path = os.path.join(tmpdir, "memory_psi")
        with open(meminfo_path, "w", encoding="utf-8") as f:
            f.write(f"MemTotal:       {int(total_mb * 1024)} kB\n")
            f.write(f"MemAvailable:   {int(avail_mb * 1024)} kB\n")
            f.write(f"SwapTotal:      2097152 kB\n")
            f.write(f"SwapFree:       {int(swap_free_mb * 1024)} kB\n")
        with open(psi_path, "w", encoding="utf-8") as f:
            f.write(f"some avg10={psi_some:.2f} avg60=0.00 avg300=0.00 total=0\n")
            f.write(f"full avg10={psi_full:.2f} avg60=0.00 avg300=0.00 total=0\n")
        return meminfo_path, psi_path

    def test_scales_back_and_evicts_under_memory_pressure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Start with plenty of available RAM (8 GB of 16 GB free)
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

            pinner = RamPinner(cooldown_sec=0.0, ram_governor=governor)
            try:
                assets = []
                for idx in range(4):
                    p = os.path.join(tmpdir, f"chunk_{idx}.pbo")
                    with open(p, "wb") as f:
                        f.write(b"A" * (64 * 1024))
                    assets.append(p)
                    self.assertTrue(pinner.request_pin(p))

                deadline = time.monotonic() + 2.0
                while pinner.get_active_pinned_bytes() < (4 * 64 * 1024) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(pinner.get_active_pinned_bytes(), 4 * 64 * 1024)

                # Simulate heavy background browser/app demanding RAM (avail drops below reserve)
                self._write_proc_files(tmpdir, total_mb=16384, avail_mb=900, swap_free_mb=950, psi_some=22.0)
                evicted_count = governor.enforce_budget(pinner)

                self.assertEqual(evicted_count, 4)
                self.assertEqual(pinner.get_active_pinned_bytes(), 0)
                self.assertEqual(pinner.stats["evicted_files"], 4)

                # Further pin requests are blocked while critical pressure persists
                self.assertFalse(pinner.request_pin(assets[0]))
                self.assertGreaterEqual(pinner.stats["skipped_ram_pressure"], 1)
            finally:
                pinner.shutdown()


class GuiControlDeckTests(unittest.TestCase):
    def test_presets_toggles_and_parameter_levels_persist_and_reload(self):
        import json
        from ui import RecogDDesktop, PARAMETER_DEFINITIONS

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, "config.json")
            tel_path = os.path.join(tmpdir, "runtime.json")
            app = RecogDDesktop(config_path=cfg_path, telemetry_path=tel_path, headless=True)

            # Test switching across all 4 performance tiers
            for tier, expected_children in (("eco", 18), ("balanced", 35), ("aggressive", 60), ("ultra", 90)):
                app.apply_preset(tier)
                with open(cfg_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.assertEqual(saved["engine"]["max_preload_children"], expected_children)

            # Test toggle switches
            app._on_toggle_velocity(False)
            app._on_toggle_corner_lookahead(False)
            app._on_toggle_ram_governor(False)
            app._on_toggle_striping(False)
            app._on_toggle_instant_confidence(True)

            with open(cfg_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertFalse(saved["velocity"]["enabled"])
            self.assertEqual(saved["velocity"]["max_lookahead_hops"], 1)
            self.assertFalse(saved["ram_governor"]["enabled"])
            self.assertFalse(saved["external_drive"]["enable_cross_bus_striping"])
            self.assertEqual(saved["engine"]["min_confidence_hits"], 1)

            # Test discrete parameter level update
            pdef = PARAMETER_DEFINITIONS[0]  # max_preload_children
            app._set_parameter(pdef, 60)
            with open(cfg_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["engine"]["max_preload_children"], 60)

            # Re-enable velocity and test corner burst simulator
            app._on_toggle_velocity(True)
            app._on_toggle_corner_lookahead(True)
            app.simulate_velocity_burst()
            self.assertGreaterEqual(app.local_velocity.get_stats()["peak_velocity_score"], 0.7)
            app.daemon.pinner.nvme_executor.shutdown(wait=True)
            app.daemon.pinner.ext_executor.shutdown(wait=True)
            app.quit()


if __name__ == "__main__":
    unittest.main()
