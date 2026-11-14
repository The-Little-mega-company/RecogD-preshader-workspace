#!/usr/bin/env python3
import os
import glob
import math
import time
import select
import struct
import threading
from collections import deque

# Linux input_event struct on 64-bit systems: (tv_sec, tv_usec, type, code, value)
INPUT_EVENT_STRUCT = struct.Struct("llHHi")

EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

REL_X = 0x00
REL_Y = 0x01
REL_RX = 0x03
REL_RY = 0x04

ABS_X = 0x00
ABS_Y = 0x01
ABS_Z = 0x02
ABS_RX = 0x03
ABS_RY = 0x04
ABS_RZ = 0x05

# Common WASD / movement / vehicle sprint keycodes in Linux input-event-codes.h
MOVEMENT_KEYS = {
    17,  # KEY_W
    30,  # KEY_A
    31,  # KEY_S
    32,  # KEY_D
    42,  # KEY_LEFTSHIFT (sprint / vehicle boost)
    57,  # KEY_SPACE
    103, # KEY_UP
    105, # KEY_LEFT
    106, # KEY_RIGHT
    108, # KEY_DOWN
}


class PredictiveVelocityTracker:
    """
    Monitors real-time player input velocity (camera panning speed, analog stick
    deflection, movement keys) alongside asset streaming cadence to dynamically
    scale pre-fetch depth and multi-hop lookahead before the character turns a corner.
    """

    def __init__(self, config=None, enable_evdev=True):
        self.lock = threading.Lock()
        self.mouse_samples = deque()   # (timestamp, delta_magnitude)
        self.key_samples = deque()     # (timestamp, weight)
        self.asset_samples = deque()   # (timestamp, filepath)
        self.active_keys = set()
        self.stick_magnitude = 0.0

        self.current_velocity = 0.0
        self.peak_velocity = 0.0
        self.state = "IDLE"
        self.corner_boosts = 0

        self.input_fds = []
        self._last_device_scan = 0.0
        self._evdev_requested = enable_evdev
        self.apply_config(config)
        if self.enable_evdev:
            self._open_input_devices()

    def apply_config(self, config):
        cfg = (config or {}).get("velocity", {}) if isinstance(config, dict) else {}
        with self.lock:
            self.enabled = bool(cfg.get("enabled", True))
            self.window_seconds = float(cfg.get("sample_window_seconds", 1.5))
            self.pan_sensitivity = float(cfg.get("mouse_pan_reference_px_per_sec", 1800.0))
            self.asset_rate_reference = float(cfg.get("asset_burst_reference_per_sec", 25.0))
            self.max_multiplier = float(cfg.get("max_preload_multiplier", 2.5))
            self.corner_lookahead_threshold = float(cfg.get("corner_lookahead_threshold", 0.55))
            self.max_lookahead_hops = int(cfg.get("max_lookahead_hops", 2))
            self.lookahead_children_limit = int(cfg.get("lookahead_children_limit", 12))
            self.enable_evdev = self._evdev_requested and self.enabled

    def _open_input_devices(self):
        self.close_devices()
        for path in sorted(glob.glob("/dev/input/event*")):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                self.input_fds.append(fd)
            except (OSError, PermissionError):
                continue
        self._last_device_scan = time.monotonic()

    def close_devices(self):
        for fd in self.input_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.input_fds.clear()

    def poll_hardware_input(self, now=None):
        """
        Non-blocking read of available Linux /dev/input/event* streams to capture
        rapid mouse camera panning, controller analog stick velocity, and movement keys.
        """
        if not self.enable_evdev:
            return
        if now is None:
            now = time.monotonic()

        if not self.input_fds and (now - self._last_device_scan) > 30.0:
            self._open_input_devices()

        if not self.input_fds:
            return

        try:
            ready, _, _ = select.select(self.input_fds, [], [], 0.0)
        except (ValueError, OSError):
            return

        event_size = INPUT_EVENT_STRUCT.size
        dead_fds = []

        for fd in ready:
            try:
                raw = os.read(fd, event_size * 64)
            except BlockingIOError:
                continue
            except OSError:
                dead_fds.append(fd)
                continue

            offset = 0
            raw_len = len(raw)
            dx = 0.0
            dy = 0.0

            while offset + event_size <= raw_len:
                _, _, ev_type, ev_code, ev_val = INPUT_EVENT_STRUCT.unpack_from(raw, offset)
                offset += event_size

                if ev_type == EV_REL:
                    if ev_code in (REL_X, REL_RX):
                        dx += abs(ev_val)
                    elif ev_code in (REL_Y, REL_RY):
                        dy += abs(ev_val)
                elif ev_type == EV_KEY and ev_code in MOVEMENT_KEYS:
                    if ev_val == 1:  # key down
                        with self.lock:
                            self.active_keys.add(ev_code)
                            weight = 1.5 if ev_code == 42 else 1.0
                            self.key_samples.append((now, weight))
                    elif ev_val == 0:  # key up
                        with self.lock:
                            self.active_keys.discard(ev_code)
                    elif ev_val == 2:  # key repeat (held down while moving/driving)
                        with self.lock:
                            weight = 1.2 if ev_code == 42 else 0.6
                            self.key_samples.append((now, weight))
                elif ev_type == EV_ABS and ev_code in (ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ):
                    # Normalize typical signed 16-bit or unsigned 8/16-bit axis deflection
                    norm = min(1.0, abs(ev_val) / 32768.0)
                    with self.lock:
                        self.stick_magnitude = max(self.stick_magnitude * 0.85, norm)

            if dx > 0 or dy > 0:
                mag = math.hypot(dx, dy)
                with self.lock:
                    self.mouse_samples.append((now, mag))

        for fd in dead_fds:
            if fd in self.input_fds:
                self.input_fds.remove(fd)
            try:
                os.close(fd)
            except OSError:
                pass

    def record_input_sample(self, mouse_delta=0.0, key_weight=0.0, stick_deflection=0.0, now=None):
        """
        Direct injection method for synthetic input telemetry or testing.
        """
        if now is None:
            now = time.monotonic()
        with self.lock:
            if mouse_delta > 0:
                self.mouse_samples.append((now, float(mouse_delta)))
            if key_weight > 0:
                self.key_samples.append((now, float(key_weight)))
            if stick_deflection > 0:
                self.stick_magnitude = max(self.stick_magnitude, min(1.0, float(stick_deflection)))
            self._recalculate_locked(now)

    def record_asset_event(self, filepath, now=None):
        """
        Records an asset open event and updates the composite velocity score.
        Rapid bursts of distinct terrain/world assets indicate high traversal speed
        (e.g. driving a vehicle in DayZ or sprinting across map grids).
        """
        if now is None:
            now = time.monotonic()
        self.poll_hardware_input(now)
        with self.lock:
            self.asset_samples.append((now, filepath))
            return self._recalculate_locked(now)

    def _prune_locked(self, now):
        cutoff = now - self.window_seconds
        while self.mouse_samples and self.mouse_samples[0][0] < cutoff:
            self.mouse_samples.popleft()
        while self.key_samples and self.key_samples[0][0] < cutoff:
            self.key_samples.popleft()
        while self.asset_samples and self.asset_samples[0][0] < cutoff:
            self.asset_samples.popleft()
        if not self.mouse_samples and not self.key_samples:
            self.stick_magnitude *= 0.7

    def _recalculate_locked(self, now):
        self._prune_locked(now)
        window = max(0.25, self.window_seconds)

        # 1. Camera panning angular velocity (pixels/counts per second)
        total_mouse_delta = sum(mag for _, mag in self.mouse_samples)
        pan_rate = total_mouse_delta / window
        pan_score = min(1.0, pan_rate / max(1.0, self.pan_sensitivity))

        # 2. Movement key / sprint / controller activity
        key_sum = sum(w for _, w in self.key_samples)
        sprint_bonus = 0.35 if 42 in self.active_keys else 0.0
        move_held = min(0.5, len(self.active_keys) * 0.2)
        key_score = min(1.0, (key_sum / (window * 6.0)) + move_held + sprint_bonus)
        input_score = max(pan_score, key_score, self.stick_magnitude)

        # 3. Asset streaming burst velocity (unique assets opened per second)
        unique_assets = len({fp for _, fp in self.asset_samples})
        asset_rate = unique_assets / window
        asset_score = min(1.0, asset_rate / max(1.0, self.asset_rate_reference))

        # Combine hardware input velocity and asset traversal cadence
        if input_score > 0.0 and asset_score > 0.0:
            composite = min(1.0, (0.65 * max(input_score, asset_score)) + (0.35 * min(input_score, asset_score)))
        else:
            composite = max(input_score, asset_score)

        self.current_velocity = round(composite, 4)
        if self.current_velocity > self.peak_velocity:
            self.peak_velocity = self.current_velocity

        if self.current_velocity >= 0.75:
            self.state = "VEHICLE_OR_RAPID_PAN"
        elif self.current_velocity >= self.corner_lookahead_threshold:
            self.state = "SPRINTING"
        elif self.current_velocity >= 0.20:
            self.state = "WALKING"
        else:
            self.state = "IDLE"

        return self.current_velocity

    def get_adaptive_profile(self, base_max_preload, base_time_window, base_min_confidence, now=None):
        """
        Computes adaptive engine parameters scaled by current velocity:
        - effective_max_preload: scales up as velocity increases
        - effective_time_window: widens slightly at high speed to link rapid asset chains
        - effective_min_confidence: lowers threshold during rapid panning/driving
        - lookahead_hops: 2 hops when turning corners / driving fast, 1 hop otherwise
        """
        if not self.enabled:
            return {
                "velocity": 0.0,
                "state": "STATIC",
                "multiplier": 1.0,
                "max_preload": base_max_preload,
                "time_window": base_time_window,
                "min_confidence": base_min_confidence,
                "lookahead_hops": 1,
                "lookahead_limit": 0
            }

        if now is None:
            now = time.monotonic()
        self.poll_hardware_input(now)

        with self.lock:
            velocity = self._recalculate_locked(now)
            state = self.state

        # Scale multiplier smoothly from 1.0x at idle up to self.max_multiplier at velocity=1.0
        multiplier = 1.0 + (self.max_multiplier - 1.0) * velocity
        effective_max_preload = max(1, int(round(base_max_preload * multiplier)))
        effective_time_window = round(base_time_window * (1.0 + 0.5 * velocity), 3)

        if velocity >= self.corner_lookahead_threshold and base_min_confidence > 1:
            effective_min_confidence = max(1, base_min_confidence - 1)
        else:
            effective_min_confidence = base_min_confidence

        lookahead_hops = self.max_lookahead_hops if velocity >= self.corner_lookahead_threshold else 1
        lookahead_limit = (
            max(1, int(round(self.lookahead_children_limit * velocity)))
            if lookahead_hops > 1
            else 0
        )

        return {
            "velocity": velocity,
            "state": state,
            "multiplier": round(multiplier, 2),
            "max_preload": effective_max_preload,
            "time_window": effective_time_window,
            "min_confidence": effective_min_confidence,
            "lookahead_hops": lookahead_hops,
            "lookahead_limit": lookahead_limit
        }

    def get_asset_event_rate(self, now=None):
        if now is None:
            now = time.monotonic()
        with self.lock:
            self._prune_locked(now)
            return len(self.asset_samples) / max(0.25, self.window_seconds)

    def record_corner_boost(self, count=1):
        with self.lock:
            self.corner_boosts += count

    def get_stats(self):
        with self.lock:
            return {
                "velocity_score": self.current_velocity,
                "peak_velocity_score": self.peak_velocity,
                "velocity_state": self.state,
                "corner_lookahead_boosts": self.corner_boosts,
                "monitored_input_devices": len(self.input_fds)
            }

    def shutdown(self):
        self.close_devices()
