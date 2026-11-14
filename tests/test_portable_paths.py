import json
import os
import sys
import tempfile
import unittest
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from storage import BrainStorage, is_portable_asset_path, normalize_asset_path
from vault_merger import VaultCacheMerger


class PortableGraphPathTests(unittest.TestCase):
    def test_graph_paths_save_as_tokens_and_resolve_to_new_library_roots(self):
        with tempfile.TemporaryDirectory() as root:
            old_roots = {
                "install_dir": os.path.join(root, "old-user", "SteamLibrary", "steamapps", "common", "Game"),
                "workshop_dir": os.path.join(root, "old-user", "SteamLibrary", "steamapps", "workshop", "content", "123"),
                "shader_dir": os.path.join(root, "old-user", "SteamLibrary", "steamapps", "shadercache", "123")
            }
            new_roots = {
                "install_dir": os.path.join(root, "new-user", "Games", "Library", "steamapps", "common", "Game"),
                "workshop_dir": os.path.join(root, "new-user", "Games", "Library", "steamapps", "workshop", "content", "123"),
                "shader_dir": os.path.join(root, "new-user", "Games", "Library", "steamapps", "shadercache", "123")
            }
            old_paths = {
                token: os.path.join(old_roots[root_key], filename)
                for token, root_key, filename in (
                    ("game", "install_dir", "game.pbo"),
                    ("workshop", "workshop_dir", "mod.pbo"),
                    ("shader", "shader_dir", "shader.foz")
                )
            }
            brain_root = os.path.join(root, "vault")
            game_info = {
                "appid": "123",
                "name": "Game",
                "safe_id": "123_Game",
                **old_roots
            }
            storage = BrainStorage(
                brain_root,
                game_info,
                external_mount=os.path.join(root, "external"),
                enable_striping=False
            )
            graph = defaultdict(lambda: defaultdict(int))
            graph[old_paths["game"]][old_paths["workshop"]] = 3
            graph[old_paths["workshop"]][old_paths["shader"]] = 2
            graph[old_paths["shader"]][old_paths["game"]] = 2
            try:
                self.assertTrue(storage.save(graph))
                with open(storage.db_path, "r", encoding="utf-8") as file:
                    saved = json.load(file)["graph"]
                self.assertEqual(
                    set(saved),
                    {"@GAME/game.pbo", "@WORKSHOP/mod.pbo", "@SHADERCACHE/shader.foz"}
                )
                self.assertTrue(all(is_portable_asset_path(path) for path in saved))
                self.assertEqual(normalize_asset_path("@GAME/game.pbo"), "@GAME/game.pbo")
            finally:
                storage.shutdown()

            next_storage = BrainStorage(
                brain_root,
                {**game_info, **new_roots},
                external_mount=os.path.join(root, "external"),
                enable_striping=False
            )
            try:
                loaded = next_storage.load()
                new_game = os.path.join(new_roots["install_dir"], "game.pbo")
                new_workshop = os.path.join(new_roots["workshop_dir"], "mod.pbo")
                new_shader = os.path.join(new_roots["shader_dir"], "shader.foz")
                self.assertEqual(loaded[new_game][new_workshop], 3)
                self.assertEqual(loaded[new_workshop][new_shader], 2)
                self.assertEqual(loaded[new_shader][new_game], 2)
            finally:
                next_storage.shutdown()

    def test_vault_merge_keeps_portable_tokens_intact(self):
        with tempfile.TemporaryDirectory() as root:
            source = os.path.join(root, "source.json")
            destination = os.path.join(root, "destination.json")
            with open(source, "w", encoding="utf-8") as file:
                json.dump({
                    "metadata": {"game_name": "Game", "appid": "123", "safe_id": "123_Game"},
                    "graph": {"@GAME/game.pbo": {"@WORKSHOP/mod.pbo": 3}}
                }, file)

            merger = VaultCacheMerger(
                ext_vault=os.path.join(root, "external-vault"),
                nvme_vault=os.path.join(root, "fallback-vault")
            )
            self.assertTrue(merger._merge_two_brain_files(source, destination))
            with open(destination, "r", encoding="utf-8") as file:
                graph = json.load(file)["graph"]
            self.assertEqual(graph, {"@GAME/game.pbo": {"@WORKSHOP/mod.pbo": 3}})


if __name__ == "__main__":
    unittest.main()
