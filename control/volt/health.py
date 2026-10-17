"""What an operator needs to know about a running Volt, in one place.

`liveness()` is the unauthenticated /health body: it says the process answers and nothing else.
`collect(store, ...)` is the authenticated /api/health body, and `prometheus(report)` renders the same
numbers as /metrics text. Nothing here writes to the database or to disk.

Status is `ok`, `degraded` (the server works but something needs a person: a dead runner with work waiting,
stuck jobs, a disk under its floor, a storage backend that is not configured properly) or `down` (the
database cannot be read). The HTTP layer answers 503 only for `down`.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from volt import __version__, disk, migrations

DEFAULT_RUNNER_STALE = 120.0   # seconds without a heartbeat before a runner counts as gone
DEFAULT_STUCK_SECONDS = 4 * 3600.0  # a job that has run this long gets flagged (VOLT_STUCK_JOB_SECONDS)
_THUMB_SIZE_TTL = 30.0
_size_cache: dict = {}


def liveness() -> dict:
    return {"status": "ok", "version": __version__}


def _parse(value) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age(value, now: datetime) -> Optional[float]:
    parsed = _parse(value)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds())


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _float_env(env, name: str, default: float) -> float:
    try:
        return float(env.get(name) or default)
    except ValueError:
        return default


# ---- parts ---------------------------------------------------------------------------------------

def database(store) -> dict:
    """Reachable, and the schema version against what this code expects."""
    row = store.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    version = row[0] if row is not None else None
    out = {"reachable": True, "kind": "postgres" if store.postgres else "sqlite",
           "schemaVersion": version, "expectedSchemaVersion": migrations.LATEST}
    out["schemaState"] = ("current" if version == migrations.LATEST else
                          "behind" if (version or 0) < migrations.LATEST else "newer than this code")
    return out


def runners(store, now: datetime, env) -> dict:
    stale_after = _float_env(env, "VOLT_RUNNER_STALE_SECONDS", DEFAULT_RUNNER_STALE)
    rows = store.conn.execute(
        "SELECT name, pid, host, started_at, beat_at FROM runner_heartbeat ORDER BY beat_at DESC").fetchall()
    listed = []
    for row in rows:
        age = _age(row["beat_at"], now)
        listed.append({"name": row["name"], "pid": row["pid"], "host": row["host"],
                       "heartbeatAgeSeconds": None if age is None else round(age, 1),
                       "alive": age is not None and age <= stale_after})
    ages = [r["heartbeatAgeSeconds"] for r in listed if r["heartbeatAgeSeconds"] is not None]
    return {"staleAfterSeconds": stale_after, "count": len(listed),
            "alive": sum(1 for r in listed if r["alive"]),
            "newestHeartbeatAgeSeconds": min(ages) if ages else None,
            "runners": listed[:10]}


def queue(store, now: datetime, env) -> dict:
    stuck_after = _float_env(env, "VOLT_STUCK_JOB_SECONDS", DEFAULT_STUCK_SECONDS)
    by_type: dict = {}
    for row in store.conn.execute("SELECT type, status, COUNT(*) AS n FROM job GROUP BY type, status").fetchall():
        by_type.setdefault(row["type"], {})[row["status"]] = row["n"]
    oldest = store.conn.execute("SELECT MIN(created_at) AS t FROM job WHERE status = 'queued'").fetchone()
    # A queued job with a future run_after is waiting out a backoff, not starved.
    due = store.conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_at) AS t FROM job WHERE status = 'queued' "
        "AND (run_after IS NULL OR run_after <= ?)", (_stamp(now),)).fetchone()
    stuck = []
    for row in store.conn.execute(
            "SELECT id, type, asset_id, claimed_by, lease_expires_at, updated_at FROM job "
            "WHERE status = 'running' ORDER BY updated_at LIMIT 500").fetchall():
        lease = _parse(row["lease_expires_at"])
        running_for = _age(row["updated_at"], now)
        reason = None
        if lease is not None and lease < now:
            reason = "lease expired (the runner stopped or crashed)"
        elif running_for is not None and running_for > stuck_after:
            reason = f"running for more than {int(stuck_after)} s"
        if reason:
            stuck.append({"id": row["id"], "type": row["type"], "asset": row["asset_id"],
                          "runner": row["claimed_by"], "reason": reason,
                          "runningSeconds": None if running_for is None else round(running_for)})
    return {"byTypeAndState": by_type,
            "queued": sum(s.get("queued", 0) for s in by_type.values()),
            "queuedDue": due["n"],
            "oldestQueuedAgeSeconds": _round(_age(oldest["t"], now)),
            "oldestDueAgeSeconds": _round(_age(due["t"], now)),
            "running": sum(s.get("running", 0) for s in by_type.values()),
            "failed": sum(s.get("failed", 0) for s in by_type.values()),
            "stuckAfterSeconds": stuck_after, "stuckCount": len(stuck), "stuck": stuck[:10]}


def _round(value):
    return None if value is None else round(value, 1)


def as_run(store, now: datetime) -> dict:
    row = store.conn.execute("SELECT MAX(as_run_in) AS a, MAX(as_run_out) AS b FROM item").fetchone()
    newest = max((p for p in (_parse(row["a"]), _parse(row["b"])) if p), default=None)
    return {"lastAsRun": None if newest is None else _stamp(newest),
            "lastAsRunAgeSeconds": None if newest is None else round((now - newest).total_seconds())}


def directories(out_dir, env, db_target: Optional[str] = None) -> list:
    """Free space for every configured working directory, against the floor."""
    floor = disk.min_free_bytes(env)
    candidates = [("out", out_dir), ("proxy", env.get("VOLT_PROXY_DIR")), ("publish", env.get("VOLT_PUBLISH_DIR")),
                  ("quarantine", env.get("VOLT_QUARANTINE_DIR")), ("scratch", env.get("VOLT_STORAGE_SCRATCH"))]
    if db_target and not db_target.startswith(("postgres://", "postgresql://")) and db_target != ":memory:":
        candidates.append(("database", str(Path(db_target).parent)))
    result = []
    for name, path in candidates:
        if not path:
            continue
        free = disk.free_bytes(path)
        entry = {"name": name, "path": str(path), "freeBytes": free, "floorBytes": floor,
                 "low": free is not None and free < floor}
        result.append(entry)
    return result


def thumb_cache(out_dir, env) -> dict:
    root = Path(out_dir) / "thumbs"
    now = time.monotonic()
    cached = _size_cache.get(str(root))
    if cached is None or now - cached[0] > _THUMB_SIZE_TTL:
        _size_cache[str(root)] = cached = (now, disk.tree_size(root))
    return {"bytes": cached[1], "capBytes": disk.thumb_cache_cap_bytes(env)}


def storage(env) -> dict:
    """Configuration check only: no network call, so a cloud outage does not hang the health endpoint."""
    from volt import storage as storage_mod

    kind = (env.get("VOLT_STORAGE") or "local").strip().lower()
    publish_dir = env.get("VOLT_PUBLISH_DIR")
    if kind == "local" and not publish_dir:
        return {"backend": kind, "ok": True, "probed": False,
                "detail": "no publish directory set (VOLT_PUBLISH_DIR); the runner cannot publish until one is"}
    try:
        storage_mod.from_env(env, local_root=Path(publish_dir) if publish_dir else None)
    except Exception as exc:  # noqa: BLE001 - StorageConfigError, or a missing SDK
        from volt.logs import redact
        return {"backend": kind, "ok": False, "probed": False, "detail": redact(str(exc))[:300]}
    if kind == "local":
        target = Path(publish_dir)
        probe = target if target.exists() else next((p for p in target.parents if p.exists()), target)
        writable = os.access(probe, os.W_OK)
        return {"backend": kind, "ok": writable, "probed": True,
                "detail": "publish directory is writable" if writable else f"{probe} is not writable"}
    return {"backend": kind, "ok": True, "probed": False,
            "detail": "configuration valid and SDK importable; reachability is not probed"}


# ---- the report ----------------------------------------------------------------------------------

def collect(store, out_dir, env=None, db_target: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    report: dict = {"version": __version__, "time": _stamp(now), "problems": []}
    problems = report["problems"]
    try:
        report["database"] = database(store)
        report["runners"] = runners(store, now, env)
        report["queue"] = queue(store, now, env)
        report["asRun"] = as_run(store, now)
    except Exception as exc:  # noqa: BLE001 - any failure to read means the database is not usable
        from volt.logs import redact
        report["database"] = {"reachable": False, "error": redact(str(exc) or exc.__class__.__name__)[:300]}
        report["status"] = "down"
        problems.append("database cannot be read")
        return report
    report["disk"] = directories(out_dir, env, db_target)
    report["thumbCache"] = thumb_cache(out_dir, env)
    report["storage"] = storage(env)

    db, run, q = report["database"], report["runners"], report["queue"]
    if db["schemaState"] == "newer than this code":
        problems.append("the database schema is newer than this build; upgrade Volt")
    elif db["schemaState"] == "behind":
        problems.append("the database schema is behind this build")
    if q["queuedDue"] and run["alive"] == 0:
        problems.append(f"{q['queuedDue']} job(s) are waiting and no runner has checked in recently")
    if q["stuckCount"]:
        problems.append(f"{q['stuckCount']} job(s) look stuck")
    for entry in report["disk"]:
        if entry["low"]:
            problems.append(f"low disk space on {entry['name']} ({entry['path']})")
    if not report["storage"]["ok"]:
        problems.append(f"storage backend: {report['storage']['detail']}")
    report["status"] = "degraded" if problems else "ok"
    return report


# ---- Prometheus text -----------------------------------------------------------------------------

def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def prometheus(report: dict) -> str:
    lines: list = []

    def metric(name: str, help_text: str, kind: str, samples: list) -> None:
        samples = [(labels, value) for labels, value in samples if value is not None]
        if not samples:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            tag = "{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) + "}" if labels else ""
            lines.append(f"{name}{tag} {value}")

    up = 0 if report.get("status") == "down" else 1
    metric("volt_up", "1 when the database can be read.", "gauge", [({}, up)])
    metric("volt_info", "Build information.", "gauge", [({"version": report.get("version", "")}, 1)])
    metric("volt_degraded", "1 when health has at least one problem.", "gauge",
           [({}, 1 if report.get("problems") else 0)])
    if up:
        db, run, q = report["database"], report["runners"], report["queue"]
        metric("volt_schema_version", "Applied schema version.", "gauge", [({}, db.get("schemaVersion"))])
        metric("volt_runner_heartbeat_age_seconds", "Seconds since the newest runner heartbeat.", "gauge",
               [({}, run["newestHeartbeatAgeSeconds"])])
        metric("volt_runners_alive", "Runners with a recent heartbeat.", "gauge", [({}, run["alive"])])
        metric("volt_jobs", "Jobs by type and state.", "gauge",
               [({"type": t, "state": s}, n) for t, states in sorted(q["byTypeAndState"].items())
                for s, n in sorted(states.items())])
        metric("volt_queue_oldest_queued_age_seconds", "Age of the oldest queued job.", "gauge",
               [({}, q["oldestQueuedAgeSeconds"])])
        metric("volt_jobs_stuck", "Jobs with an expired lease or running too long.", "gauge", [({}, q["stuckCount"])])
        metric("volt_last_as_run_age_seconds", "Seconds since the newest as-run time.", "gauge",
               [({}, report["asRun"]["lastAsRunAgeSeconds"])])
        metric("volt_disk_free_bytes", "Free bytes on the disk holding each directory.", "gauge",
               [({"dir": d["name"]}, d["freeBytes"]) for d in report["disk"]])
        metric("volt_disk_low", "1 when free space is under the floor.", "gauge",
               [({"dir": d["name"]}, 1 if d["low"] else 0) for d in report["disk"]])
        metric("volt_thumb_cache_bytes", "Size of the thumbnail cache.", "gauge", [({}, report["thumbCache"]["bytes"])])
        metric("volt_storage_ok", "1 when the storage backend configuration is valid.", "gauge",
               [({"backend": report["storage"]["backend"]}, 1 if report["storage"]["ok"] else 0)])
    return "\n".join(lines) + "\n"
