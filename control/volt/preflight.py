"""`volt check`: is this machine ready to run Volt with this configuration?

Read-only. It opens the database read-only, probes a port by binding and releasing it, and never prints a
secret: every detail goes through logs.redact, and tokens are only ever reported as set or not set.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import volt
from volt import disk, health, logs, migrations

OK, WARN, FAIL = "ok", "warn", "fail"
PACKAGE_ROOT = Path(volt.__file__).resolve().parents[1]  # control/ in a checkout


@dataclass
class Result:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": logs.redact(self.detail)}


def default_db() -> str:
    return str(PACKAGE_ROOT / "volt.db")


def _python() -> Result:
    v = sys.version_info
    text = f"{v.major}.{v.minor}.{v.micro}"
    return Result("python", OK if v >= (3, 12) else FAIL, text if v >= (3, 12) else f"{text}; Volt needs 3.12 or newer")


def _tool(name: str) -> Result:
    path = shutil.which(name)
    if not path:
        return Result(name, FAIL, "not found on PATH (needed for proxies, thumbnails, shot detection and publishing)")
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15)
        first = (out.stdout or out.stderr).splitlines()[0] if (out.stdout or out.stderr) else ""
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(name, FAIL, f"{path} does not run: {exc}")
    return Result(name, OK, f"{path} ({first[:60]})")


def _layout() -> Result:
    missing = [p for p in ("session/index.html", "schema.sql") if not (PACKAGE_ROOT / p).is_file()]
    if missing:
        return Result("install layout", FAIL,
                      f"{', '.join(missing)} not found beside the volt package at {PACKAGE_ROOT}; "
                      "install from a checkout with `pip install -e control`")
    return Result("install layout", OK, str(PACKAGE_ROOT))


def _schema_result(version: Optional[int], kind: str) -> Result:
    if version is None:
        return Result("database", WARN, f"{kind} reachable; no schema yet, it will be created on first start")
    if version == migrations.LATEST:
        return Result("database", OK, f"{kind} reachable, schema version {version} (current)")
    if version < migrations.LATEST:
        return Result("database", WARN, f"{kind} reachable, schema version {version}; "
                                       f"it will migrate to {migrations.LATEST} on the next start")
    return Result("database", FAIL, f"{kind} schema version {version} is newer than this build "
                                    f"({migrations.LATEST}); upgrade Volt, do not run an older build against it")


def _database(target: str) -> Result:
    from volt import pg

    if pg.is_dsn(target):
        os.environ.setdefault("PGCONNECT_TIMEOUT", "10")
        try:
            from volt.store import Store

            store = Store(target, init=False)
        except Exception as exc:  # noqa: BLE001 - missing driver, unreachable, bad credentials
            return Result("database", FAIL, f"postgres {logs.redact_dsn(target)}: {str(exc).strip()[:200]}")
        try:
            try:
                row = store.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
                version = row[0] if row is not None else None
            except Exception:  # noqa: BLE001 - no schema_version table yet
                store.conn.rollback()
                version = None
        finally:
            store.close()
        return _schema_result(version, f"postgres {logs.redact_dsn(target)}")
    path = Path(target)
    if not path.exists():
        parent = path.resolve().parent
        if not parent.is_dir():
            return Result("database", FAIL, f"{path}: directory {parent} does not exist")
        if not os.access(parent, os.W_OK):
            return Result("database", FAIL, f"{path}: directory {parent} is not writable")
        return Result("database", OK, f"sqlite {path} does not exist yet; it will be created on first start")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            try:
                version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            except sqlite3.OperationalError:
                version = None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Result("database", FAIL, f"sqlite {path}: {exc}")
    result = _schema_result(version, f"sqlite {path}")
    if result.status != FAIL and not os.access(path, os.W_OK):
        return Result("database", FAIL, f"sqlite {path} is not writable by this user")
    return result


def _port(host: str, port: int) -> Result:
    if port == 0:
        return Result("port", OK, "0: a free port is chosen at start")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host.strip("[]"), port))
    except OSError as exc:
        return Result("port", FAIL, f"{host}:{port} cannot be bound ({exc.strerror or exc}); "
                                    "is Volt already running, or is another program using it?")
    finally:
        sock.close()
    return Result("port", OK, f"{host}:{port} is free")


def run_checks(env=None, db: Optional[str] = None, host: Optional[str] = None, port: Optional[int] = None) -> list:
    from volt import serve

    env = os.environ if env is None else env
    host = host or env.get("VOLT_HOST") or "127.0.0.1"
    port = port if port is not None else int(env.get("VOLT_PORT") or 8765)
    db = db or env.get("VOLT_DB") or default_db()
    results = [_python(), _tool("ffmpeg"), _tool("ffprobe"), _layout(), _database(db)]

    storage = health.storage(env)
    results.append(Result(f"storage ({storage['backend']})", OK if storage["ok"] else FAIL, storage["detail"]))

    token = env.get("VOLT_TOKEN") or None
    try:
        serve.check_bind(host, token)
        results.append(Result("access", OK, "token required" if token else "no token: loopback only"))
    except serve.ConfigError as exc:
        results.append(Result("access", FAIL, str(exc)))
    results.append(_port(host, port))

    try:
        floor = disk.min_free_bytes(env)
    except ValueError as exc:
        results.append(Result("disk floor", FAIL, str(exc)))
        floor = None
    if floor is not None:
        out_dir = env.get("VOLT_OUT") or str(PACKAGE_ROOT / "out")
        for entry in health.directories(out_dir, env, db):
            free = entry["freeBytes"]
            detail = f"{entry['path']}: {'unknown' if free is None else f'{free // 1048576} MB'} free (floor {floor // 1048576} MB)"
            results.append(Result(f"disk {entry['name']}", WARN if entry["low"] else OK, detail))

    if not env.get("VOLT_PROXY_DIR"):
        results.append(Result("runner", WARN, "VOLT_PROXY_DIR is not set: proxy jobs will fail until it is"))
    else:
        results.append(Result("runner", OK, "proxy directory set"))
    if not env.get("VOLT_ROOM_URL"):
        results.append(Result("clock", WARN, "VOLT_ROOM_URL is not set: `volt run` will not start the clock"))
    else:
        results.append(Result("clock", OK, "room URL set" + (", token set" if env.get("VOLT_ROOM_TOKEN") else "")))
    if (env.get("VOLT_ASR_ENGINE") or "").lower() == "faster-whisper":
        try:
            import faster_whisper  # noqa: F401
            results.append(Result("speech", OK, "faster-whisper importable"))
        except ImportError:
            results.append(Result("speech", FAIL, "VOLT_ASR_ENGINE=faster-whisper but it is not installed "
                                                  "(pip install volt-control[speech])"))
    return results


def render(results: list) -> str:
    width = max(len(r.name) for r in results)
    return "\n".join(f"[{r.status:<4}] {r.name:<{width}}  {logs.redact(r.detail)}" for r in results)


def exit_code(results: list) -> int:
    return 1 if any(r.status == FAIL for r in results) else 0
