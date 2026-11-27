# RecogD

RecogD learns which game assets are commonly opened together and asks Linux to
preload likely next assets into the filesystem cache.

## Desktop tray dashboard

The desktop UI includes a system-tray icon, profile selector, start/stop control,
live read/write rates for the daemon, and a recent preload timeline. Closing the
window hides it in the tray; choose **Quit RecogD** from the tray menu to stop the
daemon and exit.

Install the system packages for Python Tkinter and GTK 3 introspection (used by
the tray icon), then install the Python tray packages and start the dashboard.
The daemon uses Linux kernel inotify directly and does not need `inotifywait`.

```sh
Arch:
git clone https://github.com/The-Little-mega-company/RecogD-preshader-workspace.git
cd RecogD-preshader-workspace
chmod +x install.sh
./install.sh
debian apt based distrobutions:
curl -fsSL https://raw.githubusercontent.com/The-Little-mega-company/RecogD-preshader-workspace/main/web-install.sh | bash
```

The selected profile's watch paths must exist. The dashboard reports startup
errors instead of showing the daemon as running. Runtime telemetry and the last
100 successful preload requests are stored in `~/.cache/recogd/runtime.json`.

## Disk Health Check & Corrupted Block Repair

RecogD Preshader includes a built-in disk health and block integrity utility for
the external SSD vault (`/run/media/$USER/preshader` or `/mnt/preshader`) and
local NVMe fallback caches.

- **Read-only health check** (SMART telemetry, read-only `badblocks` check,
  mount read-only detection, and user-space vault/shader cache integrity scan):
  ```sh
  preshader --disk-check
  ```
- **Repair mode** (quarantines corrupted `.json` brains to `.corrupt.bak`,
  removes truncated/unreadable cache files, re-syncs healthy copies from the
  twin vault, and runs `fsck -p` only when the filesystem is unmounted and
  permitted):
  ```sh
  preshader --disk-repair
  # or:
  preshader --disk-check --repair
  ```
- **Control Deck GUI**: Open the **⛨ Disk Health & Repair** tab in the Control
  Deck to run health checks or repairs asynchronously and view live SMART,
  bad-sector, and vault integrity readouts.

> Note: The utility runs safely in user space without escalating privileges. If
> `smartctl` or `badblocks` requires raw block-device permission, it logs a
> warning and continues with the user-space vault integrity scan.
