"""Lightweight host-resource probes (RAM / disk), no external dependencies.

Used by the downloader's resource guard to decide whether to run, degrade, or
abort. RAM is read from ``/proc/meminfo`` (Linux/VPS); on platforms without it
(e.g. macOS dev) the RAM probe returns ``None`` and callers treat that as
"unknown" — i.e. they don't block on it.
"""
import shutil

_MB = 1024 * 1024


def free_disk_mb(path):
    """Free disk space (MB) for the filesystem holding ``path``, or None."""
    try:
        return shutil.disk_usage(path).free / _MB
    except OSError:
        return None


def available_ram_mb():
    """Available RAM (MB) from /proc/meminfo MemAvailable, or None if unknown."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except (OSError, ValueError):
        return None
    return None


def snapshot(path):
    """Return (free_disk_mb, available_ram_mb) for ``path``."""
    return free_disk_mb(path), available_ram_mb()
