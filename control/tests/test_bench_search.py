"""The search bench harness (tests/bench_search.py) and the health `indexing` block. Counts and plans, never wall time."""
import json
import tempfile
import unittest
from pathlib import Path

from tests import bench_search as bench
from tests.pgutil import PgCase
from volt import health, searchindex
from volt.store import Store


def small(assets=300, profile="news", seed=1):
    tmp = tempfile.TemporaryDirectory()
    store = Store(str(Path(tmp.name) / "b.db"))
    info = bench.generate(store, assets, profile, seed)
    return tmp, store, info


class Generator(unittest.TestCase):
    def test_generator_is_deterministic(self):
        sums = []
        for _ in range(2):
            tmp, store, _info = small(40, seed=7)
            sums.append(bench.checksum(store))
            store.close()
            tmp.cleanup()
        tmp, store, _info = small(40, seed=8)
        other = bench.checksum(store)
        store.close()
        tmp.cleanup()
        self.assertEqual(sums[0], sums[1])
        self.assertNotEqual(sums[0], other)

    def test_profiles_differ_in_passages_per_asset(self):
        counts = []
        for profile in ("news", "long"):
            tmp, store, info = small(5, profile)
            counts.append(info["documents"])
            store.close()
            tmp.cleanup()
        self.assertGreater(counts[1], counts[0] * 3)

    def test_unknown_profile_is_refused(self):
        tmp, store, _ = small(1)
        with self.assertRaises(ValueError):
            bench.generate(store, 1, "weekly")
        store.close()
        tmp.cleanup()


class SmallCorpus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp, cls.store, cls.info = small(300)
        cls.queries = bench.query_set(cls.info)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_forty_queries_across_the_nine_classes(self):
        self.assertEqual(len(self.queries), 40)
        self.assertEqual(sorted({c for c, _n, _r in self.queries}), sorted(bench.QUERY_CLASSES))
        self.assertEqual(len({n for _c, n, _r in self.queries}), 40)

    def test_every_query_class_returns_something_at_300_assets(self):
        got = {}
        for cls, name, req in self.queries:
            out = searchindex.search(self.store, dict(req))
            got.setdefault(cls, []).append(out["total"])
        for cls in bench.QUERY_CLASSES:
            if cls == "phrase":      # a phrase of two common words may not occur in a small corpus: it must not error, and some do match
                continue
            self.assertTrue(any(got[cls]), (cls, got[cls]))
        self.assertTrue(all(t > 0 for t in got["rare_term"]), got["rare_term"])

    def test_text_queries_use_the_full_text_index_and_do_not_scan_span_or_the_documents(self):
        if not searchindex.fts_available(self.store):
            self.skipTest("this SQLite has no FTS5")
        for cls in ("single_term", "rare_term", "common_term", "prefix"):
            _c, _n, req = next(q for q in self.queries if q[0] == cls)
            r = searchindex._parse_request(self.store, dict(req))
            where, args = searchindex._asset_where(self.store, r.flt, r.include)
            sql, sargs = searchindex.text_candidates_sql(self.store, r.terms, searchindex.SCOPES[r.scope], r.include, where, args)
            plan = " | ".join(row[3] for row in self.store.conn.execute("EXPLAIN QUERY PLAN " + sql, tuple(sargs)).fetchall())
            self.assertIn("search_fts", plan, cls)
            self.assertNotIn("SCAN d", plan.replace("SCAN m", ""), (cls, plan))
            self.assertNotIn("SCAN span", plan, cls)

    def test_bench_output_shape(self):
        out = bench.bench(self.store, self.info, runs=2)
        self.assertEqual(set(out), {"profile", "assets", "documents", "db_bytes", "index_bytes", "rss_bytes", "queries", "slowest_p95_ms"})
        self.assertEqual(set(out["queries"]), set(bench.QUERY_CLASSES))
        for stats in out["queries"].values():
            self.assertEqual(set(stats), {"p50", "p95", "p99"})
            self.assertLessEqual(stats["p50"], stats["p99"])
        json.dumps(out)


class Health(unittest.TestCase):
    def test_health_reports_indexing_counts(self):
        tmp, store, _info = small(5)
        try:
            idx = health.collect(store, Path(tmp.name))["indexing"]
            self.assertEqual(set(idx), {"ftsAvailable", "assetsIndexed", "documents", "queued", "failed"})
            self.assertEqual(idx["assetsIndexed"], 5)
            self.assertGreater(idx["documents"], 5)
            self.assertEqual((idx["queued"], idx["failed"]), (0, 0))
            a = store.conn.execute("SELECT id FROM asset LIMIT 1").fetchone()[0]
            store.enqueue(a, "search", "k1")
            self.assertEqual(health.collect(store, Path(tmp.name))["indexing"]["queued"], 1)
        finally:
            store.close()
            tmp.cleanup()


class PostgresBench(PgCase):
    def test_generator_and_every_class_on_postgres(self):
        store = self.store()
        info = bench.generate(store, 60, "news", 3)
        for cls, name, req in bench.query_set(info):
            searchindex.search(store, dict(req))
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 60)
        db_bytes, index_bytes = bench.sizes(store, "")
        self.assertGreater(db_bytes, 0)
        self.assertGreaterEqual(index_bytes, 0)

    def test_postgres_and_sqlite_find_the_same_assets_for_the_same_corpus(self):
        pg = self.store()
        info = bench.generate(pg, 60, "news", 3)
        tmp, lite, info2 = small(60, seed=3)
        try:
            for cls, name, req in bench.query_set(info):
                if cls in ("prefix", "phrase"):          # the backends differ in how they cut a prefix of a pseudo-word; the suite's own parity tests cover prefix and phrase
                    continue
                a = {x["assetId"] for x in searchindex.search(pg, dict(req, limit=100))["assets"]}
                b = {x["assetId"] for x in searchindex.search(lite, dict(req, limit=100))["assets"]}
                self.assertEqual(a, b, name)
        finally:
            lite.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
