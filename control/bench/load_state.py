"""Load the operator's state poll: N desks each polling GET /api/state every 2 s against a real `serve`.

    python -m bench.load_state --url http://127.0.0.1:8765 --token T --desks 50 --seconds 600 [--no-keepalive] --json
    python -m bench.load_state --in-process --desks 50 --seconds 60 --assets 10000 --json
    python -m bench.load_state --seed scratch.db --assets 10000      # then: python -m volt.serve --db scratch.db --port 8799
                                                                    #       python -m bench.load_state --url http://127.0.0.1:8799 ...

`--in-process` seeds a scratch SQLite database, starts the real handler on loopback in this process and also reports the
number of SQL statements per request (counted with sqlite3's trace callback, bench only). Against `--url` the server is
somebody else's process, so that figure is null. Stdlib threads only.

In-process mode puts the load generator and the server in one Python process, so they share one interpreter lock: it is the pessimistic
figure. For a figure closer to a deployment, seed a file, start `serve` as its own process and point `--url` at it.

Numbers from this script belong to the machine that produced them: record the box next to the figure
(docs/operations.md, "Measured on"). They are not a capacity claim for any other box."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

CONTROL = str(Path(__file__).resolve().parents[1])
if CONTROL not in sys.path:
    sys.path.insert(0, CONTROL)

POLL_SECONDS = 2.0


def percentile(sorted_ms: list, p: float) -> float:
    if not sorted_ms:
        return 0.0
    k = min(len(sorted_ms) - 1, max(0, int(round(p / 100.0 * (len(sorted_ms) - 1)))))
    return sorted_ms[k]


def seed(db_path: str, assets: int) -> None:
    """A scratch database with `assets` library rows (each with a hi-res essence) and one show with a few items."""
    from volt.rundown import add_item, open_show
    from volt.store import Store, _id, _now

    store = Store(db_path)
    try:
        now = _now()
        with store.transaction():
            store.conn.executemany(
                "INSERT INTO asset (id, owner, retention_class, legal_hold, cleared, show_name, camera, created_at) "
                "VALUES (?, 'desk', 'show', 0, 0, 'Load', ?, ?)",
                [(f"a{n:07d}", f"cam-{n % 8}", now) for n in range(assets)])
            store.conn.executemany(
                "INSERT INTO essence (id, asset_id, role, location, open, created_at) VALUES (?, ?, 'hi-res', ?, 0, ?)",
                [(_id(), f"a{n:07d}", f"D:/media/a{n:07d}.mxf", now) for n in range(assets)])
        show = open_show(store, "Load show")
        for n in range(8):
            add_item(store, show, f"story {n}")
    finally:
        store.close()


def _poll_loop(host: str, port: int, path: str, headers: dict, keepalive: bool, deadline: float, interval: float,
               out: list, errors: list, lock: threading.Lock, jitter: float) -> None:
    time.sleep(jitter)
    conn = None
    mine, bad = [], 0
    while time.monotonic() < deadline:
        started = time.perf_counter()
        try:
            if conn is None:
                conn = http.client.HTTPConnection(host, port, timeout=30)
            conn.request("GET", path, headers=headers if keepalive else dict(headers, Connection="close"))
            resp = conn.getresponse()
            resp.read()
            if resp.status != 200:
                bad += 1
            if not keepalive:
                conn.close()
                conn = None
        except (OSError, http.client.HTTPException):
            bad += 1
            if conn is not None:
                conn.close()
            conn = None
        mine.append((time.perf_counter() - started) * 1000.0)
        left = interval - (time.perf_counter() - started)
        if left > 0:
            time.sleep(min(left, max(0.0, deadline - time.monotonic())))
    if conn is not None:
        conn.close()
    with lock:
        out.extend(mine)
        errors.append(bad)


def run(url: str, token: str | None, desks: int, seconds: float, keepalive: bool = True, interval: float = POLL_SECONDS,
        path: str = "/api/state") -> dict:
    """Drive `desks` pollers for `seconds`; returns latency percentiles, requests per second and error count."""
    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    out, errors, lock = [], [], threading.Lock()
    started = time.monotonic()
    deadline = started + seconds
    threads = [threading.Thread(target=_poll_loop, args=(host, port, path, headers, keepalive, deadline, interval, out,
                                                         errors, lock, (interval * n) / max(1, desks)), daemon=True)
               for n in range(desks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(seconds + 60)
    elapsed = max(1e-6, time.monotonic() - started)
    out.sort()
    return {"desks": desks, "seconds": round(elapsed, 2), "keepalive": keepalive, "requests": len(out),
            "errors": sum(errors), "rps": round(len(out) / elapsed, 2), "p50Ms": round(percentile(out, 50), 2),
            "p95Ms": round(percentile(out, 95), 2), "p99Ms": round(percentile(out, 99), 2),
            "maxMs": round(out[-1], 2) if out else 0.0}


def run_in_process(desks: int, seconds: float, assets: int, keepalive: bool = True, interval: float | None = None) -> dict:
    """Seed a scratch SQLite database, serve it from this process, drive it, and count SQL statements per request."""
    from volt import serve
    from volt.store import Store

    interval = POLL_SECONDS if interval is None else interval
    counted = [0]
    counted_lock = threading.Lock()
    real_store = serve.Store

    class CountingStore(real_store):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            if not self.postgres:
                def trace(_sql):
                    with counted_lock:
                        counted[0] += 1
                self.conn.set_trace_callback(trace)

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "load.db")
        seed(db, assets)
        saved = (serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.Store)
        serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.Store = db, None, None, CountingStore
        srv = serve.VoltServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            report = run(f"http://127.0.0.1:{srv.server_port}", None, desks, seconds, keepalive, interval=min(interval, max(0.05, seconds / 4)))
        finally:
            srv.shutdown()
            srv.server_close()
            serve.DB_PATH, serve.TOKEN, serve.ROOM, serve.Store = saved
    report["assets"] = assets
    report["sqlStatementsPerRequest"] = round(counted[0] / report["requests"], 2) if report["requests"] else 0.0
    return report


def box() -> dict:
    return {"os": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count(),
            "machine": platform.machine()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench.load_state", description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="base URL of a running serve (omit with --in-process)")
    ap.add_argument("--token", default=os.environ.get("VOLT_TOKEN"), help="bearer token (or VOLT_TOKEN)")
    ap.add_argument("--seed", metavar="DB", help="create a scratch SQLite database with --assets library rows and exit")
    ap.add_argument("--in-process", action="store_true", help="seed a scratch SQLite database and serve it from this process")
    ap.add_argument("--desks", type=int, default=50)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--assets", type=int, default=100, help="library size for --in-process")
    ap.add_argument("--interval", type=float, default=POLL_SECONDS, help="seconds between one desk's polls")
    ap.add_argument("--keepalive", dest="keepalive", action="store_true", default=True)
    ap.add_argument("--no-keepalive", dest="keepalive", action="store_false")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.seed:
        seed(args.seed, args.assets)
        print(f"seeded {args.assets} assets into {args.seed}")
        return 0
    if args.in_process:
        report = run_in_process(args.desks, args.seconds, args.assets, args.keepalive, args.interval)
    elif args.url:
        report = run(args.url, args.token, args.desks, args.seconds, args.keepalive, args.interval)
        report["sqlStatementsPerRequest"] = None
    else:
        ap.error("give --url or --in-process")
    report["box"] = box()
    print(json.dumps(report, indent=None if args.json else 2))
    return 0 if report["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
