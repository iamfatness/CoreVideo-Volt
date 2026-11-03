"""Request, pool, take and job metrics, in Prometheus text format, with no dependency.

`health.prometheus` renders gauges built per scrape from the database and the disk. This module adds the series that only a running
process can know: how many requests, how slow, how many failed, how long a job waited and ran, how busy the Postgres pool is.
Everything is in memory and per process; Prometheus sums the nodes.

Cardinality is bounded by construction: the route label is one of `ROUTE_CLASSES` (about eight), never a path, an id, a user or a
tenant. A registry also refuses new label sets past `MAX_SERIES`, counting what it dropped.

Nothing here writes to the database or is on the take / go / hold / release / skip / kill path except a counter increment and a
histogram observation after the response is built; a failure in this module is swallowed and never changes a response."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
MAX_SERIES = 1000

ROUTE_CLASSES = ("state", "read", "write", "heavy", "health", "auth", "static", "media")
# POST handlers that start long or expensive work: they get their own budget (rate limits use the same set).
HEAVY_ROUTES = frozenset({"act_detect", "act_export_nle", "act_ingest", "act_publish", "act_asset_archive"})
_HEALTH_PATHS = frozenset({"/health", "/ready", "/api/health", "/metrics"})
_NAME = re.compile(r"^volt_[a-z][a-z0-9_]*$")
_LABEL_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

# The series this module declares. Dashboards and alert rules may name only these and the ones health.prometheus renders;
# tests/test_telemetry.py enforces that.
HTTP_REQUESTS = "volt_http_requests_total"
HTTP_SECONDS = "volt_http_request_seconds"
HTTP_INFLIGHT = "volt_http_inflight"
DB_CHECKOUT_SECONDS = "volt_db_checkout_seconds"
DB_POOL_IN_USE = "volt_db_pool_in_use"
DB_POOL_WAITING = "volt_db_pool_waiting"
TAKE_SECONDS = "volt_take_seconds"
JOB_SECONDS = "volt_job_seconds"
JOB_QUEUE_WAIT_SECONDS = "volt_job_queue_wait_seconds"
RUNNER_CLAIMS = "volt_runner_claims_total"
BACKUP_AGE = "volt_backup_last_age_seconds"
DRILL_AGE = "volt_restore_drill_last_age_seconds"
DROPPED = "volt_telemetry_series_dropped_total"
RATE_LIMITED = "volt_rate_limited_total"
BUSY = "volt_busy_total"


def route_class(method: str, fn_name: Optional[str], path: str) -> str:
    """Which of the fixed route classes a request belongs to. Never returns a path."""
    if method in ("GET", "HEAD"):
        if path in _HEALTH_PATHS:
            return "health"
        if path == "/api/state":
            return "state"
        if path == "/auth" or path.startswith("/auth/"):
            return "auth"
        if path in ("/", "/operator", "/prompter") or path.startswith("/operator/"):
            return "static"
        if path == "/media" or path.endswith(("/proxy", "/thumb", "/prompter")):
            return "media"
        return "read"
    if method == "POST":
        if fn_name in HEAVY_ROUTES:
            return "heavy"
        return "write"
    return "read"


def status_class(status: int) -> str:
    return f"{status // 100}xx"


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(value) -> str:
    if isinstance(value, float):
        return repr(round(value, 6)) if value != int(value) else str(int(value))
    return str(value)


def _tag(labels: tuple, extra: Iterable = ()) -> str:
    pairs = list(labels) + list(extra)
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in pairs) + "}" if pairs else ""


class _Null:
    def inc(self, n=1):
        pass

    def observe(self, value):
        pass


class _Counter:
    def __init__(self, lock):
        self._lock, self.value = lock, 0

    def inc(self, n=1):
        with self._lock:
            self.value += n


class _Histogram:
    def __init__(self, lock, buckets):
        self._lock, self.buckets = lock, tuple(buckets)
        self.counts = [0] * len(self.buckets)   # per-bucket (not cumulative); the +Inf bucket is `count`
        self.sum, self.count = 0.0, 0

    def observe(self, value):
        with self._lock:
            self.sum += value
            self.count += 1
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    self.counts[i] += 1
                    break


class _Family:
    def __init__(self, kind: str, help_text: str, buckets=None):
        self.kind, self.help, self.buckets = kind, help_text, tuple(buckets or DEFAULT_BUCKETS)
        self.series: dict = {}


class Registry:
    def __init__(self, max_series: int = MAX_SERIES):
        self._lock = threading.RLock()
        self._families: dict = {}
        self._gauges: dict = {}
        self.max_series = max_series

    # -- declaring and recording
    def declare(self, name: str, kind: str, help_text: str = "", buckets=None) -> None:
        if not _NAME.match(name):
            raise ValueError(f"metric names start with volt_ and use a-z, 0-9 and _: {name!r}")
        if kind not in ("counter", "histogram", "gauge"):
            raise ValueError(f"unknown metric kind {kind!r}")
        with self._lock:
            if kind != "gauge":
                self._families.setdefault(name, _Family(kind, help_text, buckets))
            else:
                self._gauges.setdefault(name, [help_text, None])

    def _series(self, name: str, kind: str, labels, make):
        key = tuple(sorted((labels or {}).items()))
        for k, _ in key:
            if not _LABEL_NAME.match(k):
                raise ValueError(f"bad label name {k!r}")
        with self._lock:
            fam = self._families.get(name)
            if fam is None:
                fam = self._families[name] = _Family(kind, "")
            if fam.kind != kind:
                raise ValueError(f"{name} is a {fam.kind}, not a {kind}")
            found = fam.series.get(key)
            if found is not None:
                return found
            if sum(len(f.series) for f in self._families.values()) >= self.max_series:
                dropped = self._families.get(DROPPED)
                if dropped is not None and name != DROPPED:
                    dropped.series.setdefault((), _Counter(self._lock)).inc()
                return _Null()
            found = fam.series[key] = make(fam)
            return found

    def counter(self, name: str, labels: Optional[dict] = None):
        return self._series(name, "counter", labels, lambda fam: _Counter(self._lock))

    def histogram(self, name: str, labels: Optional[dict] = None, buckets=None):
        if buckets is not None:
            with self._lock:
                fam = self._families.get(name)
                if fam is None:
                    self._families[name] = _Family("histogram", "", buckets)
        return self._series(name, "histogram", labels, lambda fam: _Histogram(self._lock, fam.buckets))

    def gauge_fn(self, name: str, fn: Callable, help_text: str = "") -> None:
        """`fn()` returns a number, a list of ({labels}, number), or None (no sample). Called at scrape time; an error skips it."""
        if not _NAME.match(name):
            raise ValueError(f"metric names start with volt_ and use a-z, 0-9 and _: {name!r}")
        with self._lock:
            old = self._gauges.get(name)
            self._gauges[name] = [help_text or (old[0] if old else ""), fn]

    # -- reading
    def names(self) -> set:
        with self._lock:
            return set(self._families) | set(self._gauges)

    def series_count(self) -> int:
        with self._lock:
            return sum(len(f.series) for f in self._families.values())

    def render(self) -> str:
        lines: list = []
        with self._lock:
            families = {n: f for n, f in self._families.items()}
            snapshot = {n: (f, list(f.series.items())) for n, f in families.items()}
            gauges = {n: tuple(g) for n, g in self._gauges.items()}
        for name in sorted(set(snapshot) | set(gauges)):
            if name in snapshot:
                fam, items = snapshot[name]
                lines.append(f"# HELP {name} {fam.help or name}")
                lines.append(f"# TYPE {name} {fam.kind}")
                for key, s in sorted(items, key=lambda kv: kv[0]):
                    if fam.kind == "counter":
                        lines.append(f"{name}{_tag(key)} {_fmt(s.value)}")
                    else:
                        with self._lock:
                            counts, total, count = list(s.counts), s.sum, s.count
                        running = 0
                        for bound, n in zip(s.buckets, counts):
                            running += n
                            lines.append(f"{name}_bucket{_tag(key, [('le', _fmt(bound))])} {running}")
                        lines.append(f"{name}_bucket{_tag(key, [('le', '+Inf')])} {count}")
                        lines.append(f"{name}_sum{_tag(key)} {_fmt(total)}")
                        lines.append(f"{name}_count{_tag(key)} {count}")
            else:
                help_text, fn = gauges[name]
                lines.append(f"# HELP {name} {help_text or name}")
                lines.append(f"# TYPE {name} gauge")
                try:
                    got = fn() if fn is not None else None
                except Exception:  # noqa: BLE001 - a gauge that cannot be read is skipped, never an error page
                    got = None
                if got is None:
                    continue
                samples = got if isinstance(got, list) else [({}, got)]
                for labels, value in samples:
                    if value is not None:
                        lines.append(f"{name}{_tag(tuple(sorted(labels.items())))} {_fmt(value)}")
        return "\n".join(lines) + "\n" if lines else ""


def render(registry: "Registry") -> str:
    return registry.render()


REGISTRY = Registry()
REGISTRY.declare(HTTP_REQUESTS, "counter", "HTTP requests by route class and status class.")
REGISTRY.declare(HTTP_SECONDS, "histogram", "HTTP request duration in seconds by route class.")
REGISTRY.declare(HTTP_INFLIGHT, "gauge", "Requests being handled right now.")
REGISTRY.declare(DB_CHECKOUT_SECONDS, "histogram", "Seconds to open a database connection for a request.")
REGISTRY.declare(DB_POOL_IN_USE, "gauge", "Postgres pool connections checked out (pooling on only).")
REGISTRY.declare(DB_POOL_WAITING, "gauge", "Requests waiting for a pooled Postgres connection.")
REGISTRY.declare(TAKE_SECONDS, "histogram", "Take duration by phase: time in the room, and our own overhead.")
REGISTRY.declare(JOB_SECONDS, "histogram", "Job run time by type.", buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900, 3600))
REGISTRY.declare(JOB_QUEUE_WAIT_SECONDS, "histogram", "Seconds a job waited from creation to being claimed, by type.",
                 buckets=(0.5, 1, 5, 15, 60, 300, 900, 3600))
REGISTRY.declare(RUNNER_CLAIMS, "counter", "Runner claim attempts by outcome (claimed or empty).")
REGISTRY.declare(BACKUP_AGE, "gauge", "Seconds since the newest complete backup in VOLT_BACKUP_DIR.")
REGISTRY.declare(DRILL_AGE, "gauge", "Seconds since the last restore drill that passed (drill.json in VOLT_BACKUP_DIR).")
REGISTRY.declare(DROPPED, "counter", "Label sets refused because the registry was full.")
REGISTRY.declare(RATE_LIMITED, "counter", "Requests refused with 429, by kind: rate (a token bucket), auth (too many failed tokens), quota.")
REGISTRY.declare(BUSY, "counter", "Requests refused with 503 because the node was at its concurrency limit.")


# ---- recording helpers used by serve, take and the runner -------------------------------------------

def record_request(method: str, fn_name: Optional[str], path: str, status: int, seconds: float,
                   registry: Registry = REGISTRY) -> None:
    try:
        cls = route_class(method, fn_name, path)
        registry.counter(HTTP_REQUESTS, {"route_class": cls, "status_class": status_class(status)}).inc()
        registry.histogram(HTTP_SECONDS, {"route_class": cls}).observe(seconds)
    except Exception:  # noqa: BLE001 - metrics never change a response
        pass


class Inflight:
    """A counter of requests in progress, read by a gauge."""

    def __init__(self):
        self._lock, self.n = threading.Lock(), 0

    def __enter__(self):
        with self._lock:
            self.n += 1
        return self

    def __exit__(self, *exc):
        with self._lock:
            self.n -= 1


class TimedRoom:
    """Wraps a room and adds up the time spent inside its calls, so a take can report room time and our own overhead apart."""

    def __init__(self, room, clock=time.perf_counter):
        self._room, self._clock = room, clock
        self.seconds = 0.0

    def __getattr__(self, name):
        attr = getattr(self._room, name)
        if not callable(attr):
            return attr

        def timed(*a, **kw):
            started = self._clock()
            try:
                return attr(*a, **kw)
            finally:
                self.seconds += self._clock() - started
        return timed


# ---- backups and drills ---------------------------------------------------------------------------

def _parse_stamp(text) -> Optional[float]:
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def newest_backup_epoch(root) -> Optional[float]:
    """created_at (epoch) of the newest complete backup under `root` (the folder itself or one level down): a readable manifest whose
    data file exists at the size the manifest records. This is a cheap check, not `volt verify`; it does not re-hash the dump."""
    from volt import backup
    root = Path(root)
    if not root.is_dir():
        return None
    best = None
    for directory in [root, *[p for p in root.iterdir() if p.is_dir()]]:
        try:
            manifest = backup.read_manifest(directory)
            data = directory / manifest["file"]
            if not data.is_file() or data.stat().st_size != manifest["size"]:
                continue
        except Exception:  # noqa: BLE001 - an incomplete or foreign folder is not a backup
            continue
        at = _parse_stamp(manifest.get("created_at"))
        if at is not None and (best is None or at > best):
            best = at
    return best


def last_drill_epoch(root) -> Optional[float]:
    """When the last restore drill that passed finished (`drill.json`: {"at": ..., "ok": true}), or None."""
    try:
        doc = json.loads((Path(root) / "drill.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("ok") is not True:
        return None
    return _parse_stamp(doc.get("at"))


class Cached:
    """Call `fn` at most once per `ttl` seconds; a scrape never walks the disk more often than that."""

    def __init__(self, fn, ttl: float = 60.0, now=time.monotonic):
        self._fn, self._ttl, self._now = fn, ttl, now
        self._at, self._value = None, None
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            if self._at is None or self._now() - self._at >= self._ttl:
                self._value, self._at = self._fn(), self._now()
            return self._value


def age_gauge(find: Callable[[], Optional[float]], wall=time.time) -> Callable[[], Optional[float]]:
    def read():
        at = find()
        return None if at is None else max(0, int(wall() - at))
    return read


def install_gauges(registry: Registry, inflight: Inflight, pool_stats: Callable[[], Optional[dict]],
                   backup_dir: Callable[[], Optional[str]], wall=time.time) -> None:
    """Attach the scrape-time gauges. `pool_stats()` returns psycopg_pool stats or None; `backup_dir()` the VOLT_BACKUP_DIR or None."""
    registry.gauge_fn(HTTP_INFLIGHT, lambda: inflight.n)

    def pool(key):
        def read():
            stats = pool_stats()
            if not stats:
                return None
            if key == "in_use":
                return max(0, int(stats.get("pool_size", 0)) - int(stats.get("pool_available", 0)))
            return int(stats.get("requests_waiting", 0))
        return read
    registry.gauge_fn(DB_POOL_IN_USE, pool("in_use"))
    registry.gauge_fn(DB_POOL_WAITING, pool("waiting"))

    def folder_of(find):
        def read():
            root = backup_dir()
            return find(root) if root else None
        return read
    registry.gauge_fn(BACKUP_AGE, Cached(age_gauge(folder_of(newest_backup_epoch), wall)))
    registry.gauge_fn(DRILL_AGE, Cached(age_gauge(folder_of(last_drill_epoch), wall)))
