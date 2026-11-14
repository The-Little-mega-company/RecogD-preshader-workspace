# RecogD Preshader

**Predictive game-asset preloading and RAM management for Linux.**

RecogD Preshader learns which game assets are opened together, then preloads likely dependencies into the Linux page cache. Its GTK 3 Control Deck lets you monitor activity and tune memory protection while you play.

> **Latest release: 2.0.0-14** · For Arch Linux and Arch-based distributions, including CachyOS and EndeavourOS.

## Install

Download and install the [Preshader 2.0.0-14 package](https://github.com/The-Little-mega-company/precog-workspace/releases/download/v2.0.0-14/preshader-2.0.0-14-any.pkg.tar.zst):

```sh
curl -fLO https://github.com/The-Little-mega-company/precog-workspace/releases/download/v2.0.0-14/preshader-2.0.0-14-any.pkg.tar.zst
sudo pacman -U ./preshader-2.0.0-14-any.pkg.tar.zst
```

Pacman installs the required runtime dependencies with the package. Start RecogD from your application menu or run:

```sh
preshader
```

This starts the daemon and Control Deck. Installing the package does **not** enable the systemd user service. To start RecogD at login, opt in with:

```sh
systemctl --user enable --now recogd-preshader.service
```

To stop an enabled service:

```sh
systemctl --user disable --now recogd-preshader.service
```

### Build from source

```sh
git clone --branch RecogD-Preshader https://github.com/The-Little-mega-company/precog-workspace.git
cd precog-workspace/packaging/aur
makepkg -si
```

## Features

- **Co-access learning:** Watches game files, Workshop content, and shader caches with Linux `inotify`, building per-game `brain.json` graphs of assets opened together.
- **Predictive preloading:** Uses `POSIX_FADV_WILLNEED` to warm likely one- and two-step dependencies in the page cache.
- **Combat protection:** Detects high asset activity and can limit lookahead and background writes to protect game responsiveness.
- **Dual-bus striping:** Mirrors frequently used assets to a mounted `preshader` Ext4 drive and alternates reads across internal and external storage.
- **RAM budget governor:** Monitors available memory and system pressure, limits individual asset pins, and evicts old pins with `POSIX_FADV_DONTNEED`. The protected-free-RAM control can be set as low as **1,500 MB**.
- **GTK Control Deck:** Shows daemon status, the active game, memory usage, drive benchmarks, and controls for cache limits and lookahead.
- **Portable learned graphs:** Saves game, Workshop, and shader-cache paths as `@GAME/`, `@WORKSHOP/`, and `@SHADERCACHE/` tokens so graphs can resolve on another Linux installation.
- **Graph maintenance and per-game limits:** Decays stale graph weights, prunes edges with fewer than two hits, and supports AppID-specific RAM caps.

## Commands

| Command | Description |
| --- | --- |
| `preshader` | Start the daemon and Control Deck |
| `preshader --nogui` | Start the daemon without the Control Deck |
| `preshader --stats` | Print per-game learning and runtime statistics |
| `preshader --bench` | Compare read/write throughput for internal and external drives |
| `preshader --backup` | Archive learned graphs from the active vault |

The drive benchmark uses temporary test files and cleans them up afterward. Run it outside gameplay. Backups are written to `~/.cache/recogd-backups/`.

## Configuration

The persistent configuration is stored at `~/.config/recogd/config.json`. To set a RAM cap in megabytes for a specific Steam AppID, add an `app_ram_caps_mb` map:

```json
{
  "app_ram_caps_mb": {
    "221100": 4096,
    "489830": 2048
  }
}
```

Per-game metadata can also set `max_cache_mb`; when present, that value takes precedence over the AppID map. Games without an override use the Control Deck's global cache limit.

## Storage

RecogD uses an available `preshader` Ext4 drive mounted at `/mnt/preshader` or `/run/media/$USER/preshader`. If the external drive is unavailable, it stores its vault under `~/.cache/recogd-fallback`.

## License

See [LICENSE](LICENSE).
