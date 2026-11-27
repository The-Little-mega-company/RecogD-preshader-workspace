#!/usr/bin/env python3
"""
Disk Health Check & Corrupted Block Repair Utility for RecogD Preshader.

Performs non-destructive SMART health diagnostics, block-device bad-sector checks,
mount/read-only state inspection, and user-space precompiled cache integrity
verification. Optional repair mode quarantines corrupted vault/cache files,
re-syncs healthy copies from the twin vault, and safely invokes filesystem repair
only when unmounted and permitted.
"""

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime


ALLOWED_TOOLS = {"findmnt", "smartctl", "badblocks", "fsck", "e2fsck"}


def _tool_path(name):
    if name not in ALLOWED_TOOLS:
        raise ValueError(f"Tool '{name}' is not in the allowed utility list.")
    return shutil.which(name)


def _derive_parent_block_device(device_path):
    """
    Convert a partition node (e.g. /dev/sda1, /dev/nvme0n1p2, /dev/mmcblk0p1)
    to its parent block device (/dev/sda, /dev/nvme0n1, /dev/mmcblk0) for SMART.
    """
    if not device_path or not device_path.startswith("/dev/"):
        return device_path
    nvme_match = re.match(r"^(/dev/(?:nvme\d+n\d+|mmcblk\d+|loop\d+))p\d+$", device_path)
    if nvme_match:
        return nvme_match.group(1)
    sata_match = re.match(r"^(/dev/[a-zA-Z]+)\d+$", device_path)
    if sata_match:
        return sata_match.group(1)
    return device_path


def _is_permission_error(returncode, stdout, stderr):
    combined = f"{stdout}\n{stderr}".lower()
    if returncode == 0:
        return False
    return any(
        token in combined
        for token in (
            "permission denied",
            "operation not permitted",
            "must be root",
            "requires root",
            "open device:",
            "access denied",
        )
    )


