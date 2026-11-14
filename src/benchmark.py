#!/usr/bin/env python3
import argparse
import errno
import mmap
import os
import re
import tempfile
import time
from dataclasses import dataclass

import config_ipc


CHUNK_SIZE = 64 * 1024 * 1024
BLOCK_SIZE = 1024 * 1024
BENCHMARK_SECONDS = 2.0
WRITE_HEADROOM = 0.80


@dataclass
class DriveResult:
    label: str
    read_mbps: float = 0.0
    write_mbps: float = 0.0
    error: str = ""
    cleanup_warning: str = ""

    @property
    def successful(self):
        return not self.error


@dataclass
class BenchmarkReport:
    nvme: DriveResult
    external: DriveResult
    calibrated_write_mbps: float = 0.0
    calibration_error: str = ""


@dataclass(frozen=True)
class MountEntry:
    mountpoint: str
    filesystem: str


def _unescape_mount_field(value):
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value
    )


def _read_mount_entries():
    entries = []
    with open("/proc/self/mountinfo", "r", encoding="utf-8") as mountinfo:
        for line in mountinfo:
            fields = line.rstrip("\n").split()
            try:
                separator = fields.index("-")
                if separator >= 6 and separator + 1 < len(fields):
                    entries.append(MountEntry(
                        os.path.realpath(_unescape_mount_field(fields[4])),
                        fields[separator + 1]
                    ))
            except (ValueError, IndexError):
                continue
    return entries


def _find_external_mount():
    user = os.environ.get("USER", os.path.basename(os.path.expanduser("~")))
    candidates = (
        os.environ.get("PRESHADER_EXT_MOUNT", ""),
        "/mnt/preshader",
        f"/run/media/{user}/preshader",
        f"/media/{user}/preshader"
    )
    for candidate in candidates:
        if not candidate or not os.path.isdir(candidate):
            continue
        resolved = os.path.realpath(candidate)
        for entry in _read_mount_entries():
            if (
                entry.mountpoint == resolved
                and entry.filesystem != "autofs"
                and os.access(resolved, os.W_OK)
            ):
                return resolved
    return ""


def _write_test_chunk(path, chunk_size):
    fd, temp_path = tempfile.mkstemp(prefix=".preshader-bench-", suffix=".tmp", dir=path)
    try:
        try:
            os.posix_fallocate(fd, 0, chunk_size)
        except AttributeError:
            _fill_test_chunk(fd, chunk_size)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
                raise
            _fill_test_chunk(fd, chunk_size)
        os.fdatasync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    os.close(fd)
    return temp_path


def _fill_test_chunk(fd, chunk_size):
    os.ftruncate(fd, 0)
    block = bytes(BLOCK_SIZE)
    remaining = chunk_size
    while remaining:
        written = os.write(fd, block[:min(BLOCK_SIZE, remaining)])
        if written <= 0:
            raise OSError("could not prepare benchmark test file")
        remaining -= written


def _measure_phase(fd, buffer_view, chunk_size, duration_seconds, write):
    block_size = len(buffer_view)
    operation = os.pwritev if write else os.preadv
    total_bytes = 0
    offset = 0
    started = time.monotonic()
    deadline = started + duration_seconds
    while time.monotonic() < deadline:
        transferred = operation(fd, [buffer_view], offset)
        if transferred != block_size:
            kind = "write" if write else "read"
            raise OSError(
                f"short direct {kind} at offset {offset}: "
                f"{transferred} of {block_size} bytes"
            )
        total_bytes += transferred
        offset += block_size
        if offset + block_size > chunk_size:
            offset = 0
    if write:
        os.fdatasync(fd)
    elapsed = max(time.monotonic() - started, 1e-9)
    return total_bytes / elapsed / 1_000_000


def benchmark_drive(label, path, duration_seconds=BENCHMARK_SECONDS, chunk_size=CHUNK_SIZE):
    if duration_seconds <= 0:
        raise ValueError("benchmark duration must be positive")
    if chunk_size < BLOCK_SIZE or chunk_size % BLOCK_SIZE:
        raise ValueError("test chunk must be a positive multiple of 1 MiB")
    result = DriveResult(label=label)
    temp_path = ""
    try:
        os.makedirs(path, exist_ok=True)
        temp_path = _write_test_chunk(path, chunk_size)
        direct_flag = getattr(os, "O_DIRECT", None)
        if direct_flag is None or not hasattr(os, "preadv") or not hasattr(os, "pwritev"):
            raise OSError("O_DIRECT vectored I/O is unavailable on this system")

        buffer = mmap.mmap(-1, BLOCK_SIZE)
        buffer_view = memoryview(buffer)
        try:
            buffer_view[:] = os.urandom(BLOCK_SIZE)
            fd = os.open(temp_path, os.O_RDWR | direct_flag)
            try:
                phase_seconds = duration_seconds / 2
                result.write_mbps = _measure_phase(
                    fd, buffer_view, chunk_size, phase_seconds, write=True
                )
                result.read_mbps = _measure_phase(
                    fd, buffer_view, chunk_size, phase_seconds, write=False
                )
            finally:
                os.close(fd)
        finally:
            buffer_view.release()
            buffer.close()
    except OSError as exc:
        result.error = str(exc)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                result.cleanup_warning = f"Could not remove temporary file {temp_path}: {exc}"
    return result


