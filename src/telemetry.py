#!/usr/bin/env python3
import json
import os
import threading
import time
from datetime import datetime


class RuntimeTelemetry:
    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.lock = threading.Lock()
        self.data = {
            "status": "stopped",
            "profile": None,
            "message": "",
            "updated_at": time.time(),
            "stats": {},
            "preloaded": []
        }

        try:
            with open(self.path, "r", encoding="utf-8") as telemetry_file:
                saved_data = json.load(telemetry_file)
            if isinstance(saved_data, dict):
                self.data.update(saved_data)
                if not isinstance(self.data.get("preloaded"), list):
                    self.data["preloaded"] = []
                if not isinstance(self.data.get("stats"), dict):
                    self.data["stats"] = {}
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as error:
            print(f"[RECOGD-TELEMETRY] Could not load {self.path}: {error}")

        self._write()

    def set_status(self, status, profile=None, message=""):
        with self.lock:
            self.data["status"] = status
            if profile is not None:
                self.data["profile"] = profile
            self.data["message"] = message
            self.data["updated_at"] = time.time()
            self._write_locked()

    def update_stats(self, stats):
        with self.lock:
            self.data["stats"] = dict(stats)
            self.data["updated_at"] = time.time()
            self._write_locked()

    def record_preload(self, filepath, size_bytes, is_external, stats):
        event = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "path": filepath,
            "name": os.path.basename(filepath),
            "size_bytes": size_bytes,
            "bus": "External SSD" if is_external else "Internal NVMe"
        }
        with self.lock:
            self.data["stats"] = dict(stats)
            self.data["updated_at"] = time.time()
            self.data["preloaded"].append(event)
            self.data["preloaded"] = self.data["preloaded"][-100:]
            self._write_locked()

    def _write(self):
        with self.lock:
            self._write_locked()

    def _write_locked(self):
        self.data["updated_at"] = time.time()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as telemetry_file:
                json.dump(self.data, telemetry_file)
            os.replace(tmp_path, self.path)
        except OSError as error:
            print(f"[RECOGD-TELEMETRY] Could not save {self.path}: {error}")
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
