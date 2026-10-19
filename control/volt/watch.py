"""Watch ingest folders. Stable files are requested; the runner copies."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from volt.ingest import IngestError, IngestResult, request as ingest_request

log = logging.getLogger("volt.watch")


def env_on(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def env_quarantine(env=None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("VOLT_QUARANTINE") or env.get("VOLT_QUARANTINE_DIR")
    if raw:
        return Path(raw)
    out = env.get("VOLT_OUT")
    return Path(out) / "quarantine" if out else Path("quarantine")


def source_key(path: Path, size: int, mtime_ns: int) -> str:
    return f"file:{os.path.normcase(str(path))}:{size}:{mtime_ns}"


def seen_path(db: str, env=None) -> Optional[Path]:
    """Where the CLI stores fingerprints between `volt watch` processes. Runner periodic keeps `seen` in memory."""
    env = os.environ if env is None else env
    raw = (env.get("VOLT_WATCH_SEEN") or "").strip()
    if raw:
        return Path(raw)
    if "://" in db:
        return None
    return Path(db + ".watch-seen.json")


def load_seen(path: Optional[Path]) -> dict:
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        log.warning("cannot read watch seen file %s", path)
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, val in raw.items():
        if not isinstance(val, list) or len(val) != 3:
            continue
        size, mtime_ns, first = val
        try:
            first_seen = float("inf") if first is None else float(first)
            out[Path(key)] = (int(size), int(mtime_ns), first_seen)
        except (TypeError, ValueError):
            continue
    return out


def save_seen(path: Optional[Path], seen: dict) -> None:
    if path is None:
        return
    payload = {}
    for key, triple in seen.items():
        size, mtime_ns, first = triple
        payload[str(key)] = [size, mtime_ns, None if first == float("inf") else first]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def _inside(path: Path, root: Path) -> bool:
    try:
        a, b = os.path.normcase(str(path)), os.path.normcase(str(root))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def scan(
    store,
    roots: list[Path],
    quarantine: Path,
    *,
    owner: str = "desk",
    stable_seconds: float = 5.0,
    now: Callable[[], float] = time.time,
    seen: dict,
) -> list[IngestResult]:
    """Request ingest for regular files in `roots` whose size and mtime have been still long enough."""
    results: list[IngestResult] = []
    now_ts = now()
    resolved_roots = [Path(r).resolve() for r in roots]
    live: set[Path] = set()
    for root in resolved_roots:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            log.warning("cannot list ingest folder %s", root)
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name.endswith(".part"):
                continue
            if not entry.is_file():
                continue
            try:
                path = entry.resolve()
                st = path.stat()
            except OSError:
                continue
            if not any(_inside(path, allowed) for allowed in resolved_roots):
                continue
            live.add(path)
            size, mtime_ns = st.st_size, st.st_mtime_ns
            prev = seen.get(path)
            if prev is None or prev[0] != size or prev[1] != mtime_ns:
                seen[path] = (size, mtime_ns, now_ts)
                first_seen = now_ts
            else:
                first_seen = prev[2]
            if now_ts - first_seen < stable_seconds:
                continue
            key = source_key(path, size, mtime_ns)
            try:
                result = ingest_request(
                    store, path, owner=owner, quarantine=quarantine, source_key=key)
            except IngestError:
                log.exception("watch request failed for %s", path)
                continue
            seen[path] = (size, mtime_ns, float("inf"))
            results.append(result)
    for path in list(seen):
        if path not in live:
            del seen[path]
    return results


def main(argv: list[str]) -> int:
    import argparse

    from volt import building, logs
    from volt.migrations import SchemaTooNewError
    from volt.store import Store

    ap = argparse.ArgumentParser(
        prog="volt watch",
        description="Enqueue ingest for files that have sat still in VOLT_INGEST_ROOTS.",
    )
    ap.add_argument("db", nargs="?", default=os.environ.get("VOLT_DB"),
                    help="SQLite path or postgres:// DSN (or VOLT_DB)")
    ap.add_argument("--once", action="store_true", help="one scan, then exit")
    args = ap.parse_args(argv)
    if not args.db:
        ap.error("a database path or DSN is required")
    roots = building.ingest_roots()
    if not roots:
        print("volt watch: set VOLT_INGEST_ROOTS", file=sys.stderr)
        return 2
    quarantine = env_quarantine()
    logs.setup("watch")
    try:
        store = Store(args.db)
    except SchemaTooNewError as exc:
        print(f"volt watch: {exc}", file=sys.stderr)
        return 78
    persist = seen_path(args.db)
    seen: dict = load_seen(persist)
    every = float(os.environ.get("VOLT_WATCH_EVERY") or 2)
    stable = float(os.environ.get("VOLT_WATCH_STABLE_SECONDS") or 5)
    try:
        if args.once:
            scan(store, roots, quarantine, stable_seconds=stable, seen=seen)
            save_seen(persist, seen)
            return 0
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        log.info("watch started (every %ss, stable %ss)", every, stable)
        while not stop.is_set():
            scan(store, roots, quarantine, stable_seconds=stable, seen=seen)
            save_seen(persist, seen)
            stop.wait(every)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
