#!/usr/bin/env python3
import os
import sys
import os
# Force working directory to script location so relative assets resolve properly
os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
import warnings

warnings.filterwarnings("ignore")

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib
except Exception as e:
    print(f"[GUI-WARN] GTK3 unavailable ({e}). Running in CLI-only mode.")
    sys.exit(0)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config_ipc

APP_INDICATOR = None
for mod_name in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(mod_name, "0.1")
        APP_INDICATOR = getattr(gi.repository, mod_name)
        break
    except Exception:
        pass

DIRECT_CSS = b"""
window, .background {
    background-color: #070a0f;
    color: #f0f6fc;
    font-family: "OpenDyslexic", "Noto Sans", sans-serif;
    font-size: 11pt;
}
.panel-box {
    background-color: #0d131d;
    border: 2px solid #1e2d42;
    border-radius: 10px;
    padding: 12px;
}
.banner-normal {
    background-color: #09131c;
    border: 2px solid #00e5ff;
    border-radius: 10px;
    padding: 12px;
}
.banner-shield {
    background-color: #1f0810;
    border: 2px solid #ff2a55;
    border-radius: 10px;
    padding: 12px;
}
.title-text {
    font-size: 13pt;
    font-weight: bold;
    color: #00e5ff;
}
.val-highlight {
    font-size: 11pt;
    font-weight: bold;
    color: #00ff88;
}
button {
    background-image: none;
    background-color: #121c2b;
    color: #ffffff;
    border: 2px solid #00e5ff;
    border-radius: 8px;
    padding: 9px 12px;
    font-weight: bold;
}
button:hover {
    background-color: #1b2c44;
}
button.active-shield {
    background-color: #420b18;
    border-color: #ff2a55;
    color: #ff9ebb;
}
button.active-pause {
    background-color: #3b2600;
    border-color: #ffaa00;
    color: #ffe082;
}
trough {
    background-color: #16202e;
    border-radius: 6px;
    min-height: 12px;
}
progress, highlight {
    background-image: none;
    background-color: #00ff88;
    border-radius: 6px;
    min-height: 12px;
}
"""


def create_tray_icons():
    uid = os.getuid()
    palette = {
        "standby": ("#00e5ff", "#071a24"),
        "active": ("#00ff88", "#062919"),
        "shield": ("#ff2a55", "#330711"),
        "paused": ("#ffaa00", "#2e1f00")
    }
    paths = {}
    for key, (fg, bg) in palette.items():
        path = f"/dev/shm/recogd_icon_{uid}_{key}.svg"
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
            f'<rect x="4" y="4" width="56" height="56" rx="12" fill="{bg}" stroke="{fg}" stroke-width="5"/>'
            f'<polygon points="24,16 46,32 24,48" fill="{fg}"/>'
            f'</svg>'
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
        except Exception:
            pass
        paths[key] = path
    return paths


