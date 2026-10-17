"""Disk-space floor and cache housekeeping.

The floor: new ingest, proxy and thumbnail work is refused (named error, nothing half-written) when the disk
holding its output has less than `VOLT_MIN_FREE_MB` free (default 2048). Health reports the same numbers.

The cache: only files that can be rebuilt are ever deleted. That is the thumbnail cache (`<out>/thumbs/**.jpg`,
capped by `VOLT_THUMB_CACHE_MB`, default 1024, 0 = no cap) and stale `.part.mp4` temp files left by a killed
process (older than `VOLT_PART_MAX_AGE_HOURS`, default 24). Registered essences (hi-res, proxy, publish
files) are never touched here, whatever their size.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("volt.disk")

DEFAULT_MIN_FREE_MB = 2048
DEFAULT_THUMB_CACHE_MB = 1024
DEFAULT_PART_MAX_AGE_HOURS = 24.0
FRESH_SECONDS = 60  # a file touched this recently is never evicted: someone may be reading it


class DiskLowError(Exception):
    """Not enough free space to start this work. `code` is stable for API and job error text."""

    code = "disk_low"

    def __init__(self, message: str, path=None, free: Optional[int] = None, floor: Optional[int] = None):
        super().__init__(message)
        self.path, self.free, self.floor = path, free, floor


def _number(env, name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, not {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def min_free_bytes(env=None) -> int:
    env = os.environ if env is None else env
    return int(_number(env, "VOLT_MIN_FREE_MB", DEFAULT_MIN_FREE_MB) * 1024 * 1024)


def thumb_cache_cap_bytes(env=None) -> int:
    env = os.environ if env is None else env
    return int(_number(env, "VOLT_THUMB_CACHE_MB", DEFAULT_THUMB_CACHE_MB) * 1024 * 1024)


def part_max_age_seconds(env=None) -> float:
    env = os.environ if env is None else env
    return _number(env, "VOLT_PART_MAX_AGE_HOURS", DEFAULT_PART_MAX_AGE_HOURS) * 3600


def free_bytes(path) -> Optional[int]:
    """Free bytes on the disk that holds `path` (or its nearest existing parent). None if it cannot be read."""
    probe = Path(path)
    for candidate in [probe, *probe.parents]:
        try:
            if candidate.exists():
                return shutil.disk_usage(candidate).free
        except OSError:
            return None
    return None


def require_space(path, what: str, need: int = 0, env=None) -> None:
    """Raise DiskLowError unless `need` bytes fit on `path`'s disk and still leave the floor free.
    A disk that cannot be measured does not block work."""
    floor = min_free_bytes(env)
    free = free_bytes(path)
    if free is None or floor <= 0 and need <= 0:
        return
    if free - max(need, 0) < floor:
        mb = 1024 * 1024
        extra = f" and {need // mb} MB needed" if need else ""
        raise DiskLowError(
            f"refusing {what}: {free // mb} MB free on the disk holding {path}{extra}, floor is {floor // mb} MB "
            f"(free space or lower VOLT_MIN_FREE_MB)", path=str(path), free=free, floor=floor)


def tree_size(root) -> int:
    """Total bytes of regular files under `root` (symlinks are not followed). 0 if it does not exist."""
    total = 0
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def prune_thumbs(out_dir, cap_bytes: Optional[int] = None, now: Optional[float] = None) -> dict:
    """Keep `<out_dir>/thumbs` under `cap_bytes` by deleting the least recently written .jpg files first.
    Stops at 90 percent of the cap so it does not run on every request. Returns what it did."""
    cap = thumb_cache_cap_bytes() if cap_bytes is None else cap_bytes
    root = Path(out_dir) / "thumbs"
    result = {"cap": cap, "before": 0, "after": 0, "removed": 0, "freed": 0}
    if not root.is_dir():
        return result
    now = time.time() if now is None else now
    files = []
    for dirpath, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            if not name.lower().endswith(".jpg"):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if os.path.islink(full):
                continue
            files.append((st.st_mtime, st.st_size, full))
    total = sum(size for _m, size, _p in files)
    result["before"] = result["after"] = total
    if cap <= 0 or total <= cap:
        return result
    target = int(cap * 0.9)
    for mtime, size, full in sorted(files):
        if total <= target:
            break
        if now - mtime < FRESH_SECONDS:
            continue
        try:
            os.unlink(full)
        except OSError:
            continue
        total -= size
        result["removed"] += 1
        result["freed"] += size
    result["after"] = total
    if result["removed"]:
        log.info("thumbnail cache pruned removed=%d freed_mb=%d now_mb=%d cap_mb=%d",
                 result["removed"], result["freed"] // (1024 * 1024), total // (1024 * 1024), cap // (1024 * 1024))
    return result


def sweep_stale_parts(directory, max_age: Optional[float] = None, now: Optional[float] = None) -> int:
    """Delete `*.part.mp4` temp files in `directory` (not below it) untouched for `max_age` seconds: what a
    killed encode leaves behind. Returns how many were removed."""
    root = Path(directory)
    if not root.is_dir():
        return 0
    age = part_max_age_seconds() if max_age is None else max_age
    now = time.time() if now is None else now
    removed = 0
    for entry in root.glob("*.part.mp4"):
        try:
            if entry.is_file() and not entry.is_symlink() and now - entry.stat().st_mtime >= age:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("stale temp files removed count=%d dir=%s", removed, root)
    return removed


_last_prune = {"at": 0.0}


def maybe_prune_thumbs(out_dir, every: float = 60.0) -> None:
    """Called on the request path: prune at most once per `every` seconds per process. Never raises."""
    now = time.monotonic()
    if now - _last_prune["at"] < every:
        return
    _last_prune["at"] = now
    try:
        prune_thumbs(out_dir)
    except Exception:  # noqa: BLE001 - housekeeping must never fail a request
        log.exception("thumbnail prune failed")
