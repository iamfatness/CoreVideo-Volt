"""Search at scale: a deterministic corpus generator and a timing harness. Run by hand, never in CI.

    cd control
    python -m tests.bench_search --db /tmp/bench.db --profile news --assets 10000 --seed 1 --runs 200
    python -m tests.bench_search --db "$DSN" --profile long --assets 10000        (a postgres DSN works the same)

Prints one JSON object: {"profile", "assets", "documents", "db_bytes", "index_bytes", "rss_bytes", "queries": {"<class>": {"p50", "p95", "p99"}}}
(times in milliseconds, over every timed run of every query in the class).

The generator writes the rows search reads (assets, typed metadata, passages in `search_doc`) with `executemany`, never through the
API. It does not write word-level `span` rows: at 10,000 assets of 30 minutes those alone would be tens of gigabytes (the design's
finding to report, not something this harness hides), so it measures the search path and not transcription storage. The corpus is
deterministic for a seed: ids, times, words and metadata are drawn from one `random.Random(seed)`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from bisect import bisect_left
from datetime import datetime, timedelta, timezone

from volt import metadata, searchindex
from volt.store import Store

PROFILES = {"news": {"passages": 45, "words": 20}, "long": {"passages": 225, "words": 20}}   # about 6 and 30 minutes of speech
SHOWS = ["Morning Brief", "Final Four Watch", "Night Desk", "City Hall Live", "Weather Now", "Market Close", "Sports Tonight",
         "The Late Edition", "Harbour Report", "School Board", "Courtroom", "Studio Sessions"]
CAMERAS = ["studio-a", "studio-b", "field-1", "field-2", "iso-1", "iso-2", "iso-3", "iso-4"]
OWNERS = ["desk", "ana", "ben", "carla", "dev", "elif"]
TOPICS = ["Sports", "Politics", "Weather", "Business", "Crime", "Health", "Education", "Transport", "Culture", "Environment", "Courts", "Weather-alert"]
SYLLABLES = ["ka", "lo", "mi", "ra", "su", "ten", "vo", "bri", "dan", "fel", "gor", "han", "jul", "nem", "pal", "quo", "sar", "tal", "uvi", "wen"]
START = datetime(2025, 1, 1, tzinfo=timezone.utc)
QUERY_CLASSES = ("single_term", "phrase", "prefix", "text_filters", "filter_only", "facet_heavy", "rare_term", "common_term", "person")
FACETS = "show,camera,owner,retention,cleared,created,meta.topic,meta.approved"


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def vocabulary(size: int = 6000) -> list:
    """`size` distinct pseudo-words, in rank order (rank 0 is the most common). Deterministic and independent of the seed."""
    out, seen, i = [], set(), 0
    while len(out) < size:
        n, parts = i, []
        for _ in range(3 + i % 2):
            parts.append(SYLLABLES[n % len(SYLLABLES)])
            n //= len(SYLLABLES)
        word = "".join(parts) + (str(i // 2000) if i >= 2000 else "")
        if word not in seen:
            seen.add(word)
            out.append(word)
        i += 1
    return out


def _bulk(store: Store, sql: str, rows: list) -> None:
    """executemany on either backend (the Postgres adapter has none of its own, so go to the driver with the translated SQL)."""
    if not rows:
        return
    if store.postgres:
        from volt.pg import translate
        with store.conn._conn.cursor() as cur:
            cur.executemany(translate(sql), rows)
    else:
        store.conn.executemany(sql, rows)


def checksum(store: Store) -> str:
    """A digest of what the generator wrote, for the determinism test: the assets, their values and every passage in order."""
    h = hashlib.sha256()
    for sql in ("SELECT id, show_name, camera, owner, cleared, created_at FROM asset ORDER BY id",
                "SELECT v.asset_id, f.key, v.ord, v.v_text, v.v_num, v.v_date FROM asset_meta_value v JOIN field_def f ON f.id = v.field_id ORDER BY v.asset_id, f.key, v.ord",
                "SELECT id, asset_id, kind, ref_id, tc_in, frame_in, body FROM search_doc ORDER BY id"):
        for row in store.conn.execute(sql).fetchall():
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def generate(store: Store, assets: int, profile: str, seed: int = 1) -> dict:
    """Fill an empty store with `assets` assets. Returns what the query set needs: word counts, and the people, shows and topics used."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    rng = random.Random(seed)
    prof = PROFILES[profile]
    vocab = vocabulary()
    cum, total = [], 0.0
    for rank in range(len(vocab)):
        total += 1.0 / (rank + 1)
        cum.append(total)
    draw = lambda: vocab[min(bisect_left(cum, rng.random() * total), len(vocab) - 1)]  # noqa: E731 - Zipf by rank
    fields = {}
    with store.transaction("bench:fields"):
        for key, label, ftype, required in (("topic", "Topic", "enum", False), ("guest", "Guest", "text", False), ("approved", "Approved", "bool", False),
                                            ("air_date", "Air date", "date", False), ("producer", "Producer", "text", True)):
            metadata.define_field(store, key, label, ftype, required=required, actor="bench")
        metadata.add_terms(store, "topic", TOPICS)
        for r in store.conn.execute("SELECT id, key FROM field_def").fetchall():
            fields[r["key"]] = r["id"]
    people = [f"person-{i:03d}" for i in range(60)]
    counts: dict = {}
    asset_rows, value_rows, doc_rows = [], [], []
    now_iso = _stamp(START)
    used_shows, seen_people = set(), set()
    for n in range(assets):
        aid = f"bench-{n:06d}"
        show, camera, owner = SHOWS[int(rng.random() ** 2 * len(SHOWS))], rng.choice(CAMERAS), rng.choice(OWNERS)
        used_shows.add(show)
        created = START + timedelta(days=rng.randrange(0, 700), seconds=rng.randrange(0, 86400))
        asset_rows.append((aid, owner, "show", False, rng.random() < 0.7, show, None, camera, None, _stamp(created)))
        topic = rng.choice(TOPICS)
        guest = f"{draw().title()} {draw().title()}"
        values = [("topic", topic, None, None), ("guest", guest, None, None), ("approved", None, 1.0 if rng.random() < 0.5 else 0.0, None),
                  ("air_date", None, None, _stamp(created + timedelta(days=rng.randrange(0, 5)))[:10])]
        if rng.random() < 0.8:
            values.append(("producer", rng.choice(OWNERS), None, None))
        for key, vt, vn, vd in values:
            value_rows.append((aid, fields[key], 0, vt, vn, vd, "bench", now_iso))
        doc_rows.append((f"bench-m-{n:06d}", aid, "meta", None, None, None, None, None, f"{topic}\n{guest}", 1.5, None, None, None, None, now_iso))
        for p in range(prof["passages"]):
            words = [draw() for _ in range(prof["words"])]
            for w in words:
                counts[w] = counts.get(w, 0) + 1
            sec = p * 8
            tc = f"{1 + sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}:00"
            doc_rows.append((f"bench-d-{n:06d}-{p:04d}", aid, "transcript", f"span-{n}-{p}", tc, tc, sec * 25, sec * 25 + 200, " ".join(words), 1.0, None, None, None, None, now_iso))
        if n % 20 == 0:           # a confirmed face label now and then, for the person queries
            who = rng.choice(people)
            seen_people.add(who)
            doc_rows.append((f"bench-f-{n:06d}", aid, "face", f"label-{n}", None, None, None, None, who, 1.2, "confirmed", who, 0.9, searchindex.norm_value(who), now_iso))
    cols = "id, asset_id, kind, ref_id, tc_in, tc_out, frame_in, frame_out, body, weight, status, person_id, confidence, value_norm, created_at"
    chunk = 5000
    with store.transaction("bench:load"):
        _bulk(store, "INSERT INTO asset (id, owner, retention_class, legal_hold, cleared, show_name, room, camera, source_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", asset_rows)
        _bulk(store, "INSERT INTO asset_meta_value (asset_id, field_id, ord, v_text, v_num, v_date, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", value_rows)
        for i in range(0, len(doc_rows), chunk):
            part = doc_rows[i:i + chunk]
            if store.postgres:
                from volt.searchindex import norm_text, pg_config
                _bulk(store, f"INSERT INTO search_doc ({cols}, tsv) VALUES ({', '.join('?' * 15)}, to_tsvector(?::regconfig, ?))",
                      [(*r, pg_config(), norm_text(r[8])) for r in part])
            else:
                _bulk(store, f"INSERT INTO search_doc ({cols}) VALUES ({', '.join('?' * 15)})", part)
    return {"counts": counts, "vocab": vocab, "shows": sorted(used_shows), "people": sorted(seen_people) or people, "assets": assets, "documents": len(doc_rows)}


