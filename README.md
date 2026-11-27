# RecogD Preshader

RecogD Preshader learns which game assets are commonly opened together and asks Linux to
preload likely next assets into the filesystem cache.

## Installation (Debian/Ubuntu & Arch Linux)

Install using `./install.sh`, which handles system packages (`apt` on Debian/Ubuntu/Mint/Pop!_OS,
`pacman` on Arch/EndeavourOS/CachyOS/Manjaro), installs the `preshader` terminal commands,
registers the **RecogD Preshader Control Deck** application menu entry with its icon, and
enables the background daemon service.

```sh
git clone https://github.com/The-Little-mega-company/RecogD-preshader-workspace.git
cd RecogD-preshader-workspace
chmod +x install.sh
./install.sh
```

> **One-liner install (Debian/Ubuntu & Arch Linux):**
> ```sh
> curl -fsSL https://raw.githubusercontent.com/The-Little-mega-company/RecogD-preshader-workspace/main/web-install.sh | bash
> ```

### Manual package prerequisites (if needed)

```sh
# Debian / Ubuntu
sudo apt update && sudo apt install -y python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 desktop-file-utils hicolor-icon-theme

# Arch Linux / EndeavourOS / CachyOS
sudo pacman -S --needed python python-gobject gtk3 libayatana-appindicator desktop-file-utils hicolor-icon-theme
```

## CLI Commands & Desktop Launcher

- `preshader` — Ensures the background engine is running, opens the GTK Control Deck GUI, and streams live daemon telemetry in the terminal.
- `preshader --GUI` (or `Preshader --GUI`) — Wayland/XWayland recovery launch: resets any stuck GUI process and forces the Control Deck window open.
- `preshader --stats` — Prints live daemon / RAM Governor telemetry and per-game learned brain statistics.
- `preshader --prewarm` — Pre-warms learned asset and shader caches into RAM.
- `preshader --purge` — Releases currently pinned RAM cache via `POSIX_FADV_DONTNEED`.
- `preshader --stop` — Gracefully stops the background daemon and GUI.
- **Application Menu** — Launch **RecogD Preshader Control Deck** directly from your desktop application menu (closing the window hides it to the system tray).
