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
# Debian/Ubuntu
sudo apt install python3-tk python3-gi gir1.2-gtk-3.0

# Fedora
sudo dnf install python3-tkinter python3-gobject gtk3

# Arch Linux
sudo pacman -S tk python-gobject gtk3

python3 -m pip install -r requirements.txt
python3 src/ui.py
```

The selected profile's watch paths must exist. The dashboard reports startup
errors instead of showing the daemon as running. Runtime telemetry and the last
100 successful preload requests are stored in `~/.cache/recogd/runtime.json`.
