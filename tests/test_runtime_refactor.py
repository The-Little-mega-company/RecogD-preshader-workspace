import json
import os
import sys
import threading
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from engine import DEFAULT_EXTENSIONS, RecogEngine, _top_ranked_entries
import config_ipc
from daemon import live_config_with_app_cache_cap, resolve_app_cache_cap
from pinner import AssetPinner, POSIX_FADV_WILLNEED
from ram_governor import RamBudgetGovernor
from storage import BrainStorage, normalize_asset_path
from velocity import PredictiveVelocityTracker


class RuntimeRefactorTests(unittest.TestCase):
    def _storage(self, root, game_root=None, half_life_days=30):
        game_info = {
            "appid": "221100",
            "name": "DayZ",
            "safe_id": "221100_DayZ",
            "install_dir": game_root or root,
            "workshop_dir": root,
            "shader_dir": root
        }
        return BrainStorage(
            os.path.join(root, "vault"),
            game_info,
            external_mount=os.path.join(root, "external"),
            enable_striping=False,
            edge_decay_half_life_days=half_life_days
        )

    def test_gui_show_requests_are_unique_and_live_only(self):
        with tempfile.TemporaryDirectory() as root:
            with (
                patch.object(config_ipc, "LIVE_CONFIG_PATH", os.path.join(root, "live.json")),
                patch.object(config_ipc, "PERSISTENT_CONFIG_PATH", os.path.join(root, "config.json")),
                patch.object(config_ipc, "LEGACY_CONFIG_PATH", os.path.join(root, "legacy.json"))
            ):
                config_ipc.init_live_config()
                first = config_ipc.request_gui_show()
                second = config_ipc.request_gui_show()

                self.assertNotEqual(first, second)
                self.assertEqual(config_ipc.read_live_config()["show_gui_request"], second)
                self.assertFalse(os.path.exists(config_ipc.PERSISTENT_CONFIG_PATH))

    def test_large_candidate_sets_select_top_entries_with_stable_ties(self):
        entries = {"first": 10, "second": 10, "third": 9}
        entries.update({f"other-{index}": 1 for index in range(300)})

        self.assertEqual(
            _top_ranked_entries(entries, 2),
            [("first", 10), ("second", 10)]
        )

    def test_app_cache_caps_use_metadata_then_appid_map_and_fall_back(self):
        config = {"app_ram_caps_mb": {"221100": 4096, "444200": "1024"}}
        game = {"appid": "221100"}
        self.assertEqual(resolve_app_cache_cap(config, game), 4096)
        self.assertEqual(
            resolve_app_cache_cap(config, {**game, "max_cache_mb": 2048}),
            2048
        )
        self.assertEqual(resolve_app_cache_cap(config, {"appid": "489830"}), None)
        self.assertEqual(
            live_config_with_app_cache_cap(
                {"max_cache_mb": 1536, "app_cache_cap_mb": 1024}, None
            ),
            {"max_cache_mb": 1536}
        )
        with self.assertRaises(ValueError):
            resolve_app_cache_cap(
                {"app_ram_caps_mb": {"221100": float("inf")}}, game
            )

    def test_load_decays_and_prunes_graph_edges_below_two_hits(self):
        with tempfile.TemporaryDirectory() as root:
            storage = self._storage(root, half_life_days=30)
            parent = os.path.join(root, "trigger.pbo")
            children = {
                os.path.join(root, "strong.pbo"): 4,
                os.path.join(root, "medium.pbo"): 5,
                os.path.join(root, "weak.pbo"): 2,
                os.path.join(root, "single.pbo"): 1
            }
            old_epoch = time.time() - storage.edge_decay_half_life_seconds
            try:
                os.makedirs(os.path.dirname(storage.db_path), exist_ok=True)
                with open(storage.db_path, "w", encoding="utf-8") as file:
                    json.dump({
                        "metadata": {"graph_weight_decay_at_epoch": old_epoch},
                        "graph": {parent: children}
                    }, file)

                graph = storage.load()
                self.assertEqual(dict(graph[parent]), {
                    os.path.realpath(os.path.join(root, "strong.pbo")): 2,
                    os.path.realpath(os.path.join(root, "medium.pbo")): 2
                })
            finally:
                storage.shutdown()

    def test_save_prunes_every_edge_below_two_hits(self):
        with tempfile.TemporaryDirectory() as root:
            storage = self._storage(root)
            parent = os.path.join(root, "trigger.pbo")
            graph = {parent: {
                os.path.join(root, "learned.pbo"): 2,
                os.path.join(root, "unconfirmed.pbo"): 1
            }}
            try:
                self.assertTrue(storage.save(graph))
                self.assertEqual(
                    dict(graph[os.path.realpath(parent)]),
                    {os.path.realpath(os.path.join(root, "learned.pbo")): 2}
                )
            finally:
                storage.shutdown()

    def test_engine_pins_two_hop_chain_with_shared_governor(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [os.path.join(root, name) for name in (
                "road.pbo", "building.pbo", "interior.pbo"
            )]
            for path in paths:
                with open(path, "wb") as asset:
                    asset.write(b"x" * 4096)

            config = {
                "engine": {
                    "time_window_seconds": 0.75,
                    "min_confidence_hits": 2,
                    "stripe_confidence_hits": 20,
                    "max_preload_children": 10,
                    "max_file_size_bytes": 1024 * 1024,
                    "autosave_interval_seconds": 3600
                },
                "velocity": {
                    "enabled": True,
                    "sample_window_seconds": 1.0,
                    "mouse_pan_reference_px_per_sec": 1000.0,
                    "corner_lookahead_threshold": 0.5,
                    "max_lookahead_hops": 2,
                    "lookahead_children_limit": 8
                },
                "ram_governor": {"enabled": False, "max_pinned_budget_mb": 512},
                "max_cache_mb": 512,
                "min_free_ram_mb": 64,
                "max_hop_depth": 2,
                "target_extensions": [".pbo"]
            }
            storage = self._storage(root)
            governor = RamBudgetGovernor(config=config)
            pinner = AssetPinner(storage, config=config, ram_governor=governor)
            tracker = PredictiveVelocityTracker(config=config, enable_evdev=False)
            engine = RecogEngine(config, pinner, storage, tracker, governor)
            engine.graph[paths[0]][paths[1]] = 3
            engine.graph[paths[1]][paths[2]] = 3
            tracker.record_input_sample(mouse_delta=1200)

            try:
                output = StringIO()
                with redirect_stdout(output):
                    profile = engine.process_event(paths[0])
                self.assertNotIn("[BOOST #", output.getvalue())
                self.assertEqual(pinner.ram_governor, governor)
                self.assertEqual(profile["lookahead_hops"], 2)
                self.assertGreaterEqual(engine.lookahead_pins_total, 1)
                self.assertEqual(engine.last_pinned_file, "interior.pbo")
                self.assertEqual(pinner.get_active_pinned_bytes(), 8192)
            finally:
                engine.shutdown()

    def test_legacy_home_asset_paths_collapse_to_current_realpath(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "current-user")
            asset = os.path.join(home, "Games", "DayZ", "terrain.pbo")
            os.makedirs(os.path.dirname(asset))
            with open(asset, "wb") as file:
                file.write(b"asset")

            with patch.dict(os.environ, {"HOME": home}):
                old_home_path = "/home/previous-user/Games/DayZ/terrain.pbo"
                self.assertEqual(normalize_asset_path(old_home_path), os.path.realpath(asset))

                storage = self._storage(root)
                try:
                    with open(storage.db_path, "w", encoding="utf-8") as brain:
                        json.dump({
                            "metadata": {"updated_at_epoch": int(time.time())},
                            "graph": {
                                old_home_path: {old_home_path: 2},
                                asset: {asset: 5}
                            }
                        }, brain)
                    graph = storage.load()
                    canonical = os.path.realpath(asset)
                    self.assertEqual(list(graph), [canonical])
                    self.assertEqual(graph[canonical][canonical], 5)
                finally:
                    storage.shutdown()

    def test_prism_targets_include_jars_and_extensionless_asset_objects(self):
        with tempfile.TemporaryDirectory() as root:
            storage = self._storage(root)
            storage.game_info["name"] = "PrismLauncher"
            engine = object.__new__(RecogEngine)
            engine.storage = storage
            engine.allowed_exts = set(DEFAULT_EXTENSIONS)

            asset_dir = os.path.join(root, "PrismLauncher", "assets", "objects", "ab")
            os.makedirs(asset_dir)
            object_file = os.path.join(asset_dir, "asset-hash")
            with open(object_file, "wb") as file:
                file.write(b"asset")

            instance_dir = os.path.join(root, "instance", "minecraft", "mods")
            os.makedirs(instance_dir)
            mod_file = os.path.join(instance_dir, "mod.jar")
            with open(mod_file, "wb") as file:
                file.write(b"mod")

            try:
                self.assertTrue(engine.is_valid_target(object_file))
                self.assertTrue(engine.is_valid_target(mod_file))

                storage.game_info["name"] = "DayZ"
                self.assertFalse(engine.is_valid_target(object_file))
            finally:
                storage.shutdown()

    def test_external_vault_save_is_blocked_when_mount_disappears(self):
        with tempfile.TemporaryDirectory() as root:
            mount = os.path.join(root, "ssd")
            vault = os.path.join(mount, "recogd-vault")
            os.makedirs(mount)
            game_info = {
                "appid": "1",
                "name": "Minecraft",
                "safe_id": "1_Minecraft"
            }

            mounted = ("ext4", "/dev/test-ssd", "8:0", False)
            with patch.object(BrainStorage, "_get_external_mount_info", return_value=mounted):
                storage = BrainStorage(
                    vault,
                    game_info,
                    external_mount=mount,
                    enable_striping=True,
                    require_external_mount=True
                )
                self.assertTrue(storage.is_external_mount_available())
            try:
                with patch.object(storage, "_get_external_mount_info", return_value=None):
                    self.assertFalse(storage.is_safe_to_autosave())
                    self.assertFalse(storage.save({}))
                self.assertFalse(os.path.exists(storage.db_path))
            finally:
                storage.shutdown()

    def test_external_vault_waits_for_ext4_after_automount_reconnect(self):
        with tempfile.TemporaryDirectory() as root:
            mount = os.path.join(root, "ssd")
            vault = os.path.join(mount, "recogd-vault")
            os.makedirs(mount)
            game_info = {
                "appid": "1",
                "name": "Minecraft",
                "safe_id": "1_Minecraft"
            }
            automount = ("autofs", "systemd-1", "0:77", False)
            mounted = ("ext4", "/dev/test-ssd", "8:0", False)

            with patch.object(
                BrainStorage, "_get_external_mount_info", return_value=automount
            ):
                storage = BrainStorage(
                    vault,
                    game_info,
                    external_mount=mount,
                    enable_striping=True,
                    require_external_mount=True
                )
            try:
                self.assertFalse(storage.is_external_mount_available())
                self.assertFalse(os.path.isdir(storage.brain_dir))
                with patch.object(
                    storage, "_get_external_mount_info", return_value=mounted
                ):
                    self.assertTrue(storage.is_external_mount_available())
                    self.assertTrue(storage.save({}))
                self.assertTrue(os.path.isfile(storage.db_path))
            finally:
                storage.shutdown()

    def test_saved_graph_weights_decay_by_elapsed_time(self):
        with tempfile.TemporaryDirectory() as root:
            storage = self._storage(root, half_life_days=1)
            graph = {"/assets/old-mod.pbo": {"/assets/shared.pbo": 8}}
            storage.last_decay_epoch = time.time() - 86400
            try:
                storage.save(graph)
                with open(storage.db_path, "r", encoding="utf-8") as brain:
                    saved = json.load(brain)
                self.assertEqual(
                    next(iter(saved["graph"].values()))["/assets/shared.pbo"],
                    4
                )
                self.assertIn("graph_weight_decay_at_epoch", saved["metadata"])
                self.assertEqual(
                    next(iter(graph.values()))["/assets/shared.pbo"],
                    4
                )
                storage.last_decay_epoch -= 86400
                storage.save(graph)
                self.assertEqual(next(iter(graph.values()))["/assets/shared.pbo"], 2)
            finally:
                storage.shutdown()

    def test_large_asset_pins_only_a_budgeted_32mb_slice(self):
        with tempfile.TemporaryDirectory() as root:
            config = {
                "max_cache_mb": 64,
                "min_free_ram_mb": 2048,
                "ram_governor": {
                    "enabled": False,
                    "max_pinned_budget_mb": 64,
                    "min_free_reserve_mb": 2048
                }
            }
            storage = self._storage(root)
            governor = RamBudgetGovernor(config)
            pinner = AssetPinner(storage, max_file_mb=16, config=config, ram_governor=governor)
            archive = os.path.join(root, "large.bsa")
            with open(archive, "wb") as file:
                file.truncate(40 * 1024 * 1024)

            try:
                with patch("pinner.os.posix_fadvise") as advise:
                    self.assertTrue(
                        pinner.request_pin(archive, max_bytes_override=8 * 1024 * 1024)
                    )
                    self.assertEqual(pinner.get_active_pinned_bytes(), 32 * 1024 * 1024)
                    self.assertEqual(pinner.active_pins[os.path.realpath(archive)][0], 32 * 1024 * 1024)
                    self.assertEqual(
                        advise.call_args.args[1:],
                        (0, 32 * 1024 * 1024, POSIX_FADV_WILLNEED)
                    )

                    combat_archive = os.path.join(root, "large-combat.bsa")
                    with open(combat_archive, "wb") as file:
                        file.truncate(40 * 1024 * 1024)
                    pinner.window_start = time.monotonic() - 2
                    pinner.bytes_pinned_in_window = 0
                    self.assertTrue(pinner.request_pin(combat_archive, combat_burst_mode=True))
                    self.assertEqual(
                        pinner.active_pins[os.path.realpath(combat_archive)][0],
                        32 * 1024 * 1024
                    )
                    self.assertEqual(advise.call_args.args[1:], (0, 32 * 1024 * 1024, POSIX_FADV_WILLNEED))

                pinner.window_start = time.monotonic() - 2
                pinner.bytes_pinned_in_window = 0
                too_large_for_remaining_budget = os.path.join(root, "small.pbo")
                with open(too_large_for_remaining_budget, "wb") as file:
                    file.write(b"x" * (12 * 1024 * 1024))
                self.assertEqual(pinner.get_active_pinned_bytes(), 64 * 1024 * 1024)
                self.assertFalse(pinner.request_pin(too_large_for_remaining_budget))
            finally:
                pinner.shutdown()
                storage.shutdown()

    def test_frequent_bsa_header_read_ahead_runs_on_storage_worker(self):
        with tempfile.TemporaryDirectory() as root:
            storage = self._storage(root)
            archive = os.path.join(root, "frequent.bsa")
            with open(archive, "wb") as file:
                file.truncate(40 * 1024 * 1024)
            advised = threading.Event()

            def slow_advice(*_args):
                time.sleep(0.1)
                advised.set()

            try:
                with patch("storage.os.posix_fadvise", side_effect=slow_advice) as advice:
                    start = time.monotonic()
                    storage.consider_cross_bus_stripe(archive, weight=4)
                    self.assertLess(time.monotonic() - start, 0.05)
                    self.assertTrue(advised.wait(timeout=2))
                    self.assertEqual(advice.call_args.args[2], 32 * 1024 * 1024)
            finally:
                storage.shutdown()


if __name__ == "__main__":
    unittest.main()
