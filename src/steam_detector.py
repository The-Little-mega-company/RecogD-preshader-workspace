#!/usr/bin/env python3
import os
import re
import glob

IGNORED_APP_NAMES = {
    "steamworks common redistributables",
    "steam linux runtime",
    "proton",
    "steam controller",
    "steamvr"
}


def sanitize_name(name):
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")
    return cleaned or "UnknownGame"


class SteamGameDetector:
    def __init__(self, extra_mount="/mnt/preshader"):
        self.extra_mount = extra_mount
        self.libraries = set()
        self.manifests = {}
        self.installdir_to_appid = {}
        self.refresh_libraries()

    def refresh_libraries(self):
        candidates = [
            os.path.expanduser("~/.local/share/Steam"),
            os.path.expanduser("~/.steam/steam"),
            os.path.expanduser("~/.var/app/com.valvesoftware.Steam/.local/share/Steam"),
            os.path.join(self.extra_mount, "SteamLibrary"),
            self.extra_mount
        ]

        # Parse libraryfolders.vdf for any external or secondary drives
        for base in list(candidates):
            vdf_path = os.path.join(base, "steamapps", "libraryfolders.vdf")
            if os.path.isfile(vdf_path):
                try:
                    with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    for match in re.findall(r'"path"\s+"([^"]+)"', content):
                        candidates.append(match.replace("\\\\", "/"))
                except OSError:
                    pass

        for path in candidates:
            steamapps = os.path.join(path, "steamapps")
            if os.path.isdir(steamapps):
                self.libraries.add(os.path.abspath(steamapps))

        for steamapps in self.libraries:
            for acf_file in glob.glob(os.path.join(steamapps, "appmanifest_*.acf")):
                self._parse_acf(steamapps, acf_file)

    def _parse_acf(self, steamapps_dir, acf_path):
        try:
            with open(acf_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            appid_m = re.search(r'"appid"\s+"(\d+)"', text)
            name_m = re.search(r'"name"\s+"([^"]+)"', text)
            dir_m = re.search(r'"installdir"\s+"([^"]+)"', text)
            if not (appid_m and name_m and dir_m):
                return

            appid = appid_m.group(1)
            name = name_m.group(1)
            installdir = dir_m.group(1)

            low_name = name.lower()
            if any(ign in low_name for ign in IGNORED_APP_NAMES):
                return

            game_dir = os.path.join(steamapps_dir, "common", installdir)
            workshop_dir = os.path.join(steamapps_dir, "workshop", "content", appid)
            shadercache_dir = os.path.join(steamapps_dir, "shadercache", appid)

            info = {
                "appid": appid,
                "name": name,
                "safe_id": f"{appid}_{sanitize_name(name)}",
                "game_dir": game_dir,
                "workshop_dir": workshop_dir,
                "shadercache_dir": shadercache_dir
            }
            self.manifests[appid] = info
            self.installdir_to_appid[os.path.abspath(game_dir)] = appid
        except OSError:
            pass

    def detect_running_game(self):
        """
        Scans /proc for any active Steam game launched by the current user.
        Returns a game metadata dictionary if found, or None if no game is running.
        """
        uid = os.getuid()
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue

                cmdline_path = os.path.join(entry.path, "cmdline")
                with open(cmdline_path, "rb") as f:
                    raw_cmd = f.read().decode("utf-8", errors="ignore").replace("\x00", " ")

                if not raw_cmd:
                    continue

                # Ignore Steam helper/runtime wrapper processes unless they point to a game
                if "steamwebhelper" in raw_cmd or "preshader" in raw_cmd or "daemon.py" in raw_cmd:
                    continue

                # 1. Check for AppID in Steam reaper command line (e.g. reaper SteamLaunch AppId=221100)
                reaper_match = re.search(r"AppId=(\d+)", raw_cmd)
                if reaper_match:
                    appid = reaper_match.group(1)
                    if appid not in self.manifests:
                        self.refresh_libraries()
                    if appid in self.manifests:
                        return self.manifests[appid]

                # 2. Check /proc/<pid>/environ for SteamAppId when a game binary is active
                if "steamapps/common/" in raw_cmd or ".exe" in raw_cmd.lower():
                    env_path = os.path.join(entry.path, "environ")
                    with open(env_path, "rb") as f:
                        env_data = f.read().decode("utf-8", errors="ignore")

                    app_m = re.search(r"(?:^|\x00)SteamAppId=(\d+)", env_data)
                    if app_m:
                        appid = app_m.group(1)
                        if appid != "0":
                            if appid not in self.manifests:
                                self.refresh_libraries()
                            if appid in self.manifests:
                                return self.manifests[appid]

                            # Fallback if manifest wasn't parsed but install path is in environ
                            compat_m = re.search(
                                r"(?:^|\x00)STEAM_COMPAT_INSTALL_PATH=([^\x00]+)",
                                env_data
                            )
                            if compat_m:
                                gdir = compat_m.group(1)
                                gname = os.path.basename(gdir.rstrip("/"))
                                return {
                                    "appid": appid,
                                    "name": gname,
                                    "safe_id": f"{appid}_{sanitize_name(gname)}",
                                    "game_dir": gdir,
                                    "workshop_dir": "",
                                    "shadercache_dir": ""
                                }

                # 3. Direct path match inside steamapps/common/<GameFolder>
                common_m = re.search(r"(/[^\x00\"']+?/steamapps/common/([^/\"\x00]+))", raw_cmd)
                if common_m:
                    full_gdir = os.path.abspath(common_m.group(1))
                    folder_name = common_m.group(2)
                    low_folder = folder_name.lower()
                    if not any(ign in low_folder for ign in IGNORED_APP_NAMES):
                        appid = self.installdir_to_appid.get(full_gdir, "custom")
                        if appid in self.manifests:
                            return self.manifests[appid]
                        return {
                            "appid": appid,
                            "name": folder_name,
                            "safe_id": f"{appid}_{sanitize_name(folder_name)}",
                            "game_dir": full_gdir,
                            "workshop_dir": "",
                            "shadercache_dir": ""
                        }
            except (OSError, PermissionError):
                continue

        return None
