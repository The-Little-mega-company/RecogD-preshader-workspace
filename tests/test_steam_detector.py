import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from steam_detector import SteamGameDetector


class SteamGameDetectorTests(unittest.TestCase):
    def _process(self, proc_root, pid, cmdline, environ=b"", stat=None, cwd=None):
        process_dir = os.path.join(proc_root, str(pid))
        os.makedirs(process_dir)
        with open(os.path.join(process_dir, "cmdline"), "wb") as file:
            file.write(cmdline)
        with open(os.path.join(process_dir, "environ"), "wb") as file:
            file.write(environ)
        if stat is not None:
            with open(os.path.join(process_dir, "stat"), "w", encoding="utf-8") as file:
                file.write(stat)
        if cwd is not None:
            os.symlink(cwd, os.path.join(process_dir, "cwd"))

    def test_detects_prism_java_child_and_instance_directory(self):
        with tempfile.TemporaryDirectory() as root:
            proc_root = os.path.join(root, "proc")
            os.makedirs(proc_root)
            instance_dir = os.path.join(root, "PrismLauncher", "instances", "Modpack", ".minecraft")
            os.makedirs(instance_dir)
            self._process(
                proc_root,
                100,
                b"/usr/bin/PrismLauncher\0",
            )
            self._process(
                proc_root,
                101,
                (
                    f"/usr/lib/jvm/java/bin/java\0"
                    f"-Dminecraft.launcher.brand=PrismLauncher\0"
                    f"--gameDir\0{instance_dir}\0"
                    f"net.minecraft.client.main.Main\0"
                ).encode()
            )
            detector = SteamGameDetector(extra_mount=root, proc_root=proc_root)

            game = detector.detect_running_game()

            self.assertIsNotNone(game)
            self.assertEqual(game["name"], "PrismLauncher")
            self.assertEqual(game["appid"], "3546166962")
            self.assertEqual(game["game_dir"], instance_dir)
            self.assertIsNone(detector.detect_running_game("221100"))
            self.assertEqual(detector.detect_running_game("3546166962")["appid"], "3546166962")

    def test_detects_minecraft_java_child_by_prism_parent_without_steam(self):
        with tempfile.TemporaryDirectory() as root:
            proc_root = os.path.join(root, "proc")
            os.makedirs(proc_root)
            instance_dir = os.path.join(root, "PrismLauncher", "instances", "Vanilla", ".minecraft")
            os.makedirs(instance_dir)
            self._process(
                proc_root,
                100,
                b"/usr/bin/PrismLauncher\0",
                stat="100 (PrismLauncher) S 1 0 0 0 0"
            )
            self._process(
                proc_root,
                101,
                (
                    f"/usr/lib/jvm/java/bin/java\0"
                    f"--gameDir\0{instance_dir}\0"
                    f"net.minecraft.client.main.Main\0"
                ).encode(),
                stat="101 (java) S 100 0 0 0 0"
            )
            detector = SteamGameDetector(extra_mount=root, proc_root=proc_root)

            game = detector.detect_running_game()

            self.assertIsNotNone(game)
            self.assertEqual(game["appid"], "3546166962")
            self.assertEqual(game["game_dir"], instance_dir)

    def test_uses_prism_java_working_directory_when_game_dir_is_not_an_argument(self):
        with tempfile.TemporaryDirectory() as root:
            proc_root = os.path.join(root, "proc")
            os.makedirs(proc_root)
            instance_dir = os.path.join(
                root, "PrismLauncher", "instances", "All the Mods 10", "minecraft"
            )
            os.makedirs(instance_dir)
            self._process(
                proc_root,
                100,
                b"/usr/bin/PrismLauncher\0",
                stat="100 (PrismLauncher) S 1 0 0 0 0"
            )
            self._process(
                proc_root,
                101,
                b"/usr/lib/jvm/java/bin/java\0net.minecraft.client.main.Main\0",
                stat="101 (java) S 100 0 0 0 0",
                cwd=instance_dir
            )
            detector = SteamGameDetector(extra_mount=root, proc_root=proc_root)

            game = detector.detect_running_game()

            self.assertIsNotNone(game)
            self.assertEqual(game["game_dir"], instance_dir)

    def test_prism_java_uses_stable_id_instead_of_steam_shortcut_game_id(self):
        with tempfile.TemporaryDirectory() as root:
            proc_root = os.path.join(root, "proc")
            os.makedirs(proc_root)
            self._process(
                proc_root,
                100,
                b"/usr/bin/PrismLauncher\0",
                stat="100 (PrismLauncher) S 1 0 0 0 0"
            )
            self._process(
                proc_root,
                101,
                (
                    b"/usr/lib/jvm/java/bin/java\0"
                    b"-Dminecraft.launcher.brand=PrismLauncher\0"
                    b"net.minecraft.client.main.Main\0"
                ),
                environ=b"SteamGameId=13295781592262770688\0",
                stat="101 (java) S 100 0 0 0 0"
            )
            detector = SteamGameDetector(extra_mount=root, proc_root=proc_root)

            game = detector.detect_running_game()

            self.assertIsNotNone(game)
            self.assertEqual(game["appid"], "3546166962")

    def test_prism_launcher_without_java_game_is_not_reported_as_running(self):
        with tempfile.TemporaryDirectory() as root:
            proc_root = os.path.join(root, "proc")
            os.makedirs(proc_root)
            self._process(proc_root, 100, b"/usr/bin/PrismLauncher\0")
            detector = SteamGameDetector(extra_mount=root, proc_root=proc_root)

            self.assertIsNone(detector.detect_running_game())

    def test_cli_stats_reports_live_prism_runtime(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
        import config_ipc
        from cli_stats import print_current_runtime

        with patch.object(config_ipc, "read_live_state", return_value={
            "daemon_online": True,
            "game_name": "PrismLauncher",
            "appid": "3546166962",
            "status_text": "ACTIVE & PRE-PINNING",
            "events_processed": 17,
            "events_dropped": 0,
            "event_queue_depth": 2
        }), patch("sys.stdout") as stdout:
            self.assertTrue(print_current_runtime())
            output = "".join(call.args[0] for call in stdout.write.call_args_list)
            self.assertIn("CURRENT RUNTIME: PrismLauncher", output)
            self.assertIn("17 processed, 0 dropped, 2 queued", output)


if __name__ == "__main__":
    unittest.main()