def query_set(info: dict) -> list:
    """The 40 queries, as (class, name, search request). Words come from what the corpus holds: common ones from the top of the Zipf
    ranks, rare ones with a document frequency of one to three, so every class has something to find in a small corpus too."""
    counts = info["counts"]
    ranked = sorted(counts, key=lambda w: (-counts[w], w))
    common = ranked[:4]
    mid = ranked[len(ranked) // 20: len(ranked) // 20 + 6]
    rare = [w for w in reversed(ranked) if counts[w] <= 3][:4] or ranked[-4:]
    show = info["shows"][0]
    filt = lambda *c: {"all": list(c)}  # noqa: E731
    eq = lambda f, v: {"field": f, "op": "eq", "value": v}  # noqa: E731
    qs = []
    for i, w in enumerate(mid):
        qs.append(("single_term", f"term-{i}", {"q": w, "facets": "show,created"}))
    for i in range(4):
        qs.append(("phrase", f"phrase-{i}", {"q": f'"{common[i % 4]} {mid[i]}"'}))   # may be empty on a small corpus; the plan and time still count
    for i in range(4):
        qs.append(("prefix", f"prefix-{i}", {"q": mid[i][:3]}))
    for i, extra in enumerate([eq("show", show), eq("cleared", True), eq("meta.topic", "Sports"), eq("owner", "desk"), eq("meta.approved", True), eq("camera", "studio-a")]):
        qs.append(("text_filters", f"text-filter-{i}", {"q": mid[i % 6], "filter": filt(extra, eq("cleared", True)) if i else filt(extra, eq("owner", "desk"))}))
    for i, c in enumerate([eq("show", show), eq("cleared", False), eq("meta.topic", "Weather"), eq("owner", "ana"),
                           {"field": "created", "op": "range", "from": "2025-03-01", "to": "2025-04-30"}, {"field": "incomplete", "op": "eq", "value": True}]):
        qs.append(("filter_only", f"filter-{i}", {"filter": filt(c), "sort": "recent"}))
    for i in range(4):
        qs.append(("facet_heavy", f"facets-{i}", {"q": mid[i] if i % 2 else None, "facets": FACETS, "filter": filt(eq("cleared", True)) if i > 1 else None}))
    for i, w in enumerate(rare):
        qs.append(("rare_term", f"rare-{i}", {"q": w}))
    for i, w in enumerate(common):
        qs.append(("common_term", f"common-{i}", {"q": w}))
    for i in range(2):
        qs.append(("person", f"person-{i}", {"filter": filt({"field": "label.person", "op": "eq", "value": info["people"][i]})}))
    clean = []
    for cls, name, req in qs:
        clean.append((cls, name, {k: v for k, v in req.items() if v is not None}))
    return clean


def _percentile(sorted_ms: list, p: float) -> float:
    if not sorted_ms:
        return 0.0
    return round(sorted_ms[min(len(sorted_ms) - 1, int(round(p * (len(sorted_ms) - 1))))], 3)


def rss_bytes():
    """This process's resident memory, from the standard library only; None where it cannot be read."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            c = Counters()
            c.cb = ctypes.sizeof(c)
            kernel, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return None
            return int(c.WorkingSetSize) or None
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except Exception:  # noqa: BLE001 - a number that cannot be read is reported as missing, never guessed
        return None


def sizes(store: Store, target: str) -> tuple:
    """(database bytes, index bytes). SQLite: the file (and WAL), and the FTS shadow tables when dbstat is compiled in. Postgres: relation sizes."""
    if store.postgres:
        total = store.conn.execute("SELECT pg_total_relation_size('search_doc')").fetchone()[0]
        idx = store.conn.execute("SELECT COALESCE(pg_relation_size('search_doc_tsv'), 0)").fetchone()[0]
        return int(total), int(idx)
    db = sum(os.path.getsize(target + s) for s in ("", "-wal") if os.path.exists(target + s))
    try:
        idx = store.conn.execute("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name LIKE 'search_fts%'").fetchone()[0]
    except Exception:  # noqa: BLE001 - dbstat is an optional SQLite module
        idx = None
    return db, idx


def bench(store: Store, info: dict, target: str = "", runs: int = 200, profile: str = "news") -> dict:
    times: dict = {c: [] for c in QUERY_CLASSES}
    each: dict = {}
    for cls, name, req in query_set(info):
        searchindex.search(store, dict(req))              # warm-up, not timed
        for _ in range(runs):
            t0 = time.perf_counter()
            searchindex.search(store, dict(req))
            ms = (time.perf_counter() - t0) * 1000
            times[cls].append(ms)
            each.setdefault(name, []).append(ms)
    slowest = sorted(((n, _percentile(sorted(v), 0.95)) for n, v in each.items()), key=lambda x: -x[1])[:6]
    db_bytes, index_bytes = sizes(store, target)
    return {"profile": profile, "assets": info["assets"], "documents": info["documents"], "db_bytes": db_bytes, "index_bytes": index_bytes,
            "rss_bytes": rss_bytes(), "slowest_p95_ms": [{"query": n, "p95": v} for n, v in slowest],
            "queries": {c: {"p50": _percentile(sorted(v), 0.5), "p95": _percentile(sorted(v), 0.95), "p99": _percentile(sorted(v), 0.99)} for c, v in times.items()}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="a SQLite path (made if missing, must be empty) or a postgres DSN (a scratch database)")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="news")
    ap.add_argument("--assets", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--analyze", action="store_true", help="run ANALYZE after loading, before timing (SQLite and Postgres planner statistics)")
    args = ap.parse_args(argv)
    store = Store(args.db)
    if store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0]:
        print("refusing to load into a database that already has assets; give an empty one", file=sys.stderr)
        return 2
    t0 = time.perf_counter()
    info = generate(store, args.assets, args.profile, args.seed)
    print(f"generated {info['documents']} documents in {time.perf_counter() - t0:.1f}s", file=sys.stderr)
    if args.analyze:
        store.conn.execute("ANALYZE")
        store.conn.commit()
    out = bench(store, info, args.db, args.runs, args.profile)
    out["analyzed"] = bool(args.analyze)
    print(json.dumps(out, indent=2))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
