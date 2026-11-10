"""Conservative CPU limits for one local or Slurm coordinator process."""

import math
import os
from pathlib import Path


def _positive_int(value: str | None) -> int | None:
    try:
        number = int(value) if value is not None else 0
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def _cgroup_directories() -> list[tuple[Path, str]]:
    """Find the process cgroup and its ancestors (parent quotas also apply)."""
    try:
        lines = Path("/proc/self/cgroup").read_text().splitlines()
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return []
    directories = []
    for line in lines:
        try:
            _, controllers, relative = line.split(":", 2)
        except ValueError:
            continue
        if controllers and "cpu" not in controllers.split(","):
            continue
        version = "v2" if not controllers else "v1"
        for mount in mounts:
            try:
                before, after = mount.split(" - ", 1)
                fields, details = before.split(), after.split()
                filesystem = details[0]
                if filesystem != ("cgroup2" if version == "v2" else "cgroup"):
                    continue
                if version == "v1" and "cpu" not in details[2].split(","):
                    continue
                mount_root, mountpoint = Path(fields[3]), Path(fields[4])
                group = Path(relative)
                suffix = group.relative_to(mount_root)
            except (ValueError, IndexError):
                continue
            root = mountpoint
            path = root / suffix
            while path == root or root in path.parents:
                directories.append((path, version))
                if path == root:
                    break
                path = path.parent
    return directories


def _cgroup_quota() -> int | None:
    quotas = []
    for directory, version in _cgroup_directories():
        try:
            if version == "v2":
                quota, period = (directory / "cpu.max").read_text().split()[:2]
                if quota == "max":
                    continue
            else:
                quota = (directory / "cpu.cfs_quota_us").read_text().strip()
                period = (directory / "cpu.cfs_period_us").read_text().strip()
            numerator, denominator = int(quota), int(period)
            if numerator > 0 and denominator > 0:
                quotas.append(max(1, math.floor(numerator / denominator)))
        except (OSError, ValueError, IndexError):
            continue
    return min(quotas) if quotas else None


def cpu_limits() -> dict[str, int | None]:
    """Return independently observed limits; never infer a Slurm grant from node size."""
    slurm = _positive_int(os.environ.get("SLURM_CPUS_PER_TASK"))
    affinity = None
    if hasattr(os, "sched_getaffinity"):
        try:
            affinity = len(os.sched_getaffinity(0)) or None
        except OSError:
            pass
    quota = _cgroup_quota()
    # os.cpu_count is a useful local fallback, but does not establish a Slurm allocation.
    in_slurm = bool(os.environ.get("SLURM_JOB_ID"))
    fallback = None if in_slurm else (os.cpu_count() or 1)
    limits = {"slurm_cpus_per_task": slurm, "cpu_affinity": affinity,
              "cgroup_cpu_quota": quota, "local_cpu_count": fallback,
              "slurm_unreported_fallback": 1 if in_slurm and slurm is None else None}
    known = [value for value in limits.values() if value is not None]
    limits["available_cpus"] = min(known) if known else 1
    return limits
