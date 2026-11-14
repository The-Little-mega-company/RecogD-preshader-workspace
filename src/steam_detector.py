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
PRISM_APP_ID = "3546166962"


def sanitize_name(name):
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned or "UnknownGame"


class SteamGameDetector:
    def __init__(self, extra_mount="/mnt/preshader", proc_root="/proc"):
        self.extra_mount = extra_mount
        self.proc_root = proc_root
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

    def _extract_prism_instance_dir(self, raw_cmd, pid=None):
        mc_dir_m = re.search(
            r"--gameDir(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s\"']+))",
            raw_cmd
        )
        if mc_dir_m:
            game_dir = next(group for group in mc_dir_m.groups() if group)
            if os.path.isdir(game_dir):
                return os.path.abspath(game_dir)

        if pid is not None:
            try:
                process_cwd = os.readlink(os.path.join(self.proc_root, pid, "cwd"))
                if (
                    os.path.isdir(process_cwd)
                    and "/prismlauncher/instances/" in process_cwd.lower()
                ):
                    return os.path.abspath(process_cwd)
            except OSError:
                pass

        prism_m = re.search(r"(/[^\s\"']*PrismLauncher/instances/[^/\s\"']+)", raw_cmd)
        if prism_m and os.path.isdir(prism_m.group(1)):
            return os.path.abspath(prism_m.group(1))

        default_prism = os.path.expanduser("~/.local/share/PrismLauncher")
        if os.path.isdir(default_prism):
            return default_prism
        return os.path.expanduser("~")

    def _has_prism_ancestor(self, pid, max_depth=8):
        seen = set()
        current_pid = pid
        for _ in range(max_depth):
            stat_path = os.path.join(self.proc_root, current_pid, "stat")
            try:
                with open(stat_path, "r", encoding="utf-8") as stat_file:
                    stat_line = stat_file.read()
                stat_fields = stat_line.rsplit(")", 1)[1].split()
                parent_pid = stat_fields[1]
            except (OSError, IndexError):
                return False

            if parent_pid in seen or parent_pid == "0":
                return False
            seen.add(parent_pid)

            parent_cmdline = os.path.join(self.proc_root, parent_pid, "cmdline")
            try:
                with open(parent_cmdline, "rb") as cmd_file:
                    parent_cmd = cmd_file.read().decode(
                        "utf-8", errors="ignore"
                    ).replace("\x00", " ").lower()
            except OSError:
                return False

            if "prismlauncher" in parent_cmd or "multimc" in parent_cmd:
                return True
            if parent_pid == "1":
                return False
            current_pid = parent_pid
        return False

    def detect_running_game(self, appid=None):
        """
        Scans /proc for any active Steam game or PrismLauncher/Minecraft Java child process.
        Returns a game metadata dictionary if found, or None if no game is running.
        """
        uid = os.getuid()
        expected_appid = str(appid) if appid is not None else None
        with os.scandir(self.proc_root) as processes:
            process_entries = list(processes)
        for entry in process_entries:
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

                if "steamwebhelper" in raw_cmd or "preshader" in raw_cmd or "daemon.py" in raw_cmd:
                    continue

                low_cmd = raw_cmd.lower()
                executable = raw_cmd.split(" ", 1)[0]
                is_java_process = "java" in os.path.basename(executable).lower()
                has_minecraft_args = any(
                    marker in low_cmd for marker in (
                        "minecraft",
                        "fabric",
                        "forge",
                        "neoforge",
                        "quilt",
                        "--gamedir",
                        "/instances/"
                    )
                )
                is_prism_java = (
                    is_java_process
                    and has_minecraft_args
                    and (
                        "prismlauncher" in low_cmd
                        or "multimc" in low_cmd
                        or self._has_prism_ancestor(entry.name)
                    )
                )

                # Prism's launcher is a parent process; only its Minecraft JVM is the game.
                if is_prism_java:
                    detected_appid = PRISM_APP_ID

                    if expected_appid is not None and detected_appid != expected_appid:
                        continue
                    gdir = self._extract_prism_instance_dir(raw_cmd, entry.name)
                    prism_root = os.path.expanduser("~/.local/share/PrismLauncher")
                    prism_root_match = re.search(
                        r"(/[^\s\"']*/PrismLauncher)(?:/instances/|/libraries/|/assets/)",
                        raw_cmd,
                        re.IGNORECASE
                    )
                    if prism_root_match:
                        prism_root = prism_root_match.group(1)
                    elif not os.path.isdir(prism_root):
                        flatpak_root = os.path.expanduser(
                            "~/.var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher"
                        )
                        if os.path.isdir(flatpak_root):
                            prism_root = flatpak_root
                    info = {
                        "appid": detected_appid,
                        "name": "PrismLauncher",
                        "safe_id": f"{detected_appid}_PrismLauncher",
                        "game_dir": gdir,
                        "workshop_dir": os.path.join(prism_root, "libraries") if os.path.isdir(prism_root) else "",
                        "shadercache_dir": os.path.join(prism_root, "assets") if os.path.isdir(prism_root) else ""
                    }
                    return info

                # 1. Check for AppID in Steam reaper command line (e.g. reaper SteamLaunch AppId=221100)
                reaper_match = re.search(r"AppId=(\d+)", raw_cmd)
                if reaper_match:
                    detected_appid = reaper_match.group(1)
                    if expected_appid is not None and detected_appid != expected_appid:
                        continue
                    if detected_appid not in self.manifests:
                        self.refresh_libraries()
                    if detected_appid in self.manifests:
                        return self.manifests[detected_appid]

                # 2. Check /proc/<pid>/environ for SteamAppId or SteamGameId when a game binary is active
                if "steamapps/common/" in raw_cmd or ".exe" in low_cmd or is_java_process:
                    env_path = os.path.join(entry.path, "environ")
                    with open(env_path, "rb") as f:
                        env_data = f.read().decode("utf-8", errors="ignore")

                    app_m = re.search(r"(?:^|\x00)SteamAppId=(\d+)", env_data)
                    game_m = re.search(r"(?:^|\x00)SteamGameId=(\d+)", env_data)
                    appid = None
                    if app_m and app_m.group(1) != "0":
                        detected_appid = app_m.group(1)
                    elif game_m and game_m.group(1) != "0":
                        detected_appid = game_m.group(1)
                    else:
                        detected_appid = None

                    if detected_appid:
                        if expected_appid is not None and detected_appid != expected_appid:
                            continue
                        if detected_appid not in self.manifests:
                            self.refresh_libraries()
                        if detected_appid in self.manifests:
                            return self.manifests[detected_appid]

                        compat_m = re.search(
                            r"(?:^|\x00)STEAM_COMPAT_INSTALL_PATH=([^\x00]+)",
                            env_data
                        )
                        if compat_m:
                            gdir = compat_m.group(1)
                            gname = os.path.basename(gdir.rstrip("/"))
                            if gname and not gname.startswith("appid_"):
                                return {
                                    "appid": detected_appid,
                                    "name": gname,
                                    "safe_id": f"{detected_appid}_{sanitize_name(gname)}",
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
                        detected_appid = self.installdir_to_appid.get(full_gdir, "custom")
                        if expected_appid is not None and detected_appid != expected_appid:
                            continue
                        if detected_appid in self.manifests:
                            return self.manifests[detected_appid]
                        return {
                            "appid": detected_appid,
                            "name": folder_name,
                            "safe_id": f"{detected_appid}_{sanitize_name(folder_name)}",
                            "game_dir": full_gdir,
                            "workshop_dir": "",
                            "shadercache_dir": ""
                        }
            except (OSError, PermissionError):
                continue

        return None
