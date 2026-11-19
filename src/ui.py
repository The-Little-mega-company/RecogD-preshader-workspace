#!/usr/bin/env python3
import argparse
import glob
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError as error:
    raise SystemExit(
        "RecogD's desktop UI needs Tkinter. Install your distribution's "
        "python3-tk package and try again."
    ) from error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from daemon import InProcessPreshaderDaemon
from ram_governor import RamBudgetGovernor
from steam_detector import SteamGameDetector
from vault_merger import VaultCacheMerger, detect_preshader_mounts
from velocity import PredictiveVelocityTracker

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT_DIR, "config.json")
DEFAULT_TELEMETRY = "~/.cache/recogd/runtime.json"
SINGLETON_SOCK = f"/dev/shm/recogd_single_instance_{os.getuid()}.sock"

COLORS = {
    "bg": "#070b14",
    "sidebar": "#0b111e",
    "surface": "#10192b",
    "surface_alt": "#162238",
    "surface_hover": "#1d2d49",
    "border": "#223454",
    "text": "#ebf2ff",
    "muted": "#8da2c4",
    "dim": "#586c8c",
    "cyan": "#2ce5d8",
    "blue": "#5d8bff",
    "green": "#3ce68a",
    "amber": "#ffbe55",
    "red": "#ff5c7c",
    "purple": "#b07cff"
}

PERFORMANCE_PRESETS = {
    "eco": {
        "label": "LVL 1 · ECO / HANDHELD",
        "subtitle": "Steam Deck & 16GB Bazzite · Tight RAM reserve, conservative 1-hop pins",
        "color": COLORS["green"],
        "engine": {
            "time_window_seconds": 0.5,
            "max_file_size_bytes": 8388608,
            "min_confidence_hits": 3,
            "stripe_confidence_hits": 6,
            "max_preload_children": 18,
            "ram_pin_cooldown_seconds": 8.0,
            "max_parent_nodes": 30000
        },
        "velocity": {
            "enabled": True,
            "sample_window_seconds": 1.2,
            "mouse_pan_reference_px_per_sec": 2200.0,
            "asset_burst_reference_per_sec": 30.0,
            "max_preload_multiplier": 1.6,
            "corner_lookahead_threshold": 0.70,
            "max_lookahead_hops": 1,
            "lookahead_children_limit": 6
        },
        "ram_governor": {
            "enabled": True,
            "max_pinned_budget_mb": 768.0,
            "min_free_reserve_mb": 2048.0,
            "soft_available_ratio": 0.32,
            "hard_available_ratio": 0.16,
            "usable_headroom_fraction": 0.25,
            "psi_some_avg10_soft": 4.0,
            "psi_some_avg10_hard": 12.0,
            "swap_spike_threshold_mb": 16.0
        }
    },
    "balanced": {
        "label": "LVL 2 · BALANCED",
        "subtitle": "Default Bazzite Desktop · 2-hop corner lookahead + dynamic RAM governor",
        "color": COLORS["cyan"],
        "engine": {
            "time_window_seconds": 0.75,
            "max_file_size_bytes": 16777216,
            "min_confidence_hits": 2,
            "stripe_confidence_hits": 4,
            "max_preload_children": 35,
            "ram_pin_cooldown_seconds": 5.0,
            "max_parent_nodes": 60000
        },
        "velocity": {
            "enabled": True,
            "sample_window_seconds": 1.5,
            "mouse_pan_reference_px_per_sec": 1800.0,
            "asset_burst_reference_per_sec": 25.0,
            "max_preload_multiplier": 2.5,
            "corner_lookahead_threshold": 0.55,
            "max_lookahead_hops": 2,
            "lookahead_children_limit": 12
        },
        "ram_governor": {
            "enabled": True,
            "max_pinned_budget_mb": 2048.0,
            "min_free_reserve_mb": 1536.0,
            "soft_available_ratio": 0.28,
            "hard_available_ratio": 0.12,
            "usable_headroom_fraction": 0.35,
            "psi_some_avg10_soft": 5.0,
            "psi_some_avg10_hard": 18.0,
            "swap_spike_threshold_mb": 32.0
        }
    },
    "aggressive": {
        "label": "LVL 3 · AGGRESSIVE",
        "subtitle": "DayZ & Open-World Streaming · Early corner lookahead & fast vehicle pre-fetch",
        "color": COLORS["amber"],
        "engine": {
            "time_window_seconds": 1.15,
            "max_file_size_bytes": 33554432,
            "min_confidence_hits": 1,
            "stripe_confidence_hits": 3,
            "max_preload_children": 60,
            "ram_pin_cooldown_seconds": 3.0,
            "max_parent_nodes": 90000
        },
        "velocity": {
            "enabled": True,
            "sample_window_seconds": 1.8,
            "mouse_pan_reference_px_per_sec": 1350.0,
            "asset_burst_reference_per_sec": 18.0,
            "max_preload_multiplier": 3.0,
            "corner_lookahead_threshold": 0.40,
            "max_lookahead_hops": 2,
            "lookahead_children_limit": 18
        },
        "ram_governor": {
            "enabled": True,
            "max_pinned_budget_mb": 4096.0,
            "min_free_reserve_mb": 1280.0,
            "soft_available_ratio": 0.22,
            "hard_available_ratio": 0.10,
            "usable_headroom_fraction": 0.50,
            "psi_some_avg10_soft": 8.0,
            "psi_some_avg10_hard": 22.0,
            "swap_spike_threshold_mb": 48.0
        }
    },
    "ultra": {
        "label": "LVL 4 · ULTRA BENCHMARK",
        "subtitle": "32GB+ Rig · Max dual-bus RAID-0 striping & deep 2-hop predictive horizon",
        "color": COLORS["purple"],
        "engine": {
            "time_window_seconds": 1.5,
            "max_file_size_bytes": 67108864,
            "min_confidence_hits": 1,
            "stripe_confidence_hits": 2,
            "max_preload_children": 90,
            "ram_pin_cooldown_seconds": 1.5,
            "max_parent_nodes": 120000
        },
        "velocity": {
            "enabled": True,
            "sample_window_seconds": 2.0,
            "mouse_pan_reference_px_per_sec": 1000.0,
            "asset_burst_reference_per_sec": 14.0,
            "max_preload_multiplier": 3.5,
            "corner_lookahead_threshold": 0.30,
            "max_lookahead_hops": 2,
            "lookahead_children_limit": 26
        },
        "ram_governor": {
            "enabled": True,
            "max_pinned_budget_mb": 6144.0,
            "min_free_reserve_mb": 1024.0,
            "soft_available_ratio": 0.18,
            "hard_available_ratio": 0.08,
            "usable_headroom_fraction": 0.65,
            "psi_some_avg10_soft": 10.0,
            "psi_some_avg10_hard": 25.0,
            "swap_spike_threshold_mb": 64.0
        }
    }
}

PARAMETER_DEFINITIONS = [
    {
        "section": "MARKOV ASSOCIATION ENGINE",
        "key": ("engine", "max_preload_children"),
        "title": "Max Preload Children per Trigger",
        "desc": "Base number of ranked child assets pre-pinned into RAM when a parent trigger opens.",
        "type": "int",
        "min": 5,
        "max": 120,
        "levels": [("LOW (18)", 18), ("MED (35)", 35), ("HIGH (60)", 60), ("MAX (90)", 90)]
    },
    {
        "section": "MARKOV ASSOCIATION ENGINE",
        "key": ("engine", "time_window_seconds"),
        "title": "Correlation Time Window (sec)",
        "desc": "Sliding window linking consecutive file opens into parent → child Markov edges.",
        "type": "float",
        "min": 0.2,
        "max": 3.0,
        "levels": [("TIGHT (0.45s)", 0.45), ("STD (0.75s)", 0.75), ("WIDE (1.15s)", 1.15), ("DEEP (1.75s)", 1.75)]
    },
    {
        "section": "MARKOV ASSOCIATION ENGINE",
        "key": ("engine", "min_confidence_hits"),
        "title": "Minimum Confidence Hits",
        "desc": "Co-occurrence threshold required before an edge triggers automatic RAM pinning.",
        "type": "int",
        "min": 1,
        "max": 10,
        "levels": [("INSTANT (1)", 1), ("BALANCED (2)", 2), ("STRICT (3)", 3), ("CONSERVATIVE (5)", 5)]
    },
    {
        "section": "MARKOV ASSOCIATION ENGINE",
        "key": ("engine", "max_file_size_bytes"),
        "title": "Max Single Asset Pin Size",
        "desc": "Maximum byte length per file hinted via posix_fadvise(POSIX_FADV_WILLNEED).",
        "type": "bytes_mb",
        "min": 2,
        "max": 128,
        "levels": [("8 MB", 8388608), ("16 MB", 16777216), ("32 MB", 33554432), ("64 MB", 67108864)]
    },
    {
        "section": "MARKOV ASSOCIATION ENGINE",
        "key": ("engine", "ram_pin_cooldown_seconds"),
        "title": "Pin Cooldown Interval (sec)",
        "desc": "Minimum delay before re-issuing kernel page-cache hints for the same asset path.",
        "type": "float",
        "min": 0.5,
        "max": 30.0,
        "levels": [("FAST (1.5s)", 1.5), ("STD (5.0s)", 5.0), ("RELAXED (10s)", 10.0), ("LONG (20s)", 20.0)]
    },
    {
        "section": "PREDICTIVE VELOCITY (ADAPTIVE PRE-FETCH)",
        "key": ("velocity", "max_preload_multiplier"),
        "title": "Velocity Preload Multiplier",
        "desc": "Scales max_preload_children dynamically during rapid camera panning or driving.",
        "type": "float",
        "min": 1.0,
        "max": 4.0,
        "levels": [("1.5x", 1.5), ("2.5x", 2.5), ("3.0x", 3.0), ("3.5x", 3.5)]
    },
    {
        "section": "PREDICTIVE VELOCITY (ADAPTIVE PRE-FETCH)",
        "key": ("velocity", "corner_lookahead_threshold"),
        "title": "Corner Lookahead Trigger Threshold",
        "desc": "Velocity score (0.0–1.0) that activates 2-hop (A → B → C) pre-fetching around corners.",
        "type": "float",
        "min": 0.15,
        "max": 0.95,
        "levels": [("HAIR-TRIGGER (0.30)", 0.30), ("EARLY (0.42)", 0.42), ("STD (0.55)", 0.55), ("LATE (0.75)", 0.75)]
    },
    {
        "section": "PREDICTIVE VELOCITY (ADAPTIVE PRE-FETCH)",
        "key": ("velocity", "lookahead_children_limit"),
        "title": "2nd-Hop Corner Lookahead Limit",
        "desc": "Maximum number of 2nd-degree grandchildren assets pre-pinned during high-velocity turns.",
        "type": "int",
        "min": 2,
        "max": 36,
        "levels": [("LIGHT (6)", 6), ("STD (12)", 12), ("HIGH (18)", 18), ("MAX (28)", 28)]
    },
    {
        "section": "PREDICTIVE VELOCITY (ADAPTIVE PRE-FETCH)",
        "key": ("velocity", "mouse_pan_reference_px_per_sec"),
        "title": "Camera Pan Sensitivity Reference (px/s)",
        "desc": "Mouse/controller angular delta rate that maps to 100% camera pan velocity.",
        "type": "float",
        "min": 500.0,
        "max": 4000.0,
        "levels": [("HIGH SENS (1000)", 1000.0), ("MED (1800)", 1800.0), ("LOW (2600)", 2600.0), ("ULTRA LOW (3400)", 3400.0)]
    },
    {
        "section": "AUTOMATED RAM BUDGET GOVERNOR",
        "key": ("ram_governor", "max_pinned_budget_mb"),
        "title": "Max Pinned RAM Budget Ceiling (MB)",
        "desc": "Upper bound on active page-cache volume held by RecogD before LRU eviction.",
        "type": "float",
        "min": 256.0,
        "max": 12288.0,
        "levels": [("768 MB", 768.0), ("2048 MB", 2048.0), ("4096 MB", 4096.0), ("6144 MB", 6144.0)]
    },
    {
        "section": "AUTOMATED RAM BUDGET GOVERNOR",
        "key": ("ram_governor", "min_free_reserve_mb"),
        "title": "System Free RAM Safety Reserve (MB)",
        "desc": "Hard floor of MemAvailable protected for Bazzite, browser tabs, and background apps.",
        "type": "float",
        "min": 512.0,
        "max": 6144.0,
        "levels": [("1024 MB", 1024.0), ("1536 MB", 1536.0), ("2048 MB", 2048.0), ("3072 MB", 3072.0)]
    },
    {
        "section": "AUTOMATED RAM BUDGET GOVERNOR",
        "key": ("ram_governor", "soft_available_ratio"),
        "title": "Soft Memory Pressure Watermark",
        "desc": "MemAvailable / MemTotal ratio below which pre-pinning begins scaling back gracefully.",
        "type": "float",
        "min": 0.12,
        "max": 0.50,
        "levels": [("18%", 0.18), ("24%", 0.24), ("28%", 0.28), ("35%", 0.35)]
    },
    {
        "section": "AUTOMATED RAM BUDGET GOVERNOR",
        "key": ("ram_governor", "psi_some_avg10_hard"),
        "title": "Linux PSI Stall Thrash Guard (avg10)",
        "desc": "/proc/pressure/memory some avg10 stall % that triggers immediate POSIX_FADV_DONTNEED purge.",
        "type": "float",
        "min": 5.0,
        "max": 40.0,
        "levels": [("STRICT (10%)", 10.0), ("STD (18%)", 18.0), ("RELAXED (25%)", 25.0), ("HIGH (35%)", 35.0)]
    },
    {
        "section": "DUAL-BUS EXTERNAL SSD STRIPING",
        "key": ("external_drive", "max_stripe_file_bytes"),
        "title": "Max Cross-Bus Stripe Asset Size",
        "desc": "Maximum asset size mirrored to the external ext4 SSD hot_stripe_cache for alternating reads.",
        "type": "bytes_mb",
        "min": 1,
        "max": 64,
        "levels": [("4 MB", 4194304), ("8 MB", 8388608), ("16 MB", 16777216), ("32 MB", 33554432)]
    },
    {
        "section": "DUAL-BUS EXTERNAL SSD STRIPING",
        "key": ("engine", "stripe_confidence_hits"),
        "title": "Cross-Bus Stripe Confidence Hits",
        "desc": "Trigger hit count required before mirroring a hot NVMe asset onto the external SSD bus.",
        "type": "int",
        "min": 1,
        "max": 12,
        "levels": [("EAGER (2)", 2), ("STD (4)", 4), ("SELECTIVE (6)", 6), ("RARE (10)", 10)]
    }
]