class SimpleControlDeck(Gtk.Window):
    def __init__(self):
        super().__init__(title="RecogD Preshader — Control Deck")
        self.set_default_size(500, 520)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(12)

        self.icons = create_tray_icons()
        if os.path.exists(self.icons["standby"]):
            try:
                self.set_icon_from_file(self.icons["standby"])
            except Exception:
                pass

        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(DIRECT_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.cfg = config_ipc.read_live_config()
        self.current_icon = "standby"
        self.connect("delete-event", self._on_close_to_tray)

        self._build_layout()
        self._setup_tray()
        self._poll_state()
        GLib.timeout_add(500, self._poll_state)

    def _build_layout(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add(vbox)

        # 1. Top Status Banner
        self.banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.banner.get_style_context().add_class("banner-normal")
        vbox.pack_start(self.banner, False, False, 0)

        self.lbl_title = Gtk.Label(label="RECOGD PRESHADER — DIRECT DECK")
        self.lbl_title.get_style_context().add_class("title-text")
        self.lbl_title.set_xalign(0.0)
        self.banner.pack_start(self.lbl_title, False, False, 0)

        self.lbl_status = Gtk.Label(label="State: Initializing...")
        self.lbl_status.set_xalign(0.0)
        self.banner.pack_start(self.lbl_status, False, False, 0)

        self.lbl_game = Gtk.Label(label="Game: Waiting for Steam game...")
        self.lbl_game.set_xalign(0.0)
        self.banner.pack_start(self.lbl_game, False, False, 0)

        # 2. Live Metrics Box
        metrics_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        metrics_box.get_style_context().add_class("panel-box")
        vbox.pack_start(metrics_box, False, False, 0)

        self.lbl_ram = Gtk.Label(label="Pinned Cache: 0 MB / 1536 MB  |  System Free RAM: 8192 MB")
        self.lbl_ram.get_style_context().add_class("val-highlight")
        self.lbl_ram.set_xalign(0.0)
        metrics_box.pack_start(self.lbl_ram, False, False, 0)

        self.ram_bar = Gtk.ProgressBar()
        metrics_box.pack_start(self.ram_bar, False, False, 0)

        self.lbl_bus = Gtk.Label(label="NVMe Pinned: 0 MB (0 files)   |   Ext SSD Pinned: 0 MB (0 files)")
        self.lbl_bus.set_xalign(0.0)
        metrics_box.pack_start(self.lbl_bus, False, False, 0)

        self.lbl_vel = Gtk.Label(label="Velocity: 0.0/s (1 Hop)   |   OOM Evicted: 0 MB   |   Spike Shed: 0")
        self.lbl_vel.set_xalign(0.0)
        metrics_box.pack_start(self.lbl_vel, False, False, 0)

        # 3. Direct Action Buttons (2x2 Grid)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        btn_box.get_style_context().add_class("panel-box")
        vbox.pack_start(btn_box, False, False, 0)

        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.pack_start(row1, False, False, 0)

        self.btn_shield = Gtk.Button(label="Combat Spike Shield: AUTO")
        self.btn_shield.connect("clicked", self._toggle_shield)
        row1.pack_start(self.btn_shield, True, True, 0)

        self.btn_pause = Gtk.Button(label="Pause Pinning: OFF")
        self.btn_pause.connect("clicked", self._toggle_pause)
        row1.pack_start(self.btn_pause, True, True, 0)

        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.pack_start(row2, False, False, 0)

        btn_purge = Gtk.Button(label="Purge Pinned RAM Now")
        btn_purge.connect("clicked", self._purge_ram)
        row2.pack_start(btn_purge, True, True, 0)

        btn_tray = Gtk.Button(label="Minimize to System Tray")
        btn_tray.connect("clicked", lambda *_: self.hide())
        row2.pack_start(btn_tray, True, True, 0)

        # 4. Essential Sliders Only (3 Direct Sliders)
        sliders_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sliders_box.get_style_context().add_class("panel-box")
        vbox.pack_start(sliders_box, True, True, 0)

        self.slider_cache = self._create_slider(
            sliders_box,
            "Max Pinned Cache Cap (MB):",
            256, 3072, 128,
            self.cfg.get("max_cache_mb", 1536)
        )
        self.slider_free_ram = self._create_slider(
            sliders_box,
            "Protected Free System RAM Floor (MB) [Prevents Crashes]:",
            3000, 7000, 250,
            self.cfg.get("min_free_ram_mb", 4500)
        )
        self.slider_hops = self._create_slider(
            sliders_box,
            "Max Predictive Lookahead Depth (Hops):",
            1, 3, 1,
            self.cfg.get("max_hop_depth", 2)
        )

    def _create_slider(self, parent, title, lo, hi, step, val):
        lbl = Gtk.Label(label=title)
        lbl.set_xalign(0.0)
        parent.pack_start(lbl, False, False, 0)

        adj = Gtk.Adjustment(
            value=float(val),
            lower=float(lo),
            upper=float(hi),
            step_increment=float(step),
            page_increment=float(step) * 2
        )
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        scale.set_digits(0)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.connect("value-changed", self._on_slider_changed)
        parent.pack_start(scale, False, False, 0)
        return scale

    def _setup_tray(self):
        self.menu = Gtk.Menu()

        m_show = Gtk.MenuItem(label="Show / Hide Control Deck")
        m_show.connect("activate", self._toggle_window)
        self.menu.append(m_show)

        m_shield = Gtk.MenuItem(label="Toggle Combat Spike Shield")
        m_shield.connect("activate", self._toggle_shield)
        self.menu.append(m_shield)

        m_purge = Gtk.MenuItem(label="Purge Pinned RAM Cache")
        m_purge.connect("activate", self._purge_ram)
        self.menu.append(m_purge)

        self.menu.append(Gtk.SeparatorMenuItem())

        m_quit_gui = Gtk.MenuItem(label="Close Window (Keep Daemon Active)")
        m_quit_gui.connect("activate", lambda *_: Gtk.main_quit())
        self.menu.append(m_quit_gui)

        m_quit_all = Gtk.MenuItem(label="Stop Daemon & Exit")
        m_quit_all.connect("activate", self._stop_all)
        self.menu.append(m_quit_all)

        self.menu.show_all()

        self.indicator = None
        self.status_icon = None

        if APP_INDICATOR is not None:
            try:
                self.indicator = APP_INDICATOR.Indicator.new(
                    "recogd-control-deck",
                    self.icons["standby"],
                    APP_INDICATOR.IndicatorCategory.SYSTEM_SERVICES
                )
                self.indicator.set_status(APP_INDICATOR.IndicatorStatus.ACTIVE)
                self.indicator.set_menu(self.menu)
            except Exception:
                self.indicator = None

        if self.indicator is None:
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.set_from_file(self.icons["standby"])
            self.status_icon.set_tooltip_text("RecogD Preshader Control Deck")
            self.status_icon.connect("activate", self._toggle_window)
            self.status_icon.connect("popup-menu", self._on_tray_popup)
            self.status_icon.set_visible(True)

    def _update_tray_icon(self, key):
        if key == self.current_icon:
            return
        self.current_icon = key
        path = self.icons.get(key, self.icons["standby"])
        if self.indicator is not None:
            try:
                self.indicator.set_icon_full(path, f"RecogD {key}")
            except Exception:
                pass
        if self.status_icon is not None:
            try:
                self.status_icon.set_from_file(path)
            except Exception:
                pass

    def _toggle_window(self, *_):
        if self.get_visible():
            self.hide()
        else:
            self.deiconify()
            self.present()

    def _on_tray_popup(self, icon, button, activate_time):
        self.menu.popup(None, None, None, None, button, activate_time)

    def _on_close_to_tray(self, *_):
        self.hide()
        return True

    def _toggle_shield(self, *_):
        cfg = config_ipc.read_live_config()
        new_state = not cfg.get("force_combat_shield", False)
        self.cfg = config_ipc.update_live_config({"force_combat_shield": new_state}, save_persistent=False)
        self._poll_state()

    def _toggle_pause(self, *_):
        cfg = config_ipc.read_live_config()
        new_state = not cfg.get("pause_engine", False)
        self.cfg = config_ipc.update_live_config({"pause_engine": new_state}, save_persistent=False)
        self._poll_state()

    def _purge_ram(self, *_):
        cfg = config_ipc.read_live_config()
        gen = int(cfg.get("flush_generation", 0)) + 1
        self.cfg = config_ipc.update_live_config({"flush_generation": gen}, save_persistent=False)

    def _on_slider_changed(self, *_):
        updates = {
            "max_cache_mb": int(self.slider_cache.get_value()),
            "min_free_ram_mb": int(self.slider_free_ram.get_value()),
            "max_hop_depth": int(self.slider_hops.get_value())
        }
        self.cfg = config_ipc.update_live_config(updates, save_persistent=True)

    def _stop_all(self, *_):
        config_ipc.update_live_config({"shutdown_requested": True}, save_persistent=False)
        Gtk.main_quit()

    def _poll_state(self):
        state = config_ipc.read_live_state()
        self.cfg = config_ipc.read_live_config()

        shield_locked = bool(self.cfg.get("force_combat_shield", False))
        paused = bool(self.cfg.get("pause_engine", False))
        combat = bool(state.get("combat_burst", False))

        s_ctx = self.btn_shield.get_style_context()
        if shield_locked:
            self.btn_shield.set_label("Combat Spike Shield: LOCKED ON")
            s_ctx.add_class("active-shield")
        else:
            self.btn_shield.set_label("Combat Spike Shield: AUTO")
            s_ctx.remove_class("active-shield")

        p_ctx = self.btn_pause.get_style_context()
        if paused:
            self.btn_pause.set_label("Resume Engine (Paused)")
            p_ctx.add_class("active-pause")
        else:
            self.btn_pause.set_label("Pause Pinning: OFF")
            p_ctx.remove_class("active-pause")

        b_ctx = self.banner.get_style_context()
        if combat or shield_locked:
            b_ctx.remove_class("banner-normal")
            b_ctx.add_class("banner-shield")
            self._update_tray_icon("shield")
        elif paused:
            b_ctx.remove_class("banner-shield")
            b_ctx.add_class("banner-normal")
            self._update_tray_icon("paused")
        elif state.get("appid", "-") != "-" and state.get("daemon_online"):
            b_ctx.remove_class("banner-shield")
            b_ctx.add_class("banner-normal")
            self._update_tray_icon("active")
        else:
            b_ctx.remove_class("banner-shield")
            b_ctx.add_class("banner-normal")
            self._update_tray_icon("standby")

        self.lbl_status.set_text(
            f"State: {state.get('status_text', 'STANDBY')}  |  Vault: {state.get('vault_mode', 'Auto')}"
        )
        self.lbl_game.set_text(
            f"Game: {state.get('game_name', 'None')} (AppID: {state.get('appid', '-')})"
        )

        act_mb = state.get("active_ram_mb", 0.0)
        cap_mb = max(1.0, state.get("budget_ram_mb", 1536.0))
        free_mb = state.get("avail_ram_mb", 8192.0)

        self.lbl_ram.set_text(
            f"Pinned Cache: {act_mb:.1f} MB / {cap_mb:.0f} MB Cap  |  System Free RAM: {free_mb:.0f} MB"
        )
        self.ram_bar.set_fraction(min(1.0, max(0.0, act_mb / cap_mb)))

        self.lbl_bus.set_text(
            f"NVMe Pinned: {state.get('nvme_mb', 0.0):.1f} MB ({state.get('nvme_files', 0)} files)   |   "
            f"Ext SSD Pinned: {state.get('sata_mb', 0.0):.1f} MB ({state.get('sata_files', 0)} files)"
        )
        self.lbl_vel.set_text(
            f"Velocity: {state.get('velocity', 0.0):.1f}/s ({state.get('hop_depth', 1)} Hops)   |   "
            f"OOM Evicted: {state.get('evicted_mb', 0.0):.1f} MB   |   Spike Shed: {state.get('shed_writes', 0)}"
        )
        return True


def main():
    win = SimpleControlDeck()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
