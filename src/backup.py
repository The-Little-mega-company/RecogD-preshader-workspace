#!/usr/bin/env python3
import argparse
from datetime import datetime
import os
from pathlib import Path
import sys
import tarfile
import tempfile


def archive_brain_graphs(vault_root, output_dir=None):
    vault_path = Path(vault_root).expanduser()
    games_path = vault_path / "games"
    brain_paths = sorted(
        path for path in games_path.glob("*/brain_index/brain.json") if path.is_file()
    )
    if not brain_paths:
        raise FileNotFoundError(f"No brain.json graphs found in {games_path}")

    backup_dir = Path(output_dir or Path.home() / ".cache" / "recogd-backups").expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    archive_path = backup_dir / f"recogd-brains-{timestamp}.tar.gz"

    fd, temp_path = tempfile.mkstemp(prefix=".recogd-brains-", suffix=".tmp", dir=backup_dir)
    os.close(fd)
    try:
        with tarfile.open(temp_path, mode="w:gz") as archive:
            for brain_path in brain_paths:
                archive.add(
                    brain_path,
                    arcname=brain_path.relative_to(vault_path).as_posix(),
                    recursive=False
                )
        os.replace(temp_path, archive_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    return archive_path, len(brain_paths)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Back up learned Preshader brain graphs")
    parser.add_argument("--vault-root", required=True, help="Active RecogD vault directory")
    args = parser.parse_args(argv)
    try:
        archive_path, graph_count = archive_brain_graphs(args.vault_root)
    except (OSError, tarfile.TarError) as exc:
        print(f"[BACKUP] Could not create vault backup: {exc}", file=sys.stderr)
        return 1

    size_mb = archive_path.stat().st_size / (1024 * 1024)
    print(f"[BACKUP] Archived {graph_count} brain.json graphs ({size_mb:.2f} MB)")
    print(f"[BACKUP] {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