EXTENSION_GROUPS = {
    "DayZ / Enfusion": [".pbo", ".edds", ".paa", ".rvmat", ".wss", ".anm", ".xob", ".agr", ".asi", ".ast", ".asy", ".ptc"],
    "Vulkan / DXVK Shaders": [".dxvk-cache", ".foz", ".spv", ".idx", ".bin", ".c"],
    "Unreal Engine 4/5": [".pak", ".ucas", ".utoc", ".upk", ".umap", ".uasset", ".ubulk"],
    "Bethesda / Creation": [".bsa", ".ba2", ".nif", ".hkx", ".dds", ".esp", ".esm", ".esl", ".xwm", ".fuz", ".fxp"],
    "Source / Unity / Audio": [".vpk", ".assets", ".resS", ".resource", ".bundle", ".bank", ".fsb", ".ogg", ".wav"]
}


def format_bytes(size):
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def read_process_io(pid):
    values = {}
    with open(f"/proc/{pid}/io", "r", encoding="utf-8") as io_file:
        for line in io_file:
            key, value = line.split(":", 1)
            if key in ("read_bytes", "write_bytes"):
                values[key] = int(value.strip())
    return values["read_bytes"], values["write_bytes"]


class ToggleSwitch(tk.Canvas):
    """
    Sleek vector pill toggle switch built on standard tk.Canvas for 100% Linux compatibility.
    """

    def __init__(self, parent, initial=True, command=None, bg=COLORS["surface"], accent=COLORS["cyan"]):
        super().__init__(parent, width=52, height=26, bg=bg, highlightthickness=0, cursor="hand2")
        self.value = bool(initial)
        self.command = command
        self.accent = accent
        self.bind("<Button-1>", self._toggle)
        self._draw()

    def set(self, value, notify=False):
        self.value = bool(value)
        self._draw()
        if notify and self.command:
            self.command(self.value)

    def get(self):
        return self.value

    def _toggle(self, _event=None):
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)

    def _draw(self):
        self.delete("all")
        track_color = self.accent if self.value else COLORS["border"]
        knob_color = "#06101b" if self.value else COLORS["muted"]
        self.create_oval(2, 2, 24, 24, fill=track_color, outline="")
        self.create_oval(28, 2, 50, 24, fill=track_color, outline="")
        self.create_rectangle(13, 2, 39, 24, fill=track_color, outline="")
        kx = 28 if self.value else 4
        self.create_oval(kx, 4, kx + 18, 22, fill=knob_color, outline="")


class UniversalSystemTray:
    """
    Permanent Multi-Backend Linux System Tray controller with auto-respawn watchdog.
    Ensures the system tray icon never disappears even if plasmashell / desktop panel restarts.
    """

    def __init__(self, app):
        self.app = app
        self.backend_name = "builtin-dock"
        self.tray_icon = None
        self.helper_proc = None
        self.floating_dock = None
        self.stopping = False
        self._start()
        threading.Thread(
            target=self._watchdog_loop,
            name="RecogDTrayWatchdog",
            daemon=True
        ).start()

    def _start(self):
        if self._try_pystray():
            self.backend_name = "pystray"
            return
        if self._try_qt_subprocess():
            self.backend_name = "qt-sni-tray"
            return
        if self._try_gtk_subprocess():
            self.backend_name = "gtk-tray"
            return
        self.backend_name = "floating-mini-dock"

    def _watchdog_loop(self):
        while not self.stopping:
            time.sleep(3.0)
            if self.stopping:
                break
            if self.backend_name in ("qt-sni-tray", "gtk-tray"):
                if self.helper_proc is None or self.helper_proc.poll() is not None:
                    self._start()

    def _try_pystray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return False

        try:
            image = Image.new("RGBA", (64, 64), COLORS["bg"])
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((4, 4, 60, 60), radius=16, fill=COLORS["surface_alt"])
            draw.ellipse((43, 9, 53, 19), fill=COLORS["cyan"])
            draw.text((17, 15), "R", fill=COLORS["text"], stroke_width=1)
            menu = pystray.Menu(
                pystray.MenuItem("Open RecogD Control Deck", lambda *_: self.app.schedule_ui(self.app.show_window), default=True),
                pystray.MenuItem("Pre-Warm All Preshader Caches", lambda *_: self.app.schedule_ui(self.app.prewarm_all_caches)),
                pystray.MenuItem("Pause / Resume Pinning", lambda *_: self.app.schedule_ui(self.app.toggle_pause)),
                pystray.MenuItem("Toggle Combat Spike Shield", lambda *_: self.app.schedule_ui(self.app.toggle_combat_shield)),
                pystray.MenuItem("Preset: LVL 1 Eco / Handheld", lambda *_: self.app.schedule_ui(lambda: self.app.apply_preset("eco"))),
                pystray.MenuItem("Preset: LVL 2 Balanced", lambda *_: self.app.schedule_ui(lambda: self.app.apply_preset("balanced"))),
                pystray.MenuItem("Preset: LVL 3 Aggressive", lambda *_: self.app.schedule_ui(lambda: self.app.apply_preset("aggressive"))),
                pystray.MenuItem("Preset: LVL 4 Ultra Benchmark", lambda *_: self.app.schedule_ui(lambda: self.app.apply_preset("ultra"))),
                pystray.MenuItem("Purge Pinned RAM Cache", lambda *_: self.app.schedule_ui(self.app.purge_ram_cache))
            )
            self.tray_icon = pystray.Icon("recogd", image, "RecogD · Predictive Asset Preloader", menu=menu)
            self.tray_icon.run_detached()
            return True
        except Exception:
            return False

    def _try_qt_subprocess(self):
        qt_mod = None
        for candidate in ("PyQt6", "PyQt5"):
            try:
                __import__(candidate)
                qt_mod = candidate
                break
            except ImportError:
                continue
        if not qt_mod:
            return False

        helper_code = f"""
import sys, time
from {qt_mod}.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from {qt_mod}.QtGui import QIcon, QPixmap, QPainter, QColor, QFont

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)
for _ in range(15):
    if QSystemTrayIcon.isSystemTrayAvailable():
        break
    time.sleep(1.0)

pix = QPixmap(64, 64)
pix.fill(QColor("#070b14"))
p = QPainter(pix)
p.setRenderHint(QPainter.RenderHint.Antialiasing)
p.setBrush(QColor("#162238"))
p.setPen(QColor("#2ce5d8"))
p.drawRoundedRect(4, 4, 56, 56, 14, 14)
p.setBrush(QColor("#2ce5d8"))
p.drawEllipse(42, 10, 10, 10)
p.setPen(QColor("#ebf2ff"))
f = QFont("Sans", 22)
f.setBold(True)
p.setFont(f)
p.drawText(16, 44, "R")
p.end()

tray = QSystemTrayIcon(QIcon(pix))
tray.setToolTip("RecogD Preshader · Control Deck (Permanent Tray)")
menu = QMenu()

actions = [
    ("Open RecogD Control Deck", "SHOW"),
    ("Pre-Warm All Preshader Caches", "PREWARM"),
    ("Pause / Resume Pinning", "TOGGLE_PAUSE"),
    ("Toggle Combat Spike Shield", "TOGGLE_SHIELD"),
    ("Preset: LVL 1 Eco / Handheld", "PRESET:eco"),
    ("Preset: LVL 2 Balanced", "PRESET:balanced"),
    ("Preset: LVL 3 Aggressive", "PRESET:aggressive"),
    ("Preset: LVL 4 Ultra Benchmark", "PRESET:ultra"),
    ("Purge Pinned RAM Cache (DONTNEED)", "PURGE"),
    ("Hide Window to Tray", "HIDE"),
]
for label, cmd in actions:
    act = menu.addAction(label)
    act.triggered.connect(lambda _checked=False, c=cmd: print(c, flush=True))

tray.setContextMenu(menu)
tray.activated.connect(lambda reason: print("SHOW", flush=True))
tray.show()
print("READY", flush=True)
sys.exit(app.exec())
"""
        return self._spawn_helper(helper_code)

    def _try_gtk_subprocess(self):
        helper_code = """
import sys
try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
except Exception:
    sys.exit(2)

icon = Gtk.StatusIcon()
icon.set_from_icon_name("utilities-system-monitor")
icon.set_tooltip_text("RecogD Preshader · Control Deck")
icon.connect("activate", lambda *_: print("SHOW", flush=True))

def on_popup(status_icon, button, activate_time):
    menu = Gtk.Menu()
    for label, cmd in [
        ("Open Control Deck", "SHOW"),
        ("Pre-Warm All Preshader Caches", "PREWARM"),
        ("Pause / Resume Pinning", "TOGGLE_PAUSE"),
        ("Toggle Combat Spike Shield", "TOGGLE_SHIELD"),
        ("Purge Pinned RAM Cache", "PURGE")
    ]:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", lambda _w, c=cmd: print(c, flush=True))
        menu.append(item)
    menu.show_all()
    menu.popup(None, None, None, status_icon, button, activate_time)

icon.connect("popup-menu", on_popup)
icon.set_visible(True)
print("READY", flush=True)
Gtk.main()
"""
        return self._spawn_helper(helper_code)

    def _spawn_helper(self, code):
        try:
            proc = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1
            )
            self.helper_proc = proc
            threading.Thread(
                target=self._listen_helper,
                args=(proc,),
                name="RecogDTrayBridge",
                daemon=True
            ).start()
            return True
        except OSError:
            return False

    def _listen_helper(self, proc):
        if proc.stdout is None:
            return
        for raw_line in proc.stdout:
            cmd = raw_line.strip()
            if cmd == "SHOW":
                self.app.schedule_ui(self.app.show_window)
            elif cmd == "HIDE":
                self.app.schedule_ui(self.app.hide_window)
            elif cmd == "PREWARM":
                self.app.schedule_ui(self.app.prewarm_all_caches)
            elif cmd == "TOGGLE_PAUSE":
                self.app.schedule_ui(self.app.toggle_pause)
            elif cmd == "TOGGLE_SHIELD":
                self.app.schedule_ui(self.app.toggle_combat_shield)
            elif cmd.startswith("PRESET:"):
                preset_key = cmd.split(":", 1)[1]
                self.app.schedule_ui(lambda p=preset_key: self.app.apply_preset(p))
            elif cmd == "PURGE":
                self.app.schedule_ui(self.app.purge_ram_cache)

    def show_floating_dock(self):
        if self.floating_dock is not None:
            return
        dock = tk.Toplevel(self.app.root)
        dock.title("RecogD Mini-Tray")
        dock.geometry("240x42+36+36")
        dock.configure(bg=COLORS["surface_alt"], highlightthickness=1, highlightbackground=COLORS["cyan"])
        dock.attributes("-topmost", True)
        tk.Label(
            dock, text="⚡ RECOGD TRAY", bg=COLORS["surface_alt"], fg=COLORS["cyan"],
            font=("Sans", 9, "bold")
        ).pack(side="left", padx=10)
        tk.Button(
            dock, text="Open Control Deck", command=self.app.show_window,
            bg=COLORS["cyan"], fg="#06101b", relief="flat", font=("Sans", 8, "bold"), padx=8, pady=2
        ).pack(side="right", padx=6, pady=6)
        self.floating_dock = dock

    def hide_floating_dock(self):
        if self.floating_dock is not None:
            try:
                self.floating_dock.destroy()
            except tk.TclError:
                pass
            self.floating_dock = None

    def stop(self):
        self.stopping = True
        self.hide_floating_dock()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        if self.helper_proc is not None and self.helper_proc.poll() is None:
            try:
                self.helper_proc.terminate()
            except OSError:
                pass