def _calibrated_write_cap(write_mbps):
    return max(0.001, round(float(write_mbps) * WRITE_HEADROOM, 3))


def run_benchmark(duration_seconds=BENCHMARK_SECONDS, chunk_size=CHUNK_SIZE):
    nvme_path = os.path.join(os.path.expanduser("~/.cache"), "recogd", "benchmark")
    try:
        nvme = benchmark_drive("NVMe", nvme_path, duration_seconds, chunk_size)
    except OSError as exc:
        nvme = DriveResult("NVMe", error=str(exc))

    try:
        external_path = _find_external_mount()
    except OSError as exc:
        external_path = ""
        external = DriveResult("External SSD", error=f"Could not inspect mounts: {exc}")
    else:
        if external_path:
            external = benchmark_drive("External SSD", external_path, duration_seconds, chunk_size)
        else:
            external = DriveResult(
                "External SSD",
                error="The preshader SSD is not mounted as a writable filesystem"
            )

    report = BenchmarkReport(nvme=nvme, external=external)
    if nvme.successful and external.successful and external.write_mbps > 0:
        cap = _calibrated_write_cap(external.write_mbps)
        try:
            config_ipc.update_live_config({"max_write_mbps": cap}, save_persistent=True)
            persisted = config_ipc.read_persistent_config().get("max_write_mbps")
            if persisted != cap:
                raise OSError("the calibrated write cap could not be saved to config")
            report.calibrated_write_mbps = cap
        except OSError as exc:
            report.calibration_error = str(exc)
    return report


def _format_speed(result, field):
    return f"{getattr(result, field):.1f}" if result.successful else "—"


def format_gui_summary(report):
    nvme = (
        f"NVMe R/W {_format_speed(report.nvme, 'read_mbps')}/"
        f"{_format_speed(report.nvme, 'write_mbps')}"
    )
    external = (
        f"SSD R/W {_format_speed(report.external, 'read_mbps')}/"
        f"{_format_speed(report.external, 'write_mbps')} MB/s"
    )
    if report.nvme.successful and report.external.successful:
        combined = (
            f" | Dual bus {_format_speed_sum(report.nvme.read_mbps, report.external.read_mbps)}/"
            f"{_format_speed_sum(report.nvme.write_mbps, report.external.write_mbps)}"
        )
    else:
        combined = ""
    return f"{nvme} | {external}{combined}"


def _format_speed_sum(first, second):
    return f"{first + second:.1f}"


def print_report(report):
    print("Preshader Dual-Bus Storage Benchmark (MB/s)")
    print(f"{'Drive':<18} {'Read':>12} {'Write':>12}")
    print("-" * 44)
    for result in (report.nvme, report.external):
        if result.successful:
            print(f"{result.label:<18} {result.read_mbps:>12.1f} {result.write_mbps:>12.1f}")
        else:
            print(f"{result.label:<18} {'unavailable':>12} {'unavailable':>12}")
            print(f"  {result.error}")
        if result.cleanup_warning:
            print(f"  WARNING: {result.cleanup_warning}")
    if report.nvme.successful and report.external.successful:
        print("-" * 44)
        print(
            "Combined Dual-Bus bandwidth: "
            f"Read {_format_speed_sum(report.nvme.read_mbps, report.external.read_mbps)} MB/s | "
            f"Write {_format_speed_sum(report.nvme.write_mbps, report.external.write_mbps)} MB/s"
        )
    if report.calibrated_write_mbps:
        print(
            f"Background write cap set to {report.calibrated_write_mbps:.3f} MB/s "
            "(80% of external SSD write throughput)."
        )
    if report.calibration_error:
        print(f"WARNING: Write-cap calibration was not saved: {report.calibration_error}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark the NVMe and external preshader SSD")
    parser.parse_args()
    report = run_benchmark()
    print_report(report)
    return int(
        not report.nvme.successful
        or not report.external.successful
        or bool(report.calibration_error)
    )


if __name__ == "__main__":
    raise SystemExit(main())
