#!/usr/bin/env python3
import json
import os
import signal
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

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError as error:
    raise SystemExit(
        "RecogD's tray UI needs pystray and Pillow. Install them with "
        "`python3 -m pip install -r requirements.txt`."
    ) from error


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(ROOT_DIR, "config.json")
DEFAULT_TELEMETRY = "~/.cache/recogd/runtime.json"
COLORS = {
    "background": "#0b1220",
    "surface": "#111c2e",
    "surface_alt": "#17243a",
    "border": "#24334b",
    "text": "#e6eefb",
    "muted": "#91a4bf",
    "cyan": "#42d9d0",
    "blue": "#6d9cff",
    "green": "#54d68b",
    "red": "#ff7185",
    "amber": "#ffc66d"
}


def format_bytes(size):
    value = float(size)
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


def managed_daemon_pid(pid_file, telemetry_path):
    try:
        with open(pid_file, "r", encoding="utf-8") as pid_stream:
            pid = int(pid_stream.read().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as command_stream:
            command = command_stream.read().decode("utf-8", errors="replace").split("\0")
    except (OSError, ValueError):
        return None

    if not any(argument.endswith("/src/daemon.py") for argument in command):
        return None
    for index, argument in enumerate(command[:-1]):
        if argument == "--telemetry" and os.path.abspath(
            os.path.expanduser(command[index + 1])
        ) == telemetry_path:
            return pid
    return None


class RecogDDesktop:
    def __init__(self):
        self.root_dir = ROOT_DIR
        self.config_path = DEFAULT_CONFIG
        self.telemetry_path = os.path.abspath(os.path.expanduser(DEFAULT_TELEMETRY))
        self.pid_path = f"{self.telemetry_path}.pid"
        self.process = None
        self.active_pid = managed_daemon_pid(self.pid_path, self.telemetry_path)
        self.stop_requested = False
        self.last_io = None
        self.last_io_time = None
        self.read_rate = 0.0
        self.write_rate = 0.0
        self.io_samples = deque(maxlen=48)
        self.output_lines = deque(maxlen=30)
        self.output_lock = threading.Lock()
        self.telemetry = {}
        self.profiles = self._load_profiles()

        self.root = tk.Tk()
        self.root.title("RecogD · Asset Preloader")
        self.root.geometry("1020x790")
        self.root.minsize(860, 680)
        self.root.configure(bg=COLORS["background"])
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._configure_theme()
        self._build_window()
        self._start_tray()
        self.root.after(500, self._refresh)

    def _load_profiles(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as config_file:
                profiles = json.load(config_file).get("profiles", {})
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"Could not load {self.config_path}: {error}") from error
        if not profiles:
            raise SystemExit(f"No profiles are configured in {self.config_path}.")
        return profiles

    def _configure_theme(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Recog.Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            borderwidth=0,
            rowheight=34,
            font=("Sans", 10)
        )
        style.configure(
            "Recog.Treeview.Heading",
            background=COLORS["surface_alt"],
            foreground=COLORS["muted"],
            borderwidth=0,
            font=("Sans", 9, "bold")
        )
        style.map(
            "Recog.Treeview",
            background=[("selected", "#203755")],
            foreground=[("selected", COLORS["text"])]
        )
        style.configure(
            "Recog.TCombobox",
            fieldbackground=COLORS["surface_alt"],
            background=COLORS["surface_alt"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["cyan"],
            bordercolor=COLORS["border"],
            padding=8
        )

    def _build_window(self):
        outer = tk.Frame(self.root, bg=COLORS["background"], padx=28, pady=24)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=COLORS["background"])
        header.pack(fill="x")
        brand = tk.Frame(header, bg=COLORS["background"])
        brand.pack(side="left", fill="x", expand=True)
        tk.Label(
            brand,
            text="RECOGD",
            bg=COLORS["background"],
            fg=COLORS["cyan"],
            font=("Sans", 12, "bold")
        ).pack(anchor="w")
        tk.Label(
            brand,
            text="Predictive asset preloader",
            bg=COLORS["background"],
            fg=COLORS["text"],
            font=("Sans", 23, "bold")
        ).pack(anchor="w", pady=(5, 0))
        tk.Label(
            brand,
            text="Keep frequently used game assets ready in memory.",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Sans", 10)
        ).pack(anchor="w", pady=(4, 0))

        self.status_dot = tk.Canvas(
            header, width=12, height=12, bg=COLORS["background"], highlightthickness=0
        )
        self.status_dot.pack(side="right", padx=(0, 8), pady=(15, 0))
        self.status_circle = self.status_dot.create_oval(
            2, 2, 10, 10, fill=COLORS["muted"], outline=""
        )
        self.status_label = tk.Label(
            header,
            text="Stopped",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Sans", 10, "bold")
        )
        self.status_label.pack(side="right", pady=(15, 0))

        controls = tk.Frame(
            outer, bg=COLORS["surface"], padx=16, pady=14,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        controls.pack(fill="x", pady=(22, 18))
        tk.Label(
            controls, text="PROFILE", bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Sans", 8, "bold")
        ).pack(side="left", padx=(0, 12))
        self.profile_var = tk.StringVar(value=next(iter(self.profiles)))
        self.profile_box = ttk.Combobox(
            controls,
            textvariable=self.profile_var,
            values=list(self.profiles),
            state="readonly",
            style="Recog.TCombobox",
            width=18
        )
        self.profile_box.pack(side="left")
        self.profile_name = tk.Label(
            controls,
            text=self.profiles[self.profile_var.get()].get("name", self.profile_var.get()),
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            font=("Sans", 9)
        )
        self.profile_name.pack(side="left", padx=12)
        self.profile_box.bind("<<ComboboxSelected>>", self._profile_changed)
        self.run_button = tk.Button(
            controls,
            text="▶   Start preloader",
            command=self.start_daemon,
            bg=COLORS["cyan"],
            fg="#07121d",
            activebackground="#76eee5",
            activeforeground="#07121d",
            font=("Sans", 10, "bold"),
            relief="flat",
            cursor="hand2",
            padx=18,
            pady=10,
            borderwidth=0
        )
        self.run_button.pack(side="right")

        metrics = tk.Frame(outer, bg=COLORS["background"])
        metrics.pack(fill="x")
        self.read_value = self._metric_card(metrics, "DISK READ", "0 B/s", COLORS["cyan"])
        self.write_value = self._metric_card(metrics, "DISK WRITE", "0 B/s", COLORS["blue"])
        self.preload_value = self._metric_card(metrics, "PRELOADED", "0 files", COLORS["green"])
        self.bytes_value = self._metric_card(metrics, "DATA WARMED", "0 B", COLORS["amber"])

        section_header = tk.Frame(outer, bg=COLORS["background"])
        section_header.pack(fill="x", pady=(24, 9))
        tk.Label(
            section_header, text="Disk activity", bg=COLORS["background"],
            fg=COLORS["text"], font=("Sans", 13, "bold")
        ).pack(side="left")
        tk.Label(
            section_header, text="Live daemon I/O", bg=COLORS["background"],
            fg=COLORS["muted"], font=("Sans", 9)
        ).pack(side="right")

        chart_panel = tk.Frame(
            outer, bg=COLORS["surface"], padx=12, pady=12,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        chart_panel.pack(fill="x")
        self.chart = tk.Canvas(
            chart_panel, height=140, bg=COLORS["surface"], highlightthickness=0
        )
        self.chart.pack(fill="x")
        self.chart.bind("<Configure>", lambda _event: self._draw_chart())

        timeline_header = tk.Frame(outer, bg=COLORS["background"])
        timeline_header.pack(fill="x", pady=(22, 9))
        tk.Label(
            timeline_header, text="Preload timeline", bg=COLORS["background"],
            fg=COLORS["text"], font=("Sans", 13, "bold")
        ).pack(side="left")
        tk.Label(
            timeline_header, text="Most recent assets", bg=COLORS["background"],
            fg=COLORS["muted"], font=("Sans", 9)
        ).pack(side="right")

        timeline_panel = tk.Frame(
            outer, bg=COLORS["surface"],
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        timeline_panel.pack(fill="both", expand=True)
        self.timeline = ttk.Treeview(
            timeline_panel,
            columns=("time", "asset", "bus", "size"),
            show="headings",
            style="Recog.Treeview",
            height=7
        )
        for column, heading, width, anchor in (
            ("time", "TIME", 150, "w"),
            ("asset", "PRELOADED ASSET", 440, "w"),
            ("bus", "SOURCE", 150, "w"),
            ("size", "WARMED", 100, "e")
        ):
            self.timeline.heading(column, text=heading)
            self.timeline.column(column, width=width, anchor=anchor, stretch=column == "asset")
        self.timeline.pack(fill="both", expand=True, padx=4, pady=4)
        self.timeline.bind("<Double-1>", self._show_event_path)
        tk.Label(
            outer,
            text="Close to tray · double-click a timeline item to copy its path",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=("Sans", 8)
        ).pack(anchor="w", pady=(9, 0))

    def _metric_card(self, parent, title, initial_value, accent):
        card = tk.Frame(
            parent, bg=COLORS["surface"], padx=16, pady=14,
            highlightthickness=1, highlightbackground=COLORS["border"]
        )
        card.pack(side="left", fill="both", expand=True, padx=(0, 10))
        tk.Label(
            card, text=title, bg=COLORS["surface"], fg=COLORS["muted"],
            font=("Sans", 8, "bold")
        ).pack(anchor="w")
        value = tk.Label(
            card, text=initial_value, bg=COLORS["surface"], fg=accent,
            font=("Sans", 17, "bold")
        )
        value.pack(anchor="w", pady=(8, 0))
        return value

    def _profile_changed(self, _event=None):
        profile = self.profile_var.get()
        self.profile_name.configure(
            text=self.profiles[profile].get("name", profile)
        )

    def _start_tray(self):
        image = Image.new("RGBA", (64, 64), COLORS["background"])
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=16, fill=COLORS["surface_alt"])
        draw.ellipse((43, 9, 53, 19), fill=COLORS["cyan"])
        draw.text((17, 15), "R", fill=COLORS["text"], stroke_width=1)
        menu = pystray.Menu(
            pystray.MenuItem("Open dashboard", self._tray_open, default=True),
            pystray.MenuItem("Start preloader", self._tray_start),
            pystray.MenuItem("Stop preloader", self._tray_stop),
            pystray.MenuItem("Quit RecogD", self._tray_quit)
        )
        self.tray_icon = pystray.Icon(
            "recogd", image, "RecogD asset preloader", menu=menu
        )
        self.tray_icon.run_detached()

    def _tray_open(self, _icon=None, _item=None):
        self.root.after(0, self.show_window)

    def _tray_start(self, _icon=None, _item=None):
        self.root.after(0, self.start_daemon)

    def _tray_stop(self, _icon=None, _item=None):
        self.root.after(0, self.stop_daemon)

    def _tray_quit(self, _icon=None, _item=None):
        self.root.after(0, self.quit)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self):
        self.root.withdraw()

    def start_daemon(self):
        if self._current_pid():
            return
        profile = self.profile_var.get()
        daemon_path = os.path.join(self.root_dir, "src", "daemon.py")
        command = [
            sys.executable,
            daemon_path,
            "--profile", profile,
            "--config", self.config_path,
            "--telemetry", self.telemetry_path
        ]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.root_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
        except OSError as error:
            self._set_status("error", f"Could not start daemon: {error}")
            return

        self.active_pid = self.process.pid
        self.stop_requested = False
        try:
            os.makedirs(os.path.dirname(self.pid_path), exist_ok=True)
            with open(self.pid_path, "w", encoding="utf-8") as pid_file:
                pid_file.write(str(self.active_pid))
        except OSError as error:
            self.process.terminate()
            self._set_status("error", f"Could not save daemon process ID: {error}")
            return
        threading.Thread(
            target=self._read_process_output,
            args=(self.process,),
            name="RecogD-output",
            daemon=True
        ).start()
        self._set_status("starting", "Starting the preloader…")

    def _read_process_output(self, process):
        if process.stdout is None:
            return
        for line in process.stdout:
            with self.output_lock:
                self.output_lines.append(line.strip())

    def stop_daemon(self):
        pid = self._current_pid()
        if pid is None:
            self.active_pid = None
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._remove_pid_file()
            self.active_pid = None
            return
        except PermissionError as error:
            self._set_status("error", f"Could not stop daemon: {error}")
            return
        self.stop_requested = True
        self._set_status("stopping", "Saving the preload index…")

    def _current_pid(self):
        if self.process is not None:
            if self.process.poll() is None:
                return self.process.pid
            self.process = None
        if self.active_pid is not None:
            if managed_daemon_pid(self.pid_path, self.telemetry_path) == self.active_pid:
                return self.active_pid
            self._remove_pid_file()
            self.active_pid = None
        return None

    def _refresh(self):
        self._read_telemetry()
        pid = self._current_pid()
        if pid is not None:
            self._refresh_io(pid)
            status = self.telemetry.get("status", "starting")
            if status in ("running", "starting", "stopping"):
                message = self.telemetry.get("message", "")
                self._set_status(status, message)
            else:
                message = self.telemetry.get("message", "")
                self._set_status("starting", message or "Waiting for daemon startup…")
        else:
            self.read_rate = 0.0
            self.write_rate = 0.0
            self.last_io = None
            self.last_io_time = None
            status = self.telemetry.get("status", "stopped")
            message = self.telemetry.get("message", "")
            if status == "running":
                status = "stopped"
                message = "The daemon is no longer running."
            elif status == "error":
                self._set_status("error", message or "The daemon could not start.")
            else:
                self._set_status("stopped", "Ready to preload.")
            if status != "error":
                self._set_status(status, message)
            if self.active_pid is not None:
                self._remove_pid_file()
        self._update_metrics()
        self._draw_chart()
        self.root.after(1000, self._refresh)

    def _read_telemetry(self):
        try:
            with open(self.telemetry_path, "r", encoding="utf-8") as telemetry_file:
                self.telemetry = json.load(telemetry_file)
        except FileNotFoundError:
            self.telemetry = {}
        except (OSError, json.JSONDecodeError) as error:
            self._set_status("error", f"Could not read runtime telemetry: {error}")

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
                self.read_rate = max(0, current[0] - self.last_io[0]) / elapsed
                self.write_rate = max(0, current[1] - self.last_io[1]) / elapsed
        self.last_io = current
        self.last_io_time = now
        self.io_samples.append((self.read_rate, self.write_rate))

    def _update_metrics(self):
        stats = self.telemetry.get("stats", {})
        history = self.telemetry.get("preloaded", [])
        self.read_value.configure(text=f"{format_bytes(self.read_rate)}/s")
        self.write_value.configure(text=f"{format_bytes(self.write_rate)}/s")
        self.preload_value.configure(
            text=f"{stats.get('total_pinned_files', 0):,} files"
        )
        self.bytes_value.configure(
            text=format_bytes(stats.get("total_pinned_bytes", 0))
        )
        current_items = self.timeline.get_children()
        if current_items:
            self.timeline.delete(*current_items)
        events = list(reversed(history[-8:]))
        for index, event in enumerate(events):
            history_index = len(history) - index - 1
            item_id = f"preload-{history_index}"
            try:
                event_time = datetime.fromisoformat(event.get("timestamp", ""))
                display_time = event_time.strftime("%b %d  %H:%M:%S")
            except ValueError:
                display_time = "Recently"
            self.timeline.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    display_time,
                    event.get("name", os.path.basename(event.get("path", "Asset"))),
                    event.get("bus", "Unknown"),
                    format_bytes(event.get("size_bytes", 0))
                )
            )

    def _draw_chart(self):
        width = self.chart.winfo_width()
        height = self.chart.winfo_height()
        if width < 50 or height < 50:
            return
        self.chart.delete("all")
        for fraction in (0.25, 0.5, 0.75):
            y = int(height * fraction)
            self.chart.create_line(
                0, y, width, y, fill=COLORS["border"], dash=(2, 5)
            )
        samples = list(self.io_samples)
        if len(samples) < 2:
            self.chart.create_text(
                width / 2, height / 2,
                text="Waiting for disk activity…",
                fill=COLORS["muted"],
                font=("Sans", 10)
            )
            return
        peak = max(max(read, write) for read, write in samples)
        scale = max(peak, 1024)
        for values, color in (
            ([sample[0] for sample in samples], COLORS["cyan"]),
            ([sample[1] for sample in samples], COLORS["blue"])
        ):
            points = []
            for sample_index, value in enumerate(values):
                x = (sample_index / (len(samples) - 1)) * width
                y = height - 12 - (value / scale) * (height - 24)
                points.extend((x, y))
            self.chart.create_line(
                *points, fill=color, width=2, smooth=True
            )
        self.chart.create_text(
            8, 8, anchor="nw", text=f"Peak  {format_bytes(peak)}/s",
            fill=COLORS["muted"], font=("Sans", 8)
        )

    def _set_status(self, state, message):
        appearance = {
            "running": ("Running", COLORS["green"]),
            "starting": ("Starting", COLORS["amber"]),
            "stopping": ("Stopping", COLORS["amber"]),
            "error": ("Needs attention", COLORS["red"]),
            "stopped": ("Stopped", COLORS["muted"])
        }
        label, color = appearance.get(state, ("Stopped", COLORS["muted"]))
        self.status_label.configure(text=label, fg=color)
        self.status_dot.itemconfigure(self.status_circle, fill=color)
        if state in ("running", "starting", "stopping"):
            self.run_button.configure(
                text="■   Stop preloader",
                command=self.stop_daemon,
                bg=COLORS["surface_alt"],
                fg=COLORS["text"],
                activebackground=COLORS["border"],
                activeforeground=COLORS["text"]
            )
            self.root.title(f"RecogD · {label}")
        else:
            self.run_button.configure(
                text="▶   Start preloader",
                command=self.start_daemon,
                bg=COLORS["cyan"],
                fg="#07121d",
                activebackground="#76eee5",
                activeforeground="#07121d"
            )
            self.root.title("RecogD · Asset Preloader")
        if state == "error" and message:
            self.status_label.configure(text=f"Needs attention · {message[:65]}")

    def _show_event_path(self, _event):
        selection = self.timeline.selection()
        if not selection:
            return
        try:
            index = int(selection[0].removeprefix("preload-"))
            history = self.telemetry.get("preloaded", [])
            path = history[index].get("path", "")
        except (ValueError, IndexError):
            return
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)
            self._set_status(
                self.telemetry.get("status", "stopped"),
                f"Copied {path}"
            )

    def _remove_pid_file(self):
        try:
            with open(self.pid_path, "r", encoding="utf-8") as pid_file:
                recorded_pid = pid_file.read().strip()
            if recorded_pid == str(self.active_pid):
                os.unlink(self.pid_path)
        except FileNotFoundError:
            pass

    def quit(self):
        if self._current_pid() is not None:
            self.stop_daemon()
        try:
            self.tray_icon.stop()
        except (AttributeError, RuntimeError):
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    RecogDDesktop().run()


if __name__ == "__main__":
    main()