class RecogDDesktop:
    def __init__(
        self,
        config_path=DEFAULT_CONFIG,
        telemetry_path=DEFAULT_TELEMETRY,
        headless=False,
        start_minimized=False
    ):
        self.root_dir = ROOT_DIR
        self.config_path = os.path.abspath(os.path.expanduser(config_path))
        self.telemetry_path = os.path.abspath(os.path.expanduser(telemetry_path))
        self.headless = headless
        self.start_minimized = start_minimized

        self.config = self._load_or_init_config()
        ext_mount, ext_vault, nvme_vault = detect_preshader_mounts()
        self.merger = VaultCacheMerger(ext_vault=ext_vault, nvme_vault=nvme_vault)
        self.steam_detector = SteamGameDetector(extra_mount=ext_mount)

        self.output_lines = deque(maxlen=200)
        self.raw_log_history = deque(maxlen=400)
        self.terminal_subscribers = []
        self.output_lock = threading.Lock()

        # Single-process unified daemon engine
        self.daemon = InProcessPreshaderDaemon(
            config_path=self.config_path,
            vault_root=self.merger.primary_vault_root(),
            ext_mount=ext_mount,
            telemetry_path=self.telemetry_path,
            log_callback=self._on_daemon_log
        )
        self.local_governor = self.daemon.ram_governor
        self.local_velocity = self.daemon.velocity_tracker

        self.cached_vault_entries = self.merger.discover_precompiled_caches()
        self.targets = self._build_targets()
        self.active_preset = self._detect_matching_preset()

        self.last_io = None
        self.last_io_time = None
        self.read_rate = 0.0
        self.write_rate = 0.0
        self.io_samples = deque(maxlen=56)
        self.vel_samples = deque(maxlen=56)

        self.param_widgets = {}
        self.toggle_widgets = {}
        self.preset_buttons = {}
        self.ext_vars = {}
        self.current_view = "deck"
        self.ipc_server_sock = None

        if self.headless:
            return

        self._start_ipc_server()
        self.daemon.start_background()

        if not os.environ.get("DISPLAY"):
            for sock in sorted(glob.glob("/tmp/.X11-unix/X*")):
                disp_num = os.path.basename(sock)[1:]
                if disp_num.isdigit():
                    os.environ["DISPLAY"] = f":{disp_num}"
                    break

        try:
            self.root = tk.Tk(className="recogd-preshader")
        except tk.TclError as err:
            self.headless = True
            print(
                f"[RECOGD-GUI] No active X11/XWayland DISPLAY found ({err}). "
                "Running unified daemon in headless mode.",
                flush=True
            )
            return

        self.root.title("RecogD Preshader · Unified Control Deck")
        self.root.geometry("1260x860")
        self.root.minsize(1060, 740)
        self.root.configure(bg=COLORS["bg"])
        self._apply_window_icon()
        # Window close button ALWAYS minimizes to permanent system tray
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self._configure_theme()
        self._build_layout()
        self.tray = UniversalSystemTray(self)
        self.status_tray_badge.configure(text=f"TRAY: PERMANENT ({self.tray.backend_name.upper()})")

        if self.start_minimized:
            self.root.withdraw()

        self.root.after(300, self._refresh)

    def _apply_window_icon(self):
        icon_candidates = [
            os.path.expanduser("~/.local/share/icons/hicolor/256x256/apps/recogd-preshader.png"),
            os.path.expanduser("~/.local/share/icons/hicolor/64x64/apps/recogd-preshader.png")
        ]
        for path in icon_candidates:
            if os.path.exists(path):
                try:
                    self._window_icon = tk.PhotoImage(file=path)
                    self.root.iconphoto(True, self._window_icon)
                    return
                except tk.TclError:
                    pass

    def _start_ipc_server(self):
        try:
            if os.path.exists(SINGLETON_SOCK):
                os.unlink(SINGLETON_SOCK)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(SINGLETON_SOCK)
            srv.listen(5)
            self.ipc_server_sock = srv
            threading.Thread(
                target=self._ipc_accept_loop,
                args=(srv,),
                name="RecogDSingleInstanceIPC",
                daemon=True
            ).start()
        except OSError:
            pass

    def _ipc_accept_loop(self, srv):
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            keep_open = False
            try:
                cmd = conn.recv(256).decode("utf-8", errors="ignore").strip()
                if cmd == "SHOW":
                    self.schedule_ui(self.show_window)
                    conn.sendall(b"OK: Window brought to foreground.\n")
                elif cmd == "ATTACH_TERMINAL":
                    keep_open = True
                    self.schedule_ui(self.show_window)
                    with self.output_lock:
                        history_payload = "\n".join(self.raw_log_history)
                        if history_payload:
                            conn.sendall((history_payload + "\n").encode("utf-8"))
                        self.terminal_subscribers.append(conn)
                    threading.Thread(
                        target=self._watch_terminal_subscriber,
                        args=(conn,),
                        name="RecogDTerminalSubscriber",
                        daemon=True
                    ).start()
                elif cmd == "PREWARM":
                    files_p, bytes_p = self.daemon.prewarm_caches()
                    conn.sendall(f"OK: Pre-warmed {files_p} files ({bytes_p / (1024 * 1024):.2f} MB).\n".encode("utf-8"))
                elif cmd == "PURGE":
                    ev_f, ev_b = self.daemon.purge_ram_cache()
                    conn.sendall(f"OK: Purged {ev_f} files ({ev_b / (1024 * 1024):.2f} MB).\n".encode("utf-8"))
                elif cmd == "STATUS":
                    st = self.daemon.get_combined_stats()
                    conn.sendall((json.dumps(st, indent=2) + "\n").encode("utf-8"))
                elif cmd in ("DISK_CHECK", "DISK_REPAIR"):
                    repair_mode = (cmd == "DISK_REPAIR")
                    report = self.daemon.run_disk_health_check(repair=repair_mode)
                    self.schedule_ui(lambda r=report: self._render_disk_health_report(r))
                    conn.sendall((report["summary_text"] + "\n").encode("utf-8"))
                else:
                    self.schedule_ui(self.show_window)
                    conn.sendall(b"OK\n")
            except OSError:
                keep_open = False
            finally:
                if not keep_open:
                    try:
                        conn.close()
                    except OSError:
                        pass

    def _watch_terminal_subscriber(self, conn):
        try:
            while True:
                data = conn.recv(64)
                if not data:
                    break
                if b"DETACH" in data:
                    break
        except OSError:
            pass
        finally:
            with self.output_lock:
                if conn in self.terminal_subscribers:
                    self.terminal_subscribers.remove(conn)
            try:
                self.daemon.save_brains()
            except Exception:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def _on_daemon_log(self, message):
        with self.output_lock:
            self.raw_log_history.append(message)
            dead_subs = []
            for sub in self.terminal_subscribers:
                try:
                    sub.sendall((message + "\n").encode("utf-8"))
                except OSError:
                    dead_subs.append(sub)
            for sub in dead_subs:
                self.terminal_subscribers.remove(sub)
        self.schedule_ui(lambda m=message: self._append_log(m))

    def schedule_ui(self, callback):
        if not self.headless and hasattr(self, "root"):
            try:
                self.root.after(0, callback)
            except tk.TclError:
                pass

    def _load_or_init_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, json.JSONDecodeError):
            cfg = {}

        cfg.setdefault("engine", dict(PERFORMANCE_PRESETS["balanced"]["engine"]))
        cfg["engine"].setdefault("autosave_interval_seconds", 180)
        cfg["engine"].setdefault("process_scan_interval_seconds", 2.0)
        cfg.setdefault("velocity", dict(PERFORMANCE_PRESETS["balanced"]["velocity"]))
        cfg.setdefault("ram_governor", dict(PERFORMANCE_PRESETS["balanced"]["ram_governor"]))
        cfg["ram_governor"].setdefault("poll_interval_seconds", 0.5)
        cfg.setdefault("external_drive", {
            "mount_point": "/run/media/kessia/preshader",
            "label": "preshader",
            "enable_cross_bus_striping": True,
            "max_stripe_file_bytes": 8388608,
            "read_ahead_kb": 4096
        })
        cfg.setdefault("target_extensions", [
            ext for group in EXTENSION_GROUPS.values() for ext in group
        ])
        return cfg

    def save_config(self, reason="Updated configuration"):
        try:
            tmp_path = f"{self.config_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
            os.replace(tmp_path, self.config_path)
            self.daemon.reload_config(self.config)
            self._append_log(f"[GUI-CONFIG] {reason} -> Applied live in single process")
        except OSError as err:
            self._append_log(f"[GUI-ERROR] Could not save config.json: {err}")

    def _build_targets(self):
        targets = {
            "autonomous": {
                "label": "⚡ All Unified Vaults + Autonomous Steam Detector",
                "mode": "autonomous",
                "safe_id": None
            }
        }
        for entry in self.cached_vault_entries:
            sid = entry["safe_id"]
            c_count = len(entry["cache_files"])
            c_mb = entry["cache_bytes"] / (1024 * 1024)
            targets[f"vault:{sid}"] = {
                "label": f"📦 {entry['game_name']} ({c_count} cached · {c_mb:.1f} MB · {entry['nodes']:,} triggers)",
                "mode": "vault",
                "safe_id": sid
            }
        for appid, info in sorted(self.steam_detector.manifests.items(), key=lambda x: x[1]["name"]):
            sid = info["safe_id"]
            if f"vault:{sid}" not in targets:
                targets[f"steam:{appid}"] = {
                    "label": f"🎮 {info['name']} (Steam AppID {appid})",
                    "mode": "steam",
                    "safe_id": sid
                }
        return targets

    def _detect_matching_preset(self):
        max_children = self.config.get("engine", {}).get("max_preload_children", 35)
        if max_children <= 20:
            return "eco"
        if max_children <= 40:
            return "balanced"
        if max_children <= 70:
            return "aggressive"
        return "ultra"

    def _configure_theme(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Recog.Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            borderwidth=0,
            rowheight=30,
            font=("Sans", 9)
        )
        style.configure(
            "Recog.Treeview.Heading",
            background=COLORS["surface_alt"],
            foreground=COLORS["muted"],
            borderwidth=0,
            font=("Sans", 8, "bold")
        )
        style.map(
            "Recog.Treeview",
            background=[("selected", COLORS["surface_hover"])],
            foreground=[("selected", COLORS["cyan"])]
        )
        style.configure(
            "Recog.TCombobox",
            fieldbackground=COLORS["surface_alt"],
            background=COLORS["surface_alt"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["cyan"],
            bordercolor=COLORS["border"],
            padding=7
        )

    def _build_layout(self):
        self.sidebar = tk.Frame(
            self.root, bg=COLORS["sidebar"], width=265, padx=18, pady=20,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        tk.Label(
            self.sidebar, text="⚡ PRESHADER // SINGLE-PROCESS", bg=COLORS["sidebar"],
            fg=COLORS["cyan"], font=("Sans", 8, "bold")
        ).pack(anchor="w")
        tk.Label(
            self.sidebar, text="Control Deck", bg=COLORS["sidebar"],
            fg=COLORS["text"], font=("Sans", 20, "bold")
        ).pack(anchor="w", pady=(2, 2))
        tk.Label(
            self.sidebar, text="Dual-Bus Caches · Velocity · RAM Governor",
            bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Sans", 8)
        ).pack(anchor="w", pady=(0, 16))

        self.nav_buttons = {}
        nav_items = [
            ("deck", "◈   Command Deck"),
            ("matrix", "⚙   Parameter Matrix"),
            ("vault", "❖   Precompiled Caches"),
            ("health", "⛨   Disk Health & Repair"),
            ("logs", "▣   Kernel & Daemon Log")
        ]
        for key, label in nav_items:
            btn = tk.Button(
                self.sidebar,
                text=label,
                anchor="w",
                command=lambda k=key: self.switch_view(k),
                bg=COLORS["surface_alt"] if key == "deck" else COLORS["sidebar"],
                fg=COLORS["cyan"] if key == "deck" else COLORS["muted"],
                activebackground=COLORS["surface_hover"],
                activeforeground=COLORS["text"],
                relief="flat",
                borderwidth=0,
                padx=14,
                pady=10,
                cursor="hand2",
                font=("Sans", 10, "bold")
            )
            btn.pack(fill="x", pady=3)
            self.nav_buttons[key] = btn

        # Sidebar Unified Engine Action Card (every button is 100% functional in-process)
        daemon_card = tk.Frame(
            self.sidebar, bg=COLORS["surface"], padx=14, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        daemon_card.pack(fill="x", pady=(16, 10))
        status_row = tk.Frame(daemon_card, bg=COLORS["surface"])
        status_row.pack(fill="x")
        self.status_dot = tk.Canvas(status_row, width=12, height=12, bg=COLORS["surface"], highlightthickness=0)
        self.status_dot.pack(side="left", pady=2)
        self.status_circle = self.status_dot.create_oval(2, 2, 10, 10, fill=COLORS["green"], outline="")
        self.status_label = tk.Label(
            status_row, text="UNIFIED ENGINE ONLINE", bg=COLORS["surface"], fg=COLORS["green"],
            font=("Sans", 8, "bold")
        )
        self.status_label.pack(side="left", padx=6)

        self.active_game_label = tk.Label(
            daemon_card,
            text=f"Vault: {self.merger.ext_mount} connected",
            bg=COLORS["surface"], fg=COLORS["muted"], font=("Sans", 8),
            wraplength=200, justify="left"
        )
        self.active_game_label.pack(anchor="w", pady=(6, 10))

        self.pause_button = tk.Button(
            daemon_card,
            text="⏸  PAUSE PINNING ENGINE",
            command=self.toggle_pause,
            bg=COLORS["cyan"],
            fg="#05101a",
            activebackground="#6ef7ec",
            activeforeground="#05101a",
            font=("Sans", 9, "bold"),
            relief="flat",
            cursor="hand2",
            pady=8,
            borderwidth=0
        )
        self.pause_button.pack(fill="x")

        self.shield_button = tk.Button(
            daemon_card,
            text="🛡  COMBAT SPIKE SHIELD: OFF",
            command=self.toggle_combat_shield,
            bg=COLORS["surface_alt"],
            fg=COLORS["text"],
            activebackground=COLORS["red"],
            activeforeground="#05101a",
            font=("Sans", 8, "bold"),
            relief="flat",
            cursor="hand2",
            pady=7,
            borderwidth=0
        )
        self.shield_button.pack(fill="x", pady=(7, 0))

        tk.Button(
            daemon_card,
            text="⚡  Pre-Warm Preshader Caches",
            command=self.prewarm_all_caches,
            bg=COLORS["surface_alt"],
            fg=COLORS["green"],
            activebackground=COLORS["surface_hover"],
            activeforeground=COLORS["green"],
            font=("Sans", 8, "bold"),
            relief="flat",
            cursor="hand2",
            pady=7,
            borderwidth=0
        ).pack(fill="x", pady=(7, 0))

        tk.Button(
            daemon_card,
            text="🧹  Purge Pinned RAM (DONTNEED)",
            command=self.purge_ram_cache,
            bg=COLORS["surface_alt"],
            fg=COLORS["amber"],
            activebackground=COLORS["surface_hover"],
            activeforeground=COLORS["amber"],
            font=("Sans", 8, "bold"),
            relief="flat",
            cursor="hand2",
            pady=7,
            borderwidth=0
        ).pack(fill="x", pady=(7, 0))

        tk.Button(
            daemon_card,
            text="🚀  Test Corner Velocity Burst",
            command=self.simulate_velocity_burst,
            bg=COLORS["surface_alt"],
            fg=COLORS["purple"],
            activebackground=COLORS["surface_hover"],
            activeforeground=COLORS["purple"],
            font=("Sans", 8, "bold"),
            relief="flat",
            cursor="hand2",
            pady=7,
            borderwidth=0
        ).pack(fill="x", pady=(7, 0))

        footer = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        footer.pack(side="bottom", fill="x")
        self.status_tray_badge = tk.Label(
            footer, text="TRAY: PERMANENT", bg=COLORS["sidebar"], fg=COLORS["cyan"], font=("Sans", 8, "bold")
        )
        self.status_tray_badge.pack(anchor="w")
        self.bus_badge = tk.Label(
            footer,
            text=f"CACHES: {len(self.cached_vault_entries)} VAULTS | PID: {os.getpid()}",
            bg=COLORS["sidebar"], fg=COLORS["muted"], font=("Sans", 8)
        )
        self.bus_badge.pack(anchor="w", pady=(2, 8))
        tk.Button(
            footer, text="⬇  Hide to Permanent Tray", command=self.hide_window,
            bg=COLORS["surface"], fg=COLORS["muted"], relief="flat", font=("Sans", 8, "bold"), pady=6, cursor="hand2"
        ).pack(fill="x")

        self.main_area = tk.Frame(self.root, bg=COLORS["bg"], padx=22, pady=18)
        self.main_area.pack(side="left", fill="both", expand=True)

        self.views = {
            "deck": tk.Frame(self.main_area, bg=COLORS["bg"]),
            "matrix": tk.Frame(self.main_area, bg=COLORS["bg"]),
            "vault": tk.Frame(self.main_area, bg=COLORS["bg"]),
            "health": tk.Frame(self.main_area, bg=COLORS["bg"]),
            "logs": tk.Frame(self.main_area, bg=COLORS["bg"])
        }

        self._build_deck_view(self.views["deck"])
        self._build_matrix_view(self.views["matrix"])
        self._build_vault_view(self.views["vault"])
        self._build_health_view(self.views["health"])
        self._build_logs_view(self.views["logs"])

        self.switch_view("deck")

    def switch_view(self, view_key):
        for k, frame in self.views.items():
            frame.pack_forget()
        self.views[view_key].pack(fill="both", expand=True)
        self.current_view = view_key
        for k, btn in self.nav_buttons.items():
            if k == view_key:
                btn.configure(bg=COLORS["surface_alt"], fg=COLORS["cyan"])
            else:
                btn.configure(bg=COLORS["sidebar"], fg=COLORS["muted"])
        if view_key == "vault":
            self._refresh_vault_inspector()

    def _build_deck_view(self, parent):
        top_bar = tk.Frame(
            parent, bg=COLORS["surface"], padx=16, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        top_bar.pack(fill="x")

        target_row = tk.Frame(top_bar, bg=COLORS["surface"])
        target_row.pack(fill="x")
        tk.Label(
            target_row, text="ACTIVE VAULT / GAME SCOPE", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Sans", 8, "bold")
        ).pack(side="left", padx=(0, 10))

        target_labels = [v["label"] for v in self.targets.values()]
        self.target_var = tk.StringVar(value=target_labels[0])
        self.target_combo = ttk.Combobox(
            target_row, textvariable=self.target_var, values=target_labels,
            state="readonly", style="Recog.TCombobox", width=48
        )
        self.target_combo.pack(side="left")
        self.target_combo.bind("<<ComboboxSelected>>", self._on_target_scope_selected)

        tk.Button(
            target_row,
            text="⚡ Pin Selected Scope Now",
            command=self._on_target_scope_selected,
            bg=COLORS["cyan"],
            fg="#05101a",
            activebackground="#6ef7ec",
            relief="flat",
            padx=12,
            pady=5,
            cursor="hand2",
            font=("Sans", 8, "bold")
        ).pack(side="left", padx=(10, 0))

        self.preset_desc_label = tk.Label(
            target_row,
            text=PERFORMANCE_PRESETS[self.active_preset]["subtitle"],
            bg=COLORS["surface"], fg=COLORS["cyan"], font=("Sans", 8)
        )
        self.preset_desc_label.pack(side="right")

        preset_row = tk.Frame(top_bar, bg=COLORS["surface"])
        preset_row.pack(fill="x", pady=(10, 0))
        tk.Label(
            preset_row, text="PERFORMANCE TIER", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Sans", 8, "bold")
        ).pack(side="left", padx=(0, 10))

        for p_key, p_data in PERFORMANCE_PRESETS.items():
            is_sel = p_key == self.active_preset
            btn = tk.Button(
                preset_row,
                text=p_data["label"],
                command=lambda k=p_key: self.apply_preset(k),
                bg=p_data["color"] if is_sel else COLORS["surface_alt"],
                fg="#05101a" if is_sel else COLORS["text"],
                activebackground=p_data["color"],
                activeforeground="#05101a",
                relief="flat",
                borderwidth=0,
                padx=14,
                pady=6,
                cursor="hand2",
                font=("Sans", 8, "bold")
            )
            btn.pack(side="left", padx=(0, 8))
            self.preset_buttons[p_key] = btn

        # 6 Functioning Live Feature Toggle Switches
        switches_frame = tk.Frame(parent, bg=COLORS["bg"])
        switches_frame.pack(fill="x", pady=(12, 12))
        for col in range(3):
            switches_frame.columnconfigure(col, weight=1)

        toggle_specs = [
            (
                "velocity_enabled",
                "Adaptive Velocity Pre-Fetch",
                "Scale pre-pin depth with camera pan & speed",
                bool(self.config.get("velocity", {}).get("enabled", True)),
                COLORS["cyan"],
                self._on_toggle_velocity
            ),
            (
                "corner_lookahead",
                "2-Hop Corner Lookahead",
                "Pre-pin A → B → C assets before turning corners",
                int(self.config.get("velocity", {}).get("max_lookahead_hops", 2)) >= 2,
                COLORS["purple"],
                self._on_toggle_corner_lookahead
            ),
            (
                "ram_governor_enabled",
                "Automated RAM Governor",
                "Scale & evict page cache to prevent swap thrash",
                bool(self.config.get("ram_governor", {}).get("enabled", True)),
                COLORS["green"],
                self._on_toggle_ram_governor
            ),
            (
                "cross_bus_striping",
                "Dual-Bus SSD Striping",
                "Alternating reads across NVMe + /run/media/kessia/preshader",
                bool(self.config.get("external_drive", {}).get("enable_cross_bus_striping", True)),
                COLORS["amber"],
                self._on_toggle_striping
            ),
            (
                "instant_confidence",
                "Single-Hit Instant Trigger",
                "Pin on 1st co-occurrence (min_confidence = 1)",
                int(self.config.get("engine", {}).get("min_confidence_hits", 2)) == 1,
                COLORS["blue"],
                self._on_toggle_instant_confidence
            ),
            (
                "combat_shield_switch",
                "Combat Spike Shield",
                "Lock 1-hop & shed SSD writes to protect 1% low FPS",
                self.daemon.combat_shield,
                COLORS["red"],
                self._on_toggle_combat_shield_switch
            )
        ]

        for idx, (t_key, title, subtitle, initial, color, callback) in enumerate(toggle_specs):
            r, c = divmod(idx, 3)
            card = tk.Frame(
                switches_frame, bg=COLORS["surface"], padx=14, pady=9,
                highlightthickness=1, highlightbackground=COLORS["border"]
            )
            card.grid(row=r, column=c, sticky="nsew", padx=(0 if c == 0 else 8, 0), pady=(0 if r == 0 else 8, 0))
            txt_col = tk.Frame(card, bg=COLORS["surface"])
            txt_col.pack(side="left", fill="both", expand=True)
            tk.Label(
                txt_col, text=title, bg=COLORS["surface"], fg=COLORS["text"], font=("Sans", 9, "bold")
            ).pack(anchor="w")
            tk.Label(
                txt_col, text=subtitle, bg=COLORS["surface"], fg=COLORS["muted"], font=("Sans", 8)
            ).pack(anchor="w", pady=(2, 0))
            sw = ToggleSwitch(card, initial=initial, command=callback, bg=COLORS["surface"], accent=color)
            sw.pack(side="right", padx=(8, 0))
            self.toggle_widgets[t_key] = sw

        # Dual Core Telemetry Radar
        cores_row = tk.Frame(parent, bg=COLORS["bg"])
        cores_row.pack(fill="x", pady=(0, 12))
        cores_row.columnconfigure(0, weight=1)
        cores_row.columnconfigure(1, weight=1)

        vel_card = tk.Frame(
            cores_row, bg=COLORS["surface"], padx=16, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        vel_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        vel_hdr = tk.Frame(vel_card, bg=COLORS["surface"])
        vel_hdr.pack(fill="x")
        tk.Label(
            vel_hdr, text="PREDICTIVE VELOCITY ENGINE", bg=COLORS["surface"],
            fg=COLORS["cyan"], font=("Sans", 9, "bold")
        ).pack(side="left")
        self.vel_state_badge = tk.Label(
            vel_hdr, text="IDLE · 1.0x", bg=COLORS["surface_alt"], fg=COLORS["cyan"],
            font=("Sans", 8, "bold"), padx=8, pady=2
        )
        self.vel_state_badge.pack(side="right")

        self.vel_bar = tk.Canvas(vel_card, height=20, bg=COLORS["surface_alt"], highlightthickness=0)
        self.vel_bar.pack(fill="x", pady=(8, 6))

        vel_stats_row = tk.Frame(vel_card, bg=COLORS["surface"])
        vel_stats_row.pack(fill="x")
        self.vel_score_lbl = tk.Label(
            vel_stats_row, text="Velocity: 0.00 (Peak 0.00)", bg=COLORS["surface"],
            fg=COLORS["text"], font=("Sans", 9, "bold")
        )
        self.vel_score_lbl.pack(side="left")
        self.vel_lookahead_lbl = tk.Label(
            vel_stats_row, text="Corner Lookahead Pins: 0 | Hops: 2", bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Sans", 8)
        )
        self.vel_lookahead_lbl.pack(side="right")

        ram_card = tk.Frame(
            cores_row, bg=COLORS["surface"], padx=16, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        ram_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ram_hdr = tk.Frame(ram_card, bg=COLORS["surface"])
        ram_hdr.pack(fill="x")
        tk.Label(
            ram_hdr, text="AUTOMATED RAM BUDGET GOVERNOR", bg=COLORS["surface"],
            fg=COLORS["green"], font=("Sans", 9, "bold")
        ).pack(side="left")
        self.ram_state_badge = tk.Label(
            ram_hdr, text="OPTIMAL · 100%", bg=COLORS["surface_alt"], fg=COLORS["green"],
            font=("Sans", 8, "bold"), padx=8, pady=2
        )
        self.ram_state_badge.pack(side="right")

        self.ram_bar = tk.Canvas(ram_card, height=20, bg=COLORS["surface_alt"], highlightthickness=0)
        self.ram_bar.pack(fill="x", pady=(8, 6))

        ram_stats_row = tk.Frame(ram_card, bg=COLORS["surface"])
        ram_stats_row.pack(fill="x")
        self.ram_avail_lbl = tk.Label(
            ram_stats_row, text="Avail RAM: -- MB | Budget: -- MB", bg=COLORS["surface"],
            fg=COLORS["text"], font=("Sans", 9, "bold")
        )
        self.ram_avail_lbl.pack(side="left")
        self.ram_evict_lbl = tk.Label(
            ram_stats_row, text="Active Cache: 0 B | Evicted: 0 files", bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Sans", 8)
        )
        self.ram_evict_lbl.pack(side="right")

        # 4-Card KPI Strip
        metrics = tk.Frame(parent, bg=COLORS["bg"])
        metrics.pack(fill="x", pady=(0, 12))
        self.read_value = self._metric_card(metrics, "PROCESS DISK READ", "0 B/s", COLORS["cyan"])
        self.nvme_value = self._metric_card(metrics, "INTERNAL NVME PINNED", "0 files (0 B)", COLORS["blue"])
        self.ext_value = self._metric_card(metrics, "PRESHADER SSD PINNED", "0 files (0 B)", COLORS["amber"])
        self.total_value = self._metric_card(metrics, "TOTAL WARMED CACHE", "0 B", COLORS["green"], last=True)

        # Bottom Split: Chart + Preloaded Asset Timeline
        bottom_split = tk.Frame(parent, bg=COLORS["bg"])
        bottom_split.pack(fill="both", expand=True)
        bottom_split.columnconfigure(0, weight=5)
        bottom_split.columnconfigure(1, weight=6)
        bottom_split.rowconfigure(0, weight=1)

        chart_panel = tk.Frame(
            bottom_split, bg=COLORS["surface"], padx=12, pady=10,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        chart_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        c_hdr = tk.Frame(chart_panel, bg=COLORS["surface"])
        c_hdr.pack(fill="x", pady=(0, 6))
        tk.Label(
            c_hdr, text="LIVE I/O & VELOCITY SPARKLINE", bg=COLORS["surface"],
            fg=COLORS["text"], font=("Sans", 9, "bold")
        ).pack(side="left")
        tk.Label(
            c_hdr, text="Cyan: Read · Purple: Velocity", bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Sans", 8)
        ).pack(side="right")
        self.chart = tk.Canvas(chart_panel, bg=COLORS["surface"], highlightthickness=0)
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda _e: self._draw_chart())

        timeline_panel = tk.Frame(
            bottom_split, bg=COLORS["surface"], padx=10, pady=10,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        timeline_panel.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        t_hdr = tk.Frame(timeline_panel, bg=COLORS["surface"])
        t_hdr.pack(fill="x", pady=(0, 6))
        tk.Label(
            t_hdr, text="RECENT PRE-PINNED ASSETS & PRECOMPILED CACHES", bg=COLORS["surface"],
            fg=COLORS["text"], font=("Sans", 9, "bold")
        ).pack(side="left")
        tk.Label(
            t_hdr, text="Double-click row to copy path", bg=COLORS["surface"],
            fg=COLORS["muted"], font=("Sans", 8)
        ).pack(side="right")

        self.timeline = ttk.Treeview(
            timeline_panel,
            columns=("time", "asset", "bus", "size"),
            show="headings",
            style="Recog.Treeview",
            height=6
        )
        for column, heading, width, anchor in (
            ("time", "TIME", 85, "w"),
            ("asset", "PRELOADED ASSET", 250, "w"),
            ("bus", "BUS", 110, "w"),
            ("size", "SIZE", 80, "e")
        ):
            self.timeline.heading(column, text=heading)
            self.timeline.column(column, width=width, anchor=anchor, stretch=column == "asset")
        self.timeline.pack(fill="both", expand=True)
        self.timeline.bind("<Double-1>", self._show_event_path)

    def _metric_card(self, parent, title, initial_value, accent, last=False):
        card = tk.Frame(
            parent, bg=COLORS["surface"], padx=14, pady=10,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        card.pack(side="left", fill="both", expand=True, padx=(0, 0 if last else 10))
        tk.Label(
            card, text=title, bg=COLORS["surface"], fg=COLORS["muted"], font=("Sans", 8, "bold")
        ).pack(anchor="w")
        value = tk.Label(
            card, text=initial_value, bg=COLORS["surface"], fg=accent, font=("Sans", 13, "bold")
        )
        value.pack(anchor="w", pady=(4, 0))
        return value

    def _build_matrix_view(self, parent):
        hdr = tk.Frame(parent, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 12))
        tk.Label(
            hdr, text="GRANULAR PARAMETER MATRIX", bg=COLORS["bg"],
            fg=COLORS["text"], font=("Sans", 16, "bold")
        ).pack(side="left")
        tk.Label(
            hdr,
            text="Select discrete level pills or drag sliders · Updates the single-process engine immediately",
            bg=COLORS["bg"], fg=COLORS["muted"], font=("Sans", 9)
        ).pack(side="right")

        outer_box = tk.Frame(parent, bg=COLORS["bg"], highlightthickness=0)
        outer_box.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer_box, bg=COLORS["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(outer_box, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=COLORS["bg"])

        scroll_frame.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfig(canvas_window, width=event.width)
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        current_section = None
        for pdef in PARAMETER_DEFINITIONS:
            if pdef["section"] != current_section:
                current_section = pdef["section"]
                tk.Label(
                    scroll_frame, text=f"▸  {current_section}", bg=COLORS["bg"],
                    fg=COLORS["cyan"], font=("Sans", 10, "bold")
                ).pack(anchor="w", pady=(14, 6))

            sec_key, param_key = pdef["key"]
            raw_val = self.config.get(sec_key, {}).get(param_key, pdef["levels"][1][1])

            card = tk.Frame(
                scroll_frame, bg=COLORS["surface"], padx=16, pady=12,
                highlightthickness=1, highlightbackground=COLORS["border"]
            )
            card.pack(fill="x", pady=4)

            top_line = tk.Frame(card, bg=COLORS["surface"])
            top_line.pack(fill="x")
            tk.Label(
                top_line, text=pdef["title"], bg=COLORS["surface"],
                fg=COLORS["text"], font=("Sans", 10, "bold")
            ).pack(side="left")

            val_badge = tk.Label(
                top_line,
                text=self._format_param_value(pdef["type"], raw_val),
                bg=COLORS["surface_alt"], fg=COLORS["cyan"],
                font=("Sans", 9, "bold"), padx=10, pady=2
            )
            val_badge.pack(side="right")

            tk.Label(
                card, text=pdef["desc"], bg=COLORS["surface"],
                fg=COLORS["muted"], font=("Sans", 8)
            ).pack(anchor="w", pady=(2, 8))

            controls_line = tk.Frame(card, bg=COLORS["surface"])
            controls_line.pack(fill="x")

            lvl_buttons = []
            for lvl_label, lvl_val in pdef["levels"]:
                b = tk.Button(
                    controls_line,
                    text=lvl_label,
                    command=lambda pd=pdef, v=lvl_val: self._set_parameter(pd, v),
                    bg=COLORS["surface_alt"],
                    fg=COLORS["text"],
                    activebackground=COLORS["cyan"],
                    activeforeground="#05101a",
                    relief="flat",
                    borderwidth=0,
                    padx=10,
                    pady=4,
                    cursor="hand2",
                    font=("Sans", 8, "bold")
                )
                b.pack(side="left", padx=(0, 6))
                lvl_buttons.append((lvl_val, b))

            slider_min = pdef["min"]
            slider_max = pdef["max"]
            resolution = 0.05 if pdef["type"] == "float" else 1
            disp_val = raw_val / (1024 * 1024) if pdef["type"] == "bytes_mb" else raw_val

            slider = tk.Scale(
                controls_line,
                from_=slider_min,
                to=slider_max,
                orient="horizontal",
                resolution=resolution,
                showvalue=False,
                bg=COLORS["surface"],
                fg=COLORS["text"],
                troughcolor=COLORS["surface_alt"],
                highlightthickness=0,
                activebackground=COLORS["cyan"],
                length=220,
                command=lambda v, pd=pdef: self._on_slider_change(pd, v)
            )
            slider.set(disp_val)
            slider.pack(side="right")

            self.param_widgets[pdef["key"]] = {
                "pdef": pdef,
                "badge": val_badge,
                "slider": slider,
                "buttons": lvl_buttons
            }
            self._highlight_level_buttons(pdef["key"], raw_val)

    def _format_param_value(self, ptype, val):
        if ptype == "bytes_mb":
            return f"{int(val) // (1024 * 1024)} MB"
        if ptype == "float":
            return f"{float(val):.2f}"
        return f"{int(val)}"

    def _highlight_level_buttons(self, key_tuple, current_val):
        w = self.param_widgets.get(key_tuple)
        if not w:
            return
        for lvl_val, btn in w["buttons"]:
            if abs(float(lvl_val) - float(current_val)) < 1e-4:
                btn.configure(bg=COLORS["cyan"], fg="#05101a")
            else:
                btn.configure(bg=COLORS["surface_alt"], fg=COLORS["text"])

    def _set_parameter(self, pdef, raw_value):
        sec_key, param_key = pdef["key"]
        self.config.setdefault(sec_key, {})[param_key] = raw_value
        w = self.param_widgets.get(pdef["key"])
        if w:
            w["badge"].configure(text=self._format_param_value(pdef["type"], raw_value))
            disp_val = raw_value / (1024 * 1024) if pdef["type"] == "bytes_mb" else raw_value
            w["slider"].set(disp_val)
            self._highlight_level_buttons(pdef["key"], raw_value)
        self._sync_toggles_from_config()
        self.save_config(f"Set {sec_key}.{param_key} = {raw_value}")

    def _on_slider_change(self, pdef, slider_str):
        sec_key, param_key = pdef["key"]
        val = float(slider_str)
        if pdef["type"] == "bytes_mb":
            raw_val = int(round(val)) * 1024 * 1024
        elif pdef["type"] == "int":
            raw_val = int(round(val))
        else:
            raw_val = round(val, 2)

        prev_val = self.config.get(sec_key, {}).get(param_key)
        if prev_val == raw_val:
            return
        self.config.setdefault(sec_key, {})[param_key] = raw_val
        w = self.param_widgets.get(pdef["key"])
        if w:
            w["badge"].configure(text=self._format_param_value(pdef["type"], raw_val))
            self._highlight_level_buttons(pdef["key"], raw_val)
        self._sync_toggles_from_config()
        self.save_config(f"Tuned {sec_key}.{param_key} = {raw_val}")

    def _build_vault_view(self, parent):
        hdr = tk.Frame(parent, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 12))
        tk.Label(
            hdr, text="PRECOMPILED CACHES (/run/media/kessia/preshader + NVMe)", bg=COLORS["bg"],
            fg=COLORS["text"], font=("Sans", 15, "bold")
        ).pack(side="left")

        btn_bar = tk.Frame(hdr, bg=COLORS["bg"])
        btn_bar.pack(side="right")
        tk.Button(
            btn_bar, text="⚡ Pre-Warm All Caches to RAM", command=self.prewarm_all_caches,
            bg=COLORS["cyan"], fg="#05101a", relief="flat", padx=12, pady=5,
            font=("Sans", 8, "bold"), cursor="hand2"
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            btn_bar, text="🎯 Pin Selected Game Cache", command=self._pin_selected_vault_row,
            bg=COLORS["surface_alt"], fg=COLORS["green"], relief="flat", padx=12, pady=5,
            font=("Sans", 8, "bold"), cursor="hand2"
        ).pack(side="left", padx=(0, 6))
        tk.Button(
            btn_bar, text="⇄ Sync & Rescan Vaults", command=self._sync_and_rescan_vaults,
            bg=COLORS["surface_alt"], fg=COLORS["cyan"], relief="flat", padx=12, pady=5,
            font=("Sans", 8, "bold"), cursor="hand2"
        ).pack(side="left")

        vault_panel = tk.Frame(
            parent, bg=COLORS["surface"], padx=12, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        vault_panel.pack(fill="both", expand=True, pady=(0, 14))
        tk.Label(
            vault_panel,
            text="UNIFIED PRECOMPILED SHADER & HOT-STRIPE CACHES (DOUBLE-CLICK ANY GAME TO PIN ITS CACHE INTO RAM)",
            bg=COLORS["surface"], fg=COLORS["cyan"], font=("Sans", 8, "bold")
        ).pack(anchor="w", pady=(0, 8))

        self.vault_tree = ttk.Treeview(
            vault_panel,
            columns=("game", "appid", "cache_files", "cache_size", "nodes", "edges", "top_trigger"),
            show="headings",
            style="Recog.Treeview",
            height=8
        )
        for col, title, width, anchor in (
            ("game", "GAME VAULT", 220, "w"),
            ("appid", "APPID", 75, "w"),
            ("cache_files", "PRECOMPILED FILES", 130, "e"),
            ("cache_size", "CACHE SIZE", 100, "e"),
            ("nodes", "TRIGGERS", 85, "e"),
            ("edges", "EDGES", 85, "e"),
            ("top_trigger", "TOP LEARNED CHAIN / PRECOMPILED ASSET", 290, "w")
        ):
            self.vault_tree.heading(col, text=title)
            self.vault_tree.column(col, width=width, anchor=anchor, stretch=col in ("game", "top_trigger"))
        self.vault_tree.pack(fill="both", expand=True)
        self.vault_tree.bind("<Double-1>", lambda _e: self._pin_selected_vault_row())

        ext_panel = tk.Frame(
            parent, bg=COLORS["surface"], padx=16, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        ext_panel.pack(fill="x")
        tk.Label(
            ext_panel, text="MONITORED GAME ENGINE ASSET EXTENSIONS (CLICK TO TOGGLE LIVE)",
            bg=COLORS["surface"], fg=COLORS["amber"], font=("Sans", 9, "bold")
        ).pack(anchor="w", pady=(0, 8))

        active_exts = {e.lower().strip() for e in self.config.get("target_extensions", [])}
        for group_name, ext_list in EXTENSION_GROUPS.items():
            row = tk.Frame(ext_panel, bg=COLORS["surface"])
            row.pack(fill="x", pady=2)
            tk.Label(
                row, text=f"{group_name}:", width=22, anchor="w",
                bg=COLORS["surface"], fg=COLORS["text"], font=("Sans", 9, "bold")
            ).pack(side="left")
            for ext in ext_list:
                is_on = ext.lower() in active_exts
                btn = tk.Button(
                    row,
                    text=ext,
                    command=lambda e=ext: self._toggle_extension(e),
                    bg=COLORS["cyan"] if is_on else COLORS["surface_alt"],
                    fg="#05101a" if is_on else COLORS["muted"],
                    relief="flat",
                    borderwidth=0,
                    padx=7,
                    pady=2,
                    cursor="hand2",
                    font=("Sans", 8, "bold")
                )
                btn.pack(side="left", padx=2)
                self.ext_vars[ext] = btn

    def _toggle_extension(self, ext):
        current = [e.strip() for e in self.config.get("target_extensions", [])]
        low_map = {e.lower(): e for e in current}
        if ext.lower() in low_map:
            current.remove(low_map[ext.lower()])
            on = False
        else:
            current.append(ext)
            on = True
        self.config["target_extensions"] = current
        btn = self.ext_vars.get(ext)
        if btn:
            btn.configure(
                bg=COLORS["cyan"] if on else COLORS["surface_alt"],
                fg="#05101a" if on else COLORS["muted"]
            )
        self.save_config(f"{'Enabled' if on else 'Disabled'} asset extension {ext}")

    def _sync_and_rescan_vaults(self):
        merged = self.merger.sync_and_merge_vaults()
        self._refresh_vault_inspector()
        self._append_log(f"[VAULT-SYNC] Merged & synced {merged} game brains across NVMe and {self.merger.ext_mount}.")

    def _pin_selected_vault_row(self):
        if not hasattr(self, "vault_tree"):
            return
        sel = self.vault_tree.selection()
        if not sel:
            self.prewarm_all_caches()
            return
        safe_id = sel[0]
        self.daemon.prewarm_caches(safe_id=safe_id)
        self._update_metrics()

    def _refresh_vault_inspector(self):
        if not hasattr(self, "vault_tree"):
            return
        for item in self.vault_tree.get_children():
            self.vault_tree.delete(item)

        self.cached_vault_entries = self.merger.discover_precompiled_caches()
        seen_appids = set()

        for entry in self.cached_vault_entries:
            seen_appids.add(str(entry["appid"]))
            iid = entry["safe_id"]
            self.vault_tree.insert(
                "", "end", iid=iid,
                values=(
                    entry["game_name"],
                    entry["appid"],
                    f"{len(entry['cache_files'])} files",
                    format_bytes(entry["cache_bytes"]),
                    f"{entry['nodes']:,}",
                    f"{entry['edges']:,}",
                    entry["top_chain"]
                )
            )

        self.steam_detector.refresh_libraries()
        for appid, info in sorted(self.steam_detector.manifests.items(), key=lambda x: x[1]["name"]):
            if str(appid) not in seen_appids:
                self.vault_tree.insert(
                    "", "end", iid=info["safe_id"],
                    values=(
                        info["name"],
                        appid,
                        "0 files",
                        "0 B",
                        "0 (Ready)",
                        "0",
                        f"Steam Library: {os.path.basename(info['game_dir'])}"
                    )
                )

    def _build_health_view(self, parent):
        hdr = tk.Frame(parent, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 12))
        tk.Label(
            hdr, text="DISK HEALTH CHECK & CORRUPTED BLOCK REPAIR", bg=COLORS["bg"],
            fg=COLORS["text"], font=("Sans", 15, "bold")
        ).pack(side="left")

        btn_bar = tk.Frame(hdr, bg=COLORS["bg"])
        btn_bar.pack(side="right")
        self.disk_check_btn = tk.Button(
            btn_bar, text="🔍 Run Disk Health Check",
            command=lambda: self._run_disk_health_async(repair=False),
            bg=COLORS["cyan"], fg="#05101a", relief="flat", padx=12, pady=5,
            font=("Sans", 8, "bold"), cursor="hand2"
        )
        self.disk_check_btn.pack(side="left", padx=(0, 6))
        self.disk_repair_btn = tk.Button(
            btn_bar, text="🛠 Repair Corrupted Blocks & Caches",
            command=lambda: self._run_disk_health_async(repair=True),
            bg=COLORS["surface_alt"], fg=COLORS["amber"], relief="flat", padx=12, pady=5,
            font=("Sans", 8, "bold"), cursor="hand2"
        )
        self.disk_repair_btn.pack(side="left")

        cards_row = tk.Frame(parent, bg=COLORS["bg"])
        cards_row.pack(fill="x", pady=(0, 12))
        for i in range(4):
            cards_row.columnconfigure(i, weight=1)

        def _make_card(col, title, default_val, accent):
            card = tk.Frame(
                cards_row, bg=COLORS["surface"], padx=14, pady=12,
                highlightthickness=1, highlightbackground=COLORS["border"]
            )
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
            tk.Label(
                card, text=title, bg=COLORS["surface"], fg=COLORS["muted"], font=("Sans", 8, "bold")
            ).pack(anchor="w")
            val_lbl = tk.Label(
                card, text=default_val, bg=COLORS["surface"], fg=accent, font=("Sans", 13, "bold")
            )
            val_lbl.pack(anchor="w", pady=(4, 2))
            sub_lbl = tk.Label(
                card, text="Not scanned yet", bg=COLORS["surface"], fg=COLORS["dim"], font=("Sans", 8)
            )
            sub_lbl.pack(anchor="w")
            return val_lbl, sub_lbl

        self.health_overall_val, self.health_overall_sub = _make_card(0, "OVERALL HEALTH", "STANDBY", COLORS["cyan"])
        self.health_smart_val, self.health_smart_sub = _make_card(1, "SMART TELEMETRY", "PENDING", COLORS["green"])
        self.health_blocks_val, self.health_blocks_sub = _make_card(2, "BAD SECTORS", "0", COLORS["purple"])
        self.health_vault_val, self.health_vault_sub = _make_card(3, "VAULT INTEGRITY", "READY", COLORS["amber"])

        report_frame = tk.Frame(
            parent, bg=COLORS["surface"], padx=12, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        report_frame.pack(fill="both", expand=True)
        tk.Label(
            report_frame,
            text="DIAGNOSTIC OUTPUT (SMART · BADBLOCKS · VAULT INTEGRITY · SAFE FSCK REPAIR)",
            bg=COLORS["surface"], fg=COLORS["cyan"], font=("Sans", 8, "bold")
        ).pack(anchor="w", pady=(0, 8))

        self.health_text = tk.Text(
            report_frame,
            bg="#060a12",
            fg=COLORS["text"],
            insertbackground=COLORS["cyan"],
            relief="flat",
            borderwidth=0,
            font=("Monospace", 9),
            wrap="word",
            state="disabled"
        )
        self.health_text.pack(fill="both", expand=True)
        self._disk_health_running = False

        if isinstance(self.daemon.last_disk_health, dict):
            self._render_disk_health_report(self.daemon.last_disk_health)
        else:
            self._set_health_report_text(
                f"Target Vault Mount: {self.daemon.ext_mount}\n"
                "Click 'Run Disk Health Check' to inspect SMART health, bad blocks, mount state, "
                "and precompiled cache integrity.\n"
                "Click 'Repair Corrupted Blocks & Caches' to quarantine corrupted vault files, "
                "re-sync healthy twin copies, and check/repair unmounted filesystems.\n\n"
                "CLI Equivalents:\n"
                "  preshader --disk-check\n"
                "  preshader --disk-repair\n"
            )

    def _set_health_report_text(self, text):
        if self.headless or not hasattr(self, "health_text"):
            return
        self.health_text.configure(state="normal")
        self.health_text.delete("1.0", "end")
        self.health_text.insert("end", text)
        self.health_text.configure(state="disabled")

    def _run_disk_health_async(self, repair=False):
        if getattr(self, "_disk_health_running", False):
            return
        self._disk_health_running = True
        mode_text = "REPAIRING CORRUPTED BLOCKS & CACHES..." if repair else "SCANNING DISK HEALTH..."
        if not self.headless and hasattr(self, "health_overall_val"):
            self.health_overall_val.configure(text="SCANNING...", fg=COLORS["amber"])
            self.health_overall_sub.configure(text=mode_text)
            self.disk_check_btn.configure(state="disabled")
            self.disk_repair_btn.configure(state="disabled")
            self._set_health_report_text(f"[DISK-HEALTH] {mode_text}\nPlease wait while SMART, badblocks, and vault files are verified...\n")

        def _worker():
            try:
                report = self.daemon.run_disk_health_check(repair=repair)
            except Exception as err:
                report = {
                    "status": "ERROR",
                    "summary_text": f"[DISK-HEALTH] Scan failed: {err}",
                    "mount": {},
                    "smart": {"status": "ERROR"},
                    "badblocks": {"status": "ERROR", "count": 0},
                    "vault_integrity": {"files_checked": 0, "corrupted_files": [], "repaired_files": []},
                }
            self.schedule_ui(lambda r=report: self._finish_disk_health_ui(r))

        threading.Thread(target=_worker, name="RecogDDiskHealthWorker", daemon=True).start()

    def _finish_disk_health_ui(self, report):
        self._disk_health_running = False
        if not self.headless and hasattr(self, "disk_check_btn"):
            self.disk_check_btn.configure(state="normal")
            self.disk_repair_btn.configure(state="normal")
        self._render_disk_health_report(report)

    def _render_disk_health_report(self, report):
        if self.headless or not hasattr(self, "health_overall_val") or not isinstance(report, dict):
            return
        status = report.get("status", "UNKNOWN")
        status_color = {
            "HEALTHY": COLORS["green"],
            "REPAIRED": COLORS["cyan"],
            "WARNING": COLORS["amber"],
            "DEGRADED": COLORS["red"],
            "ERROR": COLORS["red"],
        }.get(status, COLORS["muted"])

        mount = report.get("mount", {})
        smart = report.get("smart", {})
        badblocks = report.get("badblocks", {})
        vault = report.get("vault_integrity", {})

        self.health_overall_val.configure(text=status, fg=status_color)
        self.health_overall_sub.configure(
            text=f"{mount.get('device') or 'Local Vault'} · {report.get('duration_seconds', 0):.2f}s"
        )

        smart_st = smart.get("status", "SKIPPED")
        smart_color = COLORS["green"] if smart_st == "PASSED" else (COLORS["red"] if smart_st == "FAILED" else COLORS["amber"])
        self.health_smart_val.configure(text=smart_st, fg=smart_color)
        temp_str = f"{smart['temperature_c']}°C" if smart.get("temperature_c") is not None else (mount.get("fstype") or "N/A")
        self.health_smart_sub.configure(text=f"Drive: {mount.get('parent_device') or 'N/A'} ({temp_str})")

        bad_cnt = int(badblocks.get("count", 0))
        self.health_blocks_val.configure(
            text=str(bad_cnt), fg=COLORS["red"] if bad_cnt > 0 else COLORS["green"]
        )
        self.health_blocks_sub.configure(text=f"Scan: {badblocks.get('status', 'SKIPPED')}")

        corrupt_cnt = len(vault.get("corrupted_files", []))
        repaired_cnt = len(vault.get("repaired_files", []))
        checked_cnt = int(vault.get("files_checked", 0))
        self.health_vault_val.configure(
            text=f"{checked_cnt} FILES",
            fg=COLORS["red"] if (corrupt_cnt > 0 and repaired_cnt == 0) else (COLORS["cyan"] if repaired_cnt > 0 else COLORS["green"])
        )
        self.health_vault_sub.configure(text=f"Corrupted: {corrupt_cnt} | Repaired: {repaired_cnt}")
        self._set_health_report_text(report.get("summary_text", ""))

    def _build_logs_view(self, parent):
        hdr = tk.Frame(parent, bg=COLORS["bg"])
        hdr.pack(fill="x", pady=(0, 12))
        tk.Label(
            hdr, text="SINGLE-PROCESS KERNEL INOTIFY, VELOCITY & RAM GOVERNOR STREAM",
            bg=COLORS["bg"], fg=COLORS["text"], font=("Sans", 15, "bold")
        ).pack(side="left")
        tk.Button(
            hdr, text="Clear Console", command=self._clear_logs,
            bg=COLORS["surface_alt"], fg=COLORS["muted"], relief="flat",
            padx=12, pady=5, font=("Sans", 9, "bold"), cursor="hand2"
        ).pack(side="right")

        log_panel = tk.Frame(
            parent, bg=COLORS["surface"], padx=12, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        log_panel.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_panel,
            bg="#060a12",
            fg=COLORS["cyan"],
            insertbackground=COLORS["cyan"],
            relief="flat",
            font=("Monospace", 9),
            state="disabled",
            wrap="word"
        )
        self.log_text.pack(fill="both", expand=True)
        for existing in list(self.output_lines):
            self.log_text.configure(state="normal")
            self.log_text.insert("end", existing + "\n")
            self.log_text.configure(state="disabled")

    def _append_log(self, line):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {line}"
        with self.output_lock:
            self.output_lines.append(entry)
        if not self.headless and hasattr(self, "log_text"):
            try:
                self.log_text.configure(state="normal")
                self.log_text.insert("end", entry + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except tk.TclError:
                pass

    def _clear_logs(self):
        with self.output_lock:
            self.output_lines.clear()
        if hasattr(self, "log_text"):
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")

    # --- Functional Action Handlers ---

    def _selected_safe_id(self):
        if not hasattr(self, "target_var"):
            return None
        lbl = self.target_var.get()
        for t in self.targets.values():
            if t["label"] == lbl:
                return t.get("safe_id")
        return None

    def _on_target_scope_selected(self, _event=None):
        safe_id = self._selected_safe_id()
        self.daemon.prewarm_caches(safe_id=safe_id)
        self._update_metrics()

    def prewarm_all_caches(self):
        self.daemon.prewarm_caches(safe_id=None)
        self._refresh_vault_inspector()
        self._update_metrics()

    def toggle_pause(self):
        new_paused = not self.daemon.paused
        self.daemon.set_paused(new_paused)
        self._update_metrics()

    def toggle_combat_shield(self):
        new_shield = not self.daemon.combat_shield
        self.daemon.set_combat_shield(new_shield)
        if "combat_shield_switch" in self.toggle_widgets:
            self.toggle_widgets["combat_shield_switch"].set(new_shield)
        self._update_metrics()

    def _on_toggle_combat_shield_switch(self, state):
        self.daemon.set_combat_shield(bool(state))
        self._update_metrics()

    def apply_preset(self, preset_key):
        if preset_key not in PERFORMANCE_PRESETS:
            return
        preset = PERFORMANCE_PRESETS[preset_key]
        self.active_preset = preset_key
        for section in ("engine", "velocity", "ram_governor"):
            self.config.setdefault(section, {}).update(preset[section])

        if not self.headless:
            self.preset_desc_label.configure(text=preset["subtitle"], fg=preset["color"])
            for k, btn in self.preset_buttons.items():
                p_color = PERFORMANCE_PRESETS[k]["color"]
                btn.configure(
                    bg=p_color if k == preset_key else COLORS["surface_alt"],
                    fg="#05101a" if k == preset_key else COLORS["text"]
                )
            for key_tuple, w in self.param_widgets.items():
                sec, p_name = key_tuple
                val = self.config.get(sec, {}).get(p_name)
                if val is not None:
                    pdef = w["pdef"]
                    w["badge"].configure(text=self._format_param_value(pdef["type"], val))
                    disp_val = val / (1024 * 1024) if pdef["type"] == "bytes_mb" else val
                    w["slider"].set(disp_val)
                    self._highlight_level_buttons(key_tuple, val)
            self._sync_toggles_from_config()

        self.save_config(f"Applied preset {preset['label']}")

    def _sync_toggles_from_config(self):
        if self.headless or not self.toggle_widgets:
            return
        self.toggle_widgets["velocity_enabled"].set(
            bool(self.config.get("velocity", {}).get("enabled", True))
        )
        self.toggle_widgets["corner_lookahead"].set(
            int(self.config.get("velocity", {}).get("max_lookahead_hops", 2)) >= 2
        )
        self.toggle_widgets["ram_governor_enabled"].set(
            bool(self.config.get("ram_governor", {}).get("enabled", True))
        )
        self.toggle_widgets["cross_bus_striping"].set(
            bool(self.config.get("external_drive", {}).get("enable_cross_bus_striping", True))
        )
        self.toggle_widgets["instant_confidence"].set(
            int(self.config.get("engine", {}).get("min_confidence_hits", 2)) == 1
        )
        self.toggle_widgets["combat_shield_switch"].set(self.daemon.combat_shield)

    def _on_toggle_velocity(self, state):
        self.config.setdefault("velocity", {})["enabled"] = bool(state)
        self.save_config(f"Adaptive Velocity Pre-Fetching {'ENABLED' if state else 'DISABLED'}")

    def _on_toggle_corner_lookahead(self, state):
        self.config.setdefault("velocity", {})["max_lookahead_hops"] = 2 if state else 1
        self.save_config(f"2-Hop Corner Lookahead {'ENABLED (2 hops)' if state else 'DISABLED (1 hop)'}")

    def _on_toggle_ram_governor(self, state):
        self.config.setdefault("ram_governor", {})["enabled"] = bool(state)
        self.save_config(f"Automated RAM Budget Governor {'ENABLED' if state else 'DISABLED'}")

    def _on_toggle_striping(self, state):
        self.config.setdefault("external_drive", {})["enable_cross_bus_striping"] = bool(state)
        self.save_config(f"Cross-Bus SSD Striping {'ENABLED' if state else 'DISABLED'}")

    def _on_toggle_instant_confidence(self, state):
        hits = 1 if state else 2
        self.config.setdefault("engine", {})["min_confidence_hits"] = hits
        if ("engine", "min_confidence_hits") in self.param_widgets:
            w = self.param_widgets[("engine", "min_confidence_hits")]
            w["badge"].configure(text=str(hits))
            w["slider"].set(hits)
            self._highlight_level_buttons(("engine", "min_confidence_hits"), hits)
        self.save_config(f"Minimum Confidence Hits set to {hits}")

    def purge_ram_cache(self):
        self.daemon.purge_ram_cache()
        self._update_metrics()

    def simulate_velocity_burst(self):
        safe_id = self._selected_safe_id()
        self.daemon.simulate_corner_burst(safe_id=safe_id)
        if not self.headless:
            self._update_metrics()
            self._draw_chart()

    def show_window(self):
        if self.headless:
            return
        self.tray.hide_floating_dock()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self):
        if self.headless:
            return
        self.root.withdraw()
        if self.tray.backend_name == "floating-mini-dock":
            self.tray.show_floating_dock()

    def _refresh(self):
        self._refresh_io(os.getpid())
        self._update_metrics()
        self._draw_chart()
        self.root.after(600, self._refresh)

    def _refresh_io(self, pid):
        now = time.monotonic()
        try:
            current = read_process_io(pid)
        except (OSError, KeyError, ValueError):
            self.read_rate = 0.0
            self.write_rate = 0.0
            self.last_io = None
            self.last_io_time = None
            return
        if self.last_io is not None and self.last_io_time is not None:
            elapsed = now - self.last_io_time
            if elapsed > 0:
                self.read_rate = max(0.0, current[0] - self.last_io[0]) / elapsed
                self.write_rate = max(0.0, current[1] - self.last_io[1]) / elapsed
        self.last_io = current
        self.last_io_time = now

    def _update_metrics(self):
        if self.headless:
            return
        stats = self.daemon.get_combined_stats()
        history = self.daemon.telemetry.data.get("preloaded", [])

        # Update Sidebar Engine Status & Pause/Shield Buttons
        if self.daemon.paused:
            self.status_label.configure(text="ENGINE PAUSED", fg=COLORS["amber"])
            self.status_dot.itemconfigure(self.status_circle, fill=COLORS["amber"])
            self.pause_button.configure(
                text="▶  RESUME PINNING ENGINE",
                bg=COLORS["amber"],
                fg="#05101a"
            )
        elif self.daemon.combat_shield:
            self.status_label.configure(text="COMBAT SHIELD LOCKED", fg=COLORS["red"])
            self.status_dot.itemconfigure(self.status_circle, fill=COLORS["red"])
            self.pause_button.configure(
                text="⏸  PAUSE PINNING ENGINE",
                bg=COLORS["cyan"],
                fg="#05101a"
            )
        else:
            self.status_label.configure(text="UNIFIED ENGINE ONLINE", fg=COLORS["green"])
            self.status_dot.itemconfigure(self.status_circle, fill=COLORS["green"])
            self.pause_button.configure(
                text="⏸  PAUSE PINNING ENGINE",
                bg=COLORS["cyan"],
                fg="#05101a"
            )

        if self.daemon.combat_shield:
            self.shield_button.configure(
                text="🛡  COMBAT SPIKE SHIELD: LOCKED ON",
                bg=COLORS["red"],
                fg="#05101a"
            )
        else:
            self.shield_button.configure(
                text="🛡  COMBAT SPIKE SHIELD: OFF",
                bg=COLORS["surface_alt"],
                fg=COLORS["text"]
            )

        active_game = stats.get("active_game_name", "")
        if active_game:
            self.active_game_label.configure(
                text=f"Active Game: {active_game} ({stats.get('active_game_appid', '')})",
                fg=COLORS["cyan"]
            )
        else:
            self.active_game_label.configure(
                text=f"Dual-Bus Warm · {self.merger.ext_mount}",
                fg=COLORS["muted"]
            )

        local_gov = self.local_governor.evaluate()
        local_vel_prof = self.local_velocity.get_adaptive_profile(
            self.config.get("engine", {}).get("max_preload_children", 35),
            self.config.get("engine", {}).get("time_window_seconds", 0.75),
            self.config.get("engine", {}).get("min_confidence_hits", 2)
        )

        vel_score = float(local_vel_prof["velocity"])
        vel_peak = float(stats.get("peak_velocity_score", 0.0))
        vel_state = "COMBAT_SHIELD" if self.daemon.combat_shield else local_vel_prof["state"]
        corner_pins = int(stats.get("lookahead_pins_total", 0)) + int(stats.get("corner_lookahead_boosts", 0))
        hops = 1 if self.daemon.combat_shield else self.config.get("velocity", {}).get("max_lookahead_hops", 2)
        mult = local_vel_prof["multiplier"]

        self.io_samples.append((self.read_rate, self.write_rate))
        self.vel_samples.append(vel_score)

        vel_color = COLORS["red"] if self.daemon.combat_shield else (
            COLORS["purple"] if vel_score >= 0.75 else (COLORS["amber"] if vel_score >= 0.5 else COLORS["cyan"])
        )
        self.vel_state_badge.configure(text=f"{vel_state} · {mult:.2f}x", fg=vel_color)
        self.vel_score_lbl.configure(text=f"Velocity: {vel_score:.2f} (Peak {vel_peak:.2f})")
        self.vel_lookahead_lbl.configure(text=f"Corner Lookahead Boosts: {corner_pins} | Hops: {hops}")
        self._draw_progress_bar(self.vel_bar, vel_score, vel_color)

        ram_state = stats.get("ram_governor_state", local_gov["state"])
        ram_scale = float(stats.get("ram_scale_factor", local_gov["scale_factor"]))
        avail_mb = local_gov["mem_available_bytes"] // (1024 * 1024)
        budget_mb = int(stats.get("ram_dynamic_budget_mb", local_gov["dynamic_budget_bytes"] // (1024 * 1024)))
        active_bytes = int(stats.get("active_pinned_bytes", 0))
        evicted_files = int(stats.get("evicted_files", 0))
        avail_ratio = local_gov["avail_ratio"]

        ram_color = COLORS["green"] if ram_scale >= 0.8 else (COLORS["amber"] if ram_scale >= 0.4 else COLORS["red"])
        self.ram_state_badge.configure(text=f"{ram_state} · {int(ram_scale * 100)}%", fg=ram_color)
        self.ram_avail_lbl.configure(
            text=f"Avail RAM: {avail_mb:,} MB ({avail_ratio * 100:.0f}%) | Budget: {budget_mb:,} MB"
        )
        self.ram_evict_lbl.configure(
            text=f"Active Cache: {format_bytes(active_bytes)} | Evicted: {evicted_files} files"
        )
        self._draw_progress_bar(self.ram_bar, avail_ratio, ram_color)

        nvme_files = stats.get("nvme_pinned_files", 0)
        nvme_bytes = stats.get("nvme_pinned_bytes", 0)
        ext_files = stats.get("ext_pinned_files", 0)
        ext_bytes = stats.get("ext_pinned_bytes", 0)
        total_bytes = stats.get("total_pinned_bytes", 0)

        self.read_value.configure(text=f"{format_bytes(self.read_rate)}/s")
        self.nvme_value.configure(text=f"{nvme_files:,} files ({format_bytes(nvme_bytes)})")
        self.ext_value.configure(text=f"{ext_files:,} files ({format_bytes(ext_bytes)})")
        self.total_value.configure(text=format_bytes(total_bytes))

        current_items = self.timeline.get_children()
        if current_items:
            self.timeline.delete(*current_items)
        for index, event in enumerate(reversed(history[-12:])):
            history_index = len(history) - index - 1
            item_id = f"preload-{history_index}"
            try:
                event_time = datetime.fromisoformat(event.get("timestamp", ""))
                display_time = event_time.strftime("%H:%M:%S")
            except ValueError:
                display_time = "Recent"
            self.timeline.insert(
                "", "end", iid=item_id,
                values=(
                    display_time,
                    event.get("name", os.path.basename(event.get("path", "Asset"))),
                    event.get("bus", "NVMe"),
                    format_bytes(event.get("size_bytes", 0))
                )
            )

    def _draw_progress_bar(self, canvas, ratio, color):
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 10:
            return
        canvas.delete("all")
        fill_w = max(4, int(w * max(0.0, min(1.0, ratio))))
        canvas.create_rectangle(0, 0, fill_w, h, fill=color, outline="")
        for tick in (0.25, 0.5, 0.75):
            tx = int(w * tick)
            canvas.create_line(tx, 0, tx, h, fill=COLORS["bg"], width=1)

    def _draw_chart(self):
        width = self.chart.winfo_width()
        height = self.chart.winfo_height()
        if width < 50 or height < 40:
            return
        self.chart.delete("all")
        for fraction in (0.25, 0.5, 0.75):
            y = int(height * fraction)
            self.chart.create_line(0, y, width, y, fill=COLORS["border"], dash=(2, 4))

        samples = list(self.io_samples)
        vel_hist = list(self.vel_samples)
        if len(samples) < 2:
            self.chart.create_text(
                width / 2, height / 2,
                text="Sampling live disk & velocity telemetry…",
                fill=COLORS["muted"], font=("Sans", 9)
            )
            return

        peak = max(max(r, w) for r, w in samples)
        scale = max(peak, 1024.0)

        read_pts = []
        for idx, (r_val, _) in enumerate(samples):
            x = (idx / (len(samples) - 1)) * width
            y = height - 10 - (r_val / scale) * (height - 22)
            read_pts.extend((x, y))
        if len(read_pts) >= 4:
            self.chart.create_line(*read_pts, fill=COLORS["cyan"], width=2, smooth=True)

        if len(vel_hist) >= 2:
            vel_pts = []
            for idx, v_val in enumerate(vel_hist):
                x = (idx / (len(vel_hist) - 1)) * width
                y = height - 10 - v_val * (height - 22)
                vel_pts.extend((x, y))
            if len(vel_pts) >= 4:
                self.chart.create_line(*vel_pts, fill=COLORS["purple"], width=2, smooth=True)

        self.chart.create_text(
            8, 6, anchor="nw",
            text=f"Peak I/O: {format_bytes(peak)}/s",
            fill=COLORS["muted"], font=("Sans", 8)
        )

    def _show_event_path(self, _event):
        selection = self.timeline.selection()
        if not selection:
            return
        try:
            index = int(selection[0].removeprefix("preload-"))
            history = self.daemon.telemetry.data.get("preloaded", [])
            path = history[index].get("path", "")
        except (ValueError, IndexError):
            return
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self._append_log(f"[CLIPBOARD] Copied asset path: {path}")

    def quit(self):
        self.daemon.stop()
        if self.ipc_server_sock is not None:
            try:
                self.ipc_server_sock.close()
            except OSError:
                pass
        try:
            if os.path.exists(SINGLETON_SOCK):
                os.unlink(SINGLETON_SOCK)
        except OSError:
            pass
        if hasattr(self, "tray"):
            self.tray.stop()
        if not self.headless and hasattr(self, "root"):
            self.root.destroy()

    def run(self):
        if not self.headless:
            self.root.mainloop()


def send_command_to_running_instance(command="SHOW", timeout=1.5):
    if not os.path.exists(SINGLETON_SOCK):
        return None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(timeout)
        client.connect(SINGLETON_SOCK)
        client.sendall((command + "\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = client.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
        client.close()
        return b"".join(chunks).decode("utf-8", errors="ignore")
    except OSError:
        return None


def attach_terminal_to_running_instance():
    if not os.path.exists(SINGLETON_SOCK):
        return False
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2.0)
        client.connect(SINGLETON_SOCK)
        client.sendall(b"ATTACH_TERMINAL\n")
        client.settimeout(None)
    except OSError:
        return False

    buf = ""
    try:
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                print(line, flush=True)
    except KeyboardInterrupt:
        print("\n[PRESHADER] Ctrl+C received. Shutting down cleanly...", flush=True)
        try:
            client.sendall(b"DETACH\n")
        except OSError:
            pass
        print("[PRESHADER] All game brains saved. Goodbye!", flush=True)
    finally:
        try:
            client.close()
        except OSError:
            pass
    return True


def main():
    parser = argparse.ArgumentParser(description="RecogD Preshader Unified Single-Process Control Deck")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config.json")
    parser.add_argument("--telemetry", default=DEFAULT_TELEMETRY, help="Path to runtime.json")
    parser.add_argument("--headless-check", action="store_true", help="Validate GUI state & presets headlessly")
    parser.add_argument("--minimized", action="store_true", help="Start minimized in permanent system tray")
    parser.add_argument("--show-gui", action="store_true", help="Bring Control Deck GUI window to front")
    parser.add_argument("--prewarm", action="store_true", help="Pre-warm all precompiled caches into RAM")
    parser.add_argument("--purge", action="store_true", help="Purge pinned RAM cache via DONTNEED")
    parser.add_argument("--disk-check", action="store_true", help="Run read-only disk SMART, bad-block, and vault integrity check")
    parser.add_argument("--disk-repair", action="store_true", help="Run disk health check and repair corrupted blocks/vault caches")
    parser.add_argument("--repair", action="store_true", help="Enable repair mode when combined with --disk-check")
    args = parser.parse_args()

    if not args.headless_check:
        if args.disk_check or args.disk_repair:
            repair_mode = bool(args.disk_repair or args.repair)
            ipc_cmd = "DISK_REPAIR" if repair_mode else "DISK_CHECK"
            resp = send_command_to_running_instance(ipc_cmd, timeout=60.0)
            if resp:
                print(resp.rstrip())
                return
            standalone_daemon = InProcessPreshaderDaemon(
                config_path=args.config,
                telemetry_path=args.telemetry
            )
            report = standalone_daemon.run_disk_health_check(repair=repair_mode)
            print(report["summary_text"])
            return
        elif args.prewarm:
            resp = send_command_to_running_instance("PREWARM")
            if resp:
                print(resp.strip())
                return
        elif args.purge:
            resp = send_command_to_running_instance("PURGE")
            if resp:
                print(resp.strip())
                return
        elif args.show_gui:
            resp = send_command_to_running_instance("SHOW")
            if resp:
                print("[PRESHADER] Connected to running single-process instance -> Control Deck opened.")
                return
        else:
            if attach_terminal_to_running_instance():
                return

    app = RecogDDesktop(
        config_path=args.config,
        telemetry_path=args.telemetry,
        headless=args.headless_check,
        start_minimized=args.minimized and not args.show_gui
    )
    if args.headless_check:
        print(f"[GUI-CHECK] Loaded {len(PARAMETER_DEFINITIONS)} parameters, {len(PERFORMANCE_PRESETS)} presets, {len(app.targets)} targets.")
        return
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n[PRESHADER] Ctrl+C received. Shutting down cleanly...", flush=True)
        app.daemon.save_brains()
        app.quit()
        print("[PRESHADER] All game brains saved. Goodbye!", flush=True)


if __name__ == "__main__":
    main()
