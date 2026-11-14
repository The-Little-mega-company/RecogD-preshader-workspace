import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from backup import archive_brain_graphs


class VaultBackupTests(unittest.TestCase):
    def test_archive_contains_brain_graphs_with_portable_vault_paths(self):
        with tempfile.TemporaryDirectory() as root:
            vault = Path(root) / "vault"
            first = vault / "games" / "221100_DayZ" / "brain_index" / "brain.json"
            second = vault / "games" / "489830_Skyrim" / "brain_index" / "brain.json"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_text('{"graph": {"@GAME/a.pbo": {}}}', encoding="utf-8")
            second.write_text('{"graph": {"@WORKSHOP/bsa": {}}}', encoding="utf-8")
            output = Path(root) / "backups"

            archive_path, count = archive_brain_graphs(vault, output)

            self.assertEqual(count, 2)
            with tarfile.open(archive_path, "r:gz") as archive:
                self.assertEqual(set(archive.getnames()), {
                    "games/221100_DayZ/brain_index/brain.json",
                    "games/489830_Skyrim/brain_index/brain.json"
                })
                archived_first = archive.extractfile(
                    "games/221100_DayZ/brain_index/brain.json"
                )
                self.assertEqual(
                    archived_first.read().decode("utf-8"),
                    '{"graph": {"@GAME/a.pbo": {}}}'
                )
            self.assertEqual(first.read_text(encoding="utf-8"), '{"graph": {"@GAME/a.pbo": {}}}')

    def test_archive_fails_without_graphs(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileNotFoundError):
                archive_brain_graphs(Path(root) / "empty-vault", Path(root) / "backups")


if __name__ == "__main__":
    unittest.main()
