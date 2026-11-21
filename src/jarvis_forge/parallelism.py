"""Automatic native-build budgets, bounded by process and Linux cgroup limits."""

import math
import os
from pathlib import Path
import sys


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _quota_limits():
    """Read the current cgroup and its ancestors; never change host settings."""
    if sys.platform != "linux":
        return []
    membership = _read("/proc/self/cgroup") or ""
    mounts = _read("/proc/self/mountinfo") or ""
    groups = []
    for line in membership.splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3:
            groups.append((set(fields[1].split(",")), fields[2]))
    limits = []
    for line in mounts.splitlines():
        before, separator, after = line.partition(" - ")
        fields, options = before.split(), after.split()
        if not separator or len(fields) < 5 or len(options) < 3:
            continue
        kind = options[0]
        if kind not in {"cgroup", "cgroup2"}:
            continue
        controllers = set(options[2].split(","))
        root = Path(fields[3].replace(r"\040", " "))
        mount = Path(fields[4].replace(r"\040", " "))
        for selected, name in groups:
            if kind == "cgroup" and not ("cpu" in controllers and "cpu" in selected):
                continue
            if kind == "cgroup2" and selected != {""}:
                continue
            try:
                relative = Path(name).relative_to(root)
            except ValueError:
                # A container can expose only the root of its delegated subtree.
                relative = Path(".")
            directory = mount / relative
            if ".." in directory.parts:
                directory = mount
            while directory.is_relative_to(mount):
                try:
                    if kind == "cgroup2":
                        quota, period = (_read(directory / "cpu.max") or "max 1").split()
                    else:
                        quota = _read(directory / "cpu.cfs_quota_us") or "-1"
                        period = _read(directory / "cpu.cfs_period_us") or "0"
                    if quota not in {"max", "-1"} and int(quota) > 0 and int(period) > 0:
                        limits.append(max(1, math.ceil(int(quota) / int(period))))
                except (ValueError, TypeError):
                    pass
                if directory == mount:
                    break
                directory = directory.parent
    return limits


def available_cpus():
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        process_count = getattr(os, "process_cpu_count", os.cpu_count)
        count = process_count() or 1
    return max(1, min([count or 1, *_quota_limits()]))


def policy():
    cpus = available_cpus()
    return {"available_cpus": cpus, "build_jobs": min(cpus, 256),
            "concurrent_packages": 16, "cmake_jobs": min(cpus, 16),
            "homebrew_make_jobs": min(cpus, 16)}