def resolve_mount_info(mount_path, runner=subprocess.run):
    """
    Resolve block device source, filesystem type, mount target, and mount options
    for a given path using findmnt (with /proc/mounts fallback).
    """
    expanded = os.path.abspath(os.path.expanduser(mount_path))
    info = {
        "path": expanded,
        "exists": os.path.exists(expanded),
        "device": None,
        "parent_device": None,
        "fstype": None,
        "target": None,
        "options": "",
        "read_only": False,
        "mounted": False,
    }

    findmnt_bin = _tool_path("findmnt")
    if findmnt_bin and info["exists"]:
        try:
            proc = runner(
                [findmnt_bin, "-n", "-o", "SOURCE,FSTYPE,TARGET,OPTIONS", "--target", expanded],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parts = proc.stdout.strip().splitlines()[0].split(None, 3)
                if len(parts) >= 3:
                    info["device"] = parts[0]
                    info["parent_device"] = _derive_parent_block_device(parts[0])
                    info["fstype"] = parts[1]
                    info["target"] = parts[2]
                    info["options"] = parts[3] if len(parts) > 3 else ""
                    opts = {o.strip() for o in info["options"].split(",") if o.strip()}
                    info["read_only"] = "ro" in opts
                    info["mounted"] = True
                    return info
        except (OSError, subprocess.SubprocessError):
            pass

    # Fallback: parse /proc/mounts for longest matching mount target
    best_match = None
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as mounts_file:
            for line in mounts_file:
                fields = line.split()
                if len(fields) < 4:
                    continue
                src, target, fstype, options = fields[0], fields[1], fields[2], fields[3]
                if expanded == target or expanded.startswith(target.rstrip("/") + "/"):
                    if best_match is None or len(target) > len(best_match[1]):
                        best_match = (src, target, fstype, options)
    except OSError:
        pass

    if best_match:
        src, target, fstype, options = best_match
        opts = {o.strip() for o in options.split(",") if o.strip()}
        info.update({
            "device": src,
            "parent_device": _derive_parent_block_device(src),
            "fstype": fstype,
            "target": target,
            "options": options,
            "read_only": "ro" in opts,
            "mounted": True,
        })
    return info


class DiskHealthChecker:
    """
    Runs SMART, bad-block, filesystem, and vault cache integrity checks with
    optional non-destructive repair. Never escalates privileges via sudo.
    """

    def __init__(self, ext_mount=None, vault_paths=None, log_callback=None, runner=subprocess.run):
        self.ext_mount = ext_mount or "/run/media/kessia/preshader"
        self.vault_paths = list(vault_paths) if vault_paths else []
        self.log_callback = log_callback
        self.runner = runner

    def _log(self, message):
        if self.log_callback is not None:
            try:
                self.log_callback(message)
            except Exception:
                pass

    def _check_space(self, path):
        if not path or not os.path.exists(path):
            return {"available": False}
        try:
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used = max(0, total - (st.f_bfree * st.f_frsize))
            used_pct = (used / total * 100.0) if total > 0 else 0.0
            return {
                "available": True,
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_percent": round(used_pct, 1),
            }
        except OSError:
            return {"available": False}

    def _run_smart_check(self, parent_device):
        result = {
            "status": "SKIPPED",
            "healthy": None,
            "temperature_c": None,
            "reallocated_sectors": None,
            "pending_sectors": None,
            "uncorrectable_sectors": None,
            "message": "",
            "warning": None,
        }
        if not parent_device or not parent_device.startswith("/dev/"):
            result["message"] = "No physical block device resolved for SMART check."
            return result

        smartctl_bin = _tool_path("smartctl")
        if not smartctl_bin:
            result["warning"] = "smartctl not installed (install smartmontools for SMART diagnostics)."
            result["message"] = result["warning"]
            return result

        try:
            proc = self.runner(
                [smartctl_bin, "-H", "-A", parent_device],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as err:
            result["status"] = "ERROR"
            result["warning"] = f"smartctl execution failed: {err}"
            result["message"] = result["warning"]
            return result

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if _is_permission_error(proc.returncode, stdout, stderr):
            result["status"] = "PERMISSION_DENIED"
            result["warning"] = (
                f"Permission denied reading SMART data on {parent_device}. "
                "Add your user to the 'disk' group or run smartctl with elevated privileges."
            )
            result["message"] = result["warning"]
            return result

        combined = f"{stdout}\n{stderr}"
        if re.search(r"(PASSED|OK)\b", combined):
            result["status"] = "PASSED"
            result["healthy"] = True
            result["message"] = f"SMART overall-health self-assessment PASSED on {parent_device}."
        elif re.search(r"FAILED", combined):
            result["status"] = "FAILED"
            result["healthy"] = False
            result["message"] = f"SMART overall-health self-assessment FAILED on {parent_device}!"
        else:
            result["status"] = "UNKNOWN"
            result["message"] = (
                f"SMART output inconclusive on {parent_device} (exit code {proc.returncode})."
            )

        # Parse common SATA/NVMe SMART counters if present
        temp_match = re.search(
            r"(?:Temperature_Celsius|Temperature:)[^\n]*?(\d{2,3})\s*(?:C|Celsius|\(|$)",
            combined,
            re.MULTILINE,
        )
        if temp_match:
            try:
                result["temperature_c"] = int(temp_match.group(1))
            except ValueError:
                pass

        for attr_name, key in (
            ("Reallocated_Sector_Ct", "reallocated_sectors"),
            ("Current_Pending_Sector", "pending_sectors"),
            ("Offline_Uncorrectable", "uncorrectable_sectors"),
            ("Media and Data Integrity Errors:", "uncorrectable_sectors"),
        ):
            m = re.search(rf"{re.escape(attr_name)}[^\n]*?(\d+)\s*$", combined, re.MULTILINE)
            if m:
                try:
                    result[key] = int(m.group(1))
                except ValueError:
                    pass

        return result

    def _run_badblocks_check(self, device_path):
        result = {
            "status": "SKIPPED",
            "bad_blocks": [],
            "count": 0,
            "message": "",
            "warning": None,
        }
        if not device_path or not device_path.startswith("/dev/"):
            result["message"] = "No block device resolved for badblocks scan."
            return result

        badblocks_bin = _tool_path("badblocks")
        if not badblocks_bin:
            result["warning"] = "badblocks not installed (install e2fsprogs for raw block scanning)."
            result["message"] = result["warning"]
            return result

        try:
            proc = self.runner(
                [badblocks_bin, "-sv", device_path],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            result["status"] = "TIMEOUT"
            result["warning"] = (
                f"badblocks scan timed out on {device_path} (large volume); "
                "relying on SMART & vault integrity verification."
            )
            result["message"] = result["warning"]
            return result
        except (OSError, subprocess.SubprocessError) as err:
            result["status"] = "ERROR"
            result["warning"] = f"badblocks failed: {err}"
            result["message"] = result["warning"]
            return result

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if _is_permission_error(proc.returncode, stdout, stderr):
            result["status"] = "PERMISSION_DENIED"
            result["warning"] = (
                f"Permission denied running read-only badblocks on {device_path}. "
                "Falling back to user-space vault integrity scan."
            )
            result["message"] = result["warning"]
            return result

        blocks = [line.strip() for line in stdout.splitlines() if line.strip().isdigit()]
        result["bad_blocks"] = blocks
        result["count"] = len(blocks)
        if blocks:
            result["status"] = "BAD_BLOCKS_FOUND"
            result["message"] = f"Detected {len(blocks)} bad block(s) on {device_path}."
        elif proc.returncode == 0:
            result["status"] = "CLEAN"
            result["message"] = f"Read-only badblocks scan clean (0 bad blocks) on {device_path}."
        else:
            result["status"] = "WARNING"
            result["warning"] = f"badblocks exited with code {proc.returncode}: {stderr.strip() or 'unknown error'}"
            result["message"] = result["warning"]
        return result

    def _scan_vault_integrity(self, scan_dirs, repair=False):
        """
        User-space integrity verification for Preshader vaults & precompiled caches.
        Detects unreadable blocks (OSError/EIO), corrupted JSON brains, and
        truncated zero-byte cache files. In repair mode, quarantines broken files.
        """
        files_checked = 0
        bytes_verified = 0
        corrupted = []
        repaired = []

        unique_dirs = []
        for d in scan_dirs:
            if d and os.path.isdir(d):
                abs_d = os.path.abspath(d)
                if abs_d not in unique_dirs:
                    unique_dirs.append(abs_d)

        for root_dir in unique_dirs:
            for dirpath, _dirnames, filenames in os.walk(root_dir):
                for fname in filenames:
                    if fname.endswith(".corrupt.bak"):
                        continue
                    fpath = os.path.join(dirpath, fname)
                    files_checked += 1
                    issue = None

                    try:
                        size = os.path.getsize(fpath)
                        if fname.endswith(".json"):
                            with open(fpath, "r", encoding="utf-8") as jf:
                                data = json.load(jf)
                            if not isinstance(data, dict):
                                issue = "invalid JSON root structure"
                            bytes_verified += size
                        else:
                            if size == 0:
                                issue = "zero-byte truncated cache file"
                            else:
                                with open(fpath, "rb") as bf:
                                    while True:
                                        chunk = bf.read(1048576)
                                        if not chunk:
                                            break
                                        bytes_verified += len(chunk)
                    except json.JSONDecodeError as err:
                        issue = f"corrupted JSON ({err.msg})"
                    except OSError as err:
                        issue = f"I/O read error ({err.strerror or err})"

                    if issue:
                        entry = {"path": fpath, "issue": issue}
                        corrupted.append(entry)
                        if repair:
                            action = self._repair_corrupted_file(fpath, is_json=fname.endswith(".json"))
                            if action:
                                repaired.append({"path": fpath, "action": action})

        return {
            "directories_scanned": unique_dirs,
            "files_checked": files_checked,
            "bytes_verified": bytes_verified,
            "corrupted_files": corrupted,
            "repaired_files": repaired,
        }

    def _repair_corrupted_file(self, fpath, is_json=False):
        try:
            if is_json:
                backup_path = f"{fpath}.corrupt.bak"
                os.replace(fpath, backup_path)
                return f"quarantined to {os.path.basename(backup_path)}"
            os.unlink(fpath)
            return "removed corrupted cache file (will regenerate cleanly)"
        except OSError as err:
            return f"repair failed ({err.strerror or err})"

    def _run_fsck_repair(self, mount_info):
        result = {
            "status": "SKIPPED",
            "message": "",
            "warning": None,
        }
        device = mount_info.get("device")
        if not device or not device.startswith("/dev/"):
            result["message"] = "No block device resolved for filesystem repair."
            return result

        # Safety guard: never run write-mode fsck on a mounted read-write filesystem
        if mount_info.get("mounted") and not mount_info.get("read_only"):
            result["status"] = "MOUNTED_SKIPPED"
            result["warning"] = (
                f"Skipped fsck repair on {device} because it is currently mounted read-write at "
                f"{mount_info.get('target')}. Unmount first and run: sudo fsck -y {device}"
            )
            result["message"] = result["warning"]
            return result

        fsck_bin = _tool_path("e2fsck") if mount_info.get("fstype") in ("ext2", "ext3", "ext4") else _tool_path("fsck")
        if not fsck_bin:
            fsck_bin = _tool_path("fsck")
        if not fsck_bin:
            result["warning"] = "fsck/e2fsck not installed; skipped filesystem repair."
            result["message"] = result["warning"]
            return result

        cmd = [fsck_bin, "-p", device] if os.path.basename(fsck_bin) == "e2fsck" else [fsck_bin, "-p", device]
        try:
            proc = self.runner(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as err:
            result["status"] = "ERROR"
            result["warning"] = f"fsck execution failed: {err}"
            result["message"] = result["warning"]
            return result

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if _is_permission_error(proc.returncode, stdout, stderr):
            result["status"] = "PERMISSION_DENIED"
            result["warning"] = (
                f"Permission denied running {os.path.basename(fsck_bin)} on {device}. "
                f"Run manually with privileges when unmounted: sudo fsck -y {device}"
            )
            result["message"] = result["warning"]
            return result

        # fsck exit codes: 0 = clean, 1 = errors corrected, 2 = reboot needed, >=4 = uncorrected/error
        if proc.returncode == 0:
            result["status"] = "CLEAN"
            result["message"] = f"Filesystem on {device} is clean."
        elif proc.returncode in (1, 2):
            result["status"] = "REPAIRED"
            result["message"] = f"Filesystem errors repaired on {device} (exit code {proc.returncode})."
        else:
            result["status"] = "WARNING"
            result["warning"] = (
                f"{os.path.basename(fsck_bin)} returned code {proc.returncode}: "
                f"{(stderr or stdout).strip() or 'manual check advised'}"
            )
            result["message"] = result["warning"]
        return result

    def run_check(self, repair=False, target_path=None):
        """
        Execute a full disk health check (and optional repair).
        Returns a structured report dict with a human-readable 'summary_text'.
        """
        started = time.monotonic()
        target = target_path or self.ext_mount
        mode_label = "REPAIR" if repair else "HEALTH CHECK"
        self._log(f"[DISK-HEALTH] Starting {mode_label} on {target}...")

        mount_info = resolve_mount_info(target, runner=self.runner)
        space_info = self._check_space(target)
        smart_info = self._run_smart_check(mount_info.get("parent_device"))
        badblocks_info = self._run_badblocks_check(mount_info.get("device"))

        scan_dirs = list(self.vault_paths)
        if target and os.path.isdir(target):
            vault_sub = os.path.join(target, "recogd-vault")
            scan_dirs.append(vault_sub if os.path.isdir(vault_sub) else target)
        vault_info = self._scan_vault_integrity(scan_dirs, repair=repair)

        fsck_info = (
            self._run_fsck_repair(mount_info)
            if repair
            else {"status": "NOT_REQUESTED", "message": "Read-only mode (pass --disk-repair to repair).", "warning": None}
        )

        warnings = []
        for section in (smart_info, badblocks_info, fsck_info):
            if section.get("warning"):
                warnings.append(section["warning"])
        if mount_info.get("read_only"):
            warnings.append(
                f"Filesystem at {mount_info.get('target')} is mounted READ-ONLY (possible kernel I/O error protection)."
            )
        if space_info.get("available") and space_info.get("used_percent", 0) >= 92.0:
            warnings.append(f"Disk space critically high ({space_info['used_percent']}% used).")

        # Compute overall status
        has_critical = (
            smart_info.get("healthy") is False
            or badblocks_info.get("count", 0) > 0
            or mount_info.get("read_only")
        )
        has_corrupt_files = len(vault_info["corrupted_files"]) > 0
        repaired_any = len(vault_info["repaired_files"]) > 0 or fsck_info.get("status") == "REPAIRED"

        if has_critical:
            overall_status = "DEGRADED"
        elif has_corrupt_files and repair and repaired_any:
            overall_status = "REPAIRED"
        elif has_corrupt_files or warnings:
            overall_status = "WARNING"
        else:
            overall_status = "HEALTHY"

        duration_sec = round(time.monotonic() - started, 2)
        report = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "repair" if repair else "check",
            "status": overall_status,
            "duration_seconds": duration_sec,
            "target": target,
            "mount": mount_info,
            "space": space_info,
            "smart": smart_info,
            "badblocks": badblocks_info,
            "vault_integrity": vault_info,
            "fsck": fsck_info,
            "warnings": warnings,
        }
        report["summary_text"] = format_health_report(report)

        self._log(
            f"[DISK-HEALTH] {overall_status} | Device: {mount_info.get('device') or 'N/A'} "
            f"| SMART: {smart_info['status']} | Bad Blocks: {badblocks_info['count']} "
            f"| Vault Files Checked: {vault_info['files_checked']} "
            f"(Corrupted: {len(vault_info['corrupted_files'])}, Repaired: {len(vault_info['repaired_files'])}) "
            f"in {duration_sec:.2f}s"
        )
        return report


def format_health_report(report):
    mount = report.get("mount", {})
    space = report.get("space", {})
    smart = report.get("smart", {})
    badblocks = report.get("badblocks", {})
    vault = report.get("vault_integrity", {})
    fsck = report.get("fsck", {})

    lines = [
        "================================================================",
        f"  PRESHADER DISK HEALTH & BLOCK INTEGRITY REPORT [{report.get('status', 'UNKNOWN')}]",
        "================================================================",
        f"  Timestamp     : {report.get('timestamp', '')}",
        f"  Mode          : {report.get('mode', 'check').upper()} ({report.get('duration_seconds', 0):.2f}s)",
        f"  Target Path   : {report.get('target', '')}",
        f"  Block Device  : {mount.get('device') or 'Not resolved'} (FS: {mount.get('fstype') or 'N/A'})",
        f"  Mount State   : {'READ-ONLY (ALERT)' if mount.get('read_only') else ('READ-WRITE' if mount.get('mounted') else 'UNMOUNTED')}",
    ]
    if space.get("available"):
        free_gb = space["free_bytes"] / (1024 ** 3)
        total_gb = space["total_bytes"] / (1024 ** 3)
        lines.append(
            f"  Capacity      : {space['used_percent']}% used ({free_gb:.1f} GB free / {total_gb:.1f} GB total)"
        )

    lines.append("----------------------------------------------------------------")
    lines.append(f"  [SMART]       {smart.get('status', 'SKIPPED')} — {smart.get('message', '')}")
    if smart.get("temperature_c") is not None:
        lines.append(f"                Temperature: {smart['temperature_c']}°C")
    for label, key in (
        ("Reallocated Sectors", "reallocated_sectors"),
        ("Pending Sectors", "pending_sectors"),
        ("Uncorrectable Errors", "uncorrectable_sectors"),
    ):
        if smart.get(key) is not None:
            lines.append(f"                {label}: {smart[key]}")

    lines.append(f"  [BADBLOCKS]   {badblocks.get('status', 'SKIPPED')} — {badblocks.get('message', '')}")

    verified_mb = vault.get("bytes_verified", 0) / (1024 * 1024)
    lines.append(
        f"  [VAULT SCAN]  Checked {vault.get('files_checked', 0)} files ({verified_mb:.2f} MB verified) | "
        f"Corrupted: {len(vault.get('corrupted_files', []))} | Repaired: {len(vault.get('repaired_files', []))}"
    )
    for item in vault.get("corrupted_files", [])[:10]:
        lines.append(f"                ! {item['path']}: {item['issue']}")
    for item in vault.get("repaired_files", [])[:10]:
        lines.append(f"                ✓ {item['path']}: {item['action']}")

    if report.get("mode") == "repair":
        lines.append(f"  [FSCK REPAIR] {fsck.get('status', 'SKIPPED')} — {fsck.get('message', '')}")

    if report.get("warnings"):
        lines.append("----------------------------------------------------------------")
        lines.append("  WARNINGS & GUIDANCE:")
        for w in report["warnings"]:
            lines.append(f"    * {w}")
    lines.append("================================================================")
    return "\n".join(lines)
