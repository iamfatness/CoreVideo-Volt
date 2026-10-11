"""Hardening: open-transaction hygiene, connection pooling, and concurrent writers.

The transaction-hygiene and concurrency tests run on SQLite always and on Postgres when VOLT_TEST_PG_DSN is
set, because the rules (one decision at a time, no transaction left open) are the same on both. The pool tests
need Postgres and psycopg_pool.

Concurrent tests give every thread its own Store, as the server does per request, and release them together
from a barrier so they really overlap. A hung join is a failure (a deadlock), not a hang of the suite.
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from volt import cues, library, pg, playout, rundown, session
from volt.runner import Claimed, Runner
from volt.store import HoldError, NotClearedError, Store

from tests.pgutil import DSN, PgCase, have_pool

JOIN = 60


def run_threads(n, work, after=None):
    """Run work(i) on n threads started together. Returns (results by index, exceptions).

    `after` runs on the same thread when work ends (SQLite connections close only on their own thread)."""
    barrier = threading.Barrier(n)
    results, errors = [None] * n, []

    def body(i):
        try:
            barrier.wait(30)
            results[i] = work(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            if after:
                after()

    threads = [threading.Thread(target=body, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN)
    stuck = [t for t in threads if t.is_alive()]
    if stuck:
        raise AssertionError(f"{len(stuck)} thread(s) still running after {JOIN}s: deadlock")
    return results, errors


class Backend:
    """Mixin: self.target is a SQLite path or a DSN; self.open() makes a Store on it."""

    _local = threading.local()

    def open(self, **kw):
        store = Store(self.target, **kw)
        if threading.current_thread() is threading.main_thread():
            self.opened.append(store)
        else:  # closed by the thread that made it (see parallel)
            self._local.__dict__.setdefault("stores", []).append(store)
        return store

    def close_mine(self):
        for s in self._local.__dict__.pop("stores", []):
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

    def parallel(self, n, work):
        return run_threads(n, work, after=self.close_mine)

    def close_all(self):
        for s in self.opened:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

    # a show of `n` items whose media is cleared, so every item can air
    def make_show(self, store, n):
        asset = store.mint_asset(owner="desk")
        store.set_cleared(asset.id, True)
        show = rundown.open_show(store, "Show")
        ids = []
        for i in range(n):
            span = store.add_span(asset.id, "clip", f"01:00:0{i}:00", f"01:00:0{i}:10")
            item = rundown.add_item(store, show, f"item-{i}")
            rundown.attach(store, item, span)
            ids.append(item)
        return show, ids


class Hygiene(Backend):
    """After every call the connection must be out of any transaction. A leaked one holds locks."""

    def check(self, store, label, fn):
        out = fn()
        self.assertFalse(store.conn.in_transaction, f"{label} left a transaction open")
        return out

    def test_no_store_or_module_call_leaves_a_transaction_open(self):
        store = self.open()
        c = lambda label, fn: self.check(store, label, fn)  # noqa: E731

        asset = c("mint_asset", lambda: store.mint_asset(owner="desk", show_name="s", camera="c1"))
        c("get_asset", lambda: store.get_asset(asset.id))
        with self.assertRaises(KeyError):
            c("get_asset (missing)", lambda: store.get_asset("nope"))
        self.assertFalse(store.conn.in_transaction)
        c("find_by_source (none)", lambda: store.find_by_source("k"))
        c("mint_source (new)", lambda: store.mint_source("k", owner="desk"))
        c("mint_source (existing)", lambda: store.mint_source("k", owner="desk"))
        c("find_by_source (hit)", lambda: store.find_by_source("k"))
        c("set_hold", lambda: store.set_hold(asset.id, True))
        with self.assertRaises(HoldError):
            c("delete_asset (held)", lambda: store.delete_asset(asset.id))
        self.assertFalse(store.conn.in_transaction)
        with self.assertRaises(NotClearedError):
            c("assert_publishable (held)", lambda: store.assert_publishable(asset.id))
        self.assertFalse(store.conn.in_transaction)
        c("set_hold off", lambda: store.set_hold(asset.id, False))
        c("set_cleared", lambda: store.set_cleared(asset.id, True))
        c("assert_publishable", lambda: store.assert_publishable(asset.id))
        essence = c("add_essence", lambda: store.add_essence(asset.id, "hi-res", "a.mxf", open_file=True))
        c("wrap_essence", lambda: store.wrap_essence(essence, "abc"))
        span = c("add_span", lambda: store.add_span(asset.id, "clip", "01:00:00:00", "01:00:05:00"))
        job = c("enqueue (new)", lambda: store.enqueue(asset.id, "proxy", "p1"))
        c("enqueue (existing)", lambda: store.enqueue(asset.id, "proxy", "p1"))
        c("get_job", lambda: store.get_job(job.id))
        c("fail_job", lambda: store.fail_job(job.id, "boom"))
        c("enqueue (failed -> queued)", lambda: store.enqueue(asset.id, "proxy", "p1"))
        c("complete_job", lambda: store.complete_job(job.id))
        c("delete_asset", lambda: store.delete_asset(asset.id))

        show = c("open_show", lambda: rundown.open_show(store, "Show"))
        item = c("add_item", lambda: rundown.add_item(store, show, "one"))
        item2 = c("add_item 2", lambda: rundown.add_item(store, show, "two"))
        c("attach", lambda: rundown.attach(store, item, span))
        c("attach 2", lambda: rundown.attach(store, item2, span))
        c("items", lambda: rundown.items(store, show))
        c("reorder", lambda: rundown.reorder(store, show, [item2, item]))
        c("hold_state", lambda: playout.hold_state(store, show))
        c("hold", lambda: playout.hold(store, show, "why"))
        c("hold (again)", lambda: playout.hold(store, show, "again"))
        with self.assertRaises(playout.HeldError):
            c("go (held)", lambda: playout.go(store, show))
        self.assertFalse(store.conn.in_transaction)
        c("release", lambda: playout.release(store, show))
        c("release (not held)", lambda: playout.release(store, show))
        c("add_cue", lambda: cues.add_cue(store, item, "other", "x.y", "{}"))
        c("cues", lambda: cues.cues(store, item))
        c("arm_cues", lambda: cues.arm_cues(store, item))
        adv = c("advance", lambda: playout.advance(store, show))
        c("on_air", lambda: playout.on_air(store, show))
        c("rewind", lambda: playout.rewind(store, adv))
        c("take", lambda: rundown.take(store, item))
        with self.assertRaises(playout.PlayoutError):
            c("skip (on air)", lambda: playout.skip(store, item))
        self.assertFalse(store.conn.in_transaction)
        c("skip", lambda: playout.skip(store, item2))
        c("kill", lambda: rundown.kill(store, item2))

        c("index_words", lambda: library.index_words(store, asset.id, [("hello", "01:00:00:00", "01:00:01:00")]))
        c("search", lambda: library.search(store, "hel"))
        c("list_assets", lambda: session.list_assets(store))
        c("rundown (session)", lambda: session.rundown(store, show))
        c("seek_hit", lambda: session.seek_hit(store, "hel"))

        runner = Runner(store, {"proxy": lambda st, job: None})
        store.enqueue(asset.id, "proxy", "p2")
        job2 = c("claim", lambda: runner.claim())
        self.assertIsNotNone(job2)
        c("run_job", lambda: runner.run_job(job2))
        c("recover_expired", lambda: runner.recover_expired())
        c("drain", lambda: runner.drain())

    def test_an_open_reader_does_not_block_another_connections_schema_bootstrap(self):
        # The A1 finding: enqueue's trailing read left a transaction open, and the next connection's
        # bootstrap (ALTER TABLE) waited on it forever.
        store = self.open()
        asset = store.mint_asset(owner="desk")
        store.enqueue(asset.id, "proxy", "p1")
        store.get_job(store.enqueue(asset.id, "proxy", "p1").id)
        _r, errors = self.parallel(1, lambda _i: self.open().get_asset(asset.id))
        self.assertEqual(errors, [])


class Races(Backend):
    def test_mint_race_makes_one_asset(self):
        def work(_i):
            store = self.open()
            return store.mint_source("rec:1", owner="desk").id

        ids, errors = self.parallel(12, work)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(self.open().conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)

    def test_enqueue_race_makes_one_job(self):
        store = self.open()
        asset = store.mint_asset(owner="desk")

        def work(_i):
            return self.open().enqueue(asset.id, "proxy", "proxy-v1").id

        ids, errors = self.parallel(12, work)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM job").fetchone()[0], 1)

    def test_requeueing_a_failed_job_counts_one_attempt_however_many_ask(self):
        store = self.open()
        asset = store.mint_asset(owner="desk")
        job = store.enqueue(asset.id, "proxy", "proxy-v1")
        store.fail_job(job.id, "encoder died")

        def work(_i):
            return self.open().enqueue(asset.id, "proxy", "proxy-v1")

        jobs, errors = self.parallel(12, work)
        self.assertEqual(errors, [])
        self.assertEqual({(j.id, j.status) for j in jobs}, {(job.id, "queued")})
        self.assertEqual(store.get_job(job.id).attempt, 2)

    def test_two_runners_never_run_one_job_twice(self):
        store = self.open()
        asset = store.mint_asset(owner="desk")
        want = {store.enqueue(asset.id, "proxy", f"proxy:{n}").id for n in range(36)}
        ran, lock = [], threading.Lock()

        def handler(_store, job: Claimed):
            with lock:
                ran.append(job.id)

        def work(_i):
            runner = Runner(self.open(), {"proxy": handler}, target=self.target, lease=60.0)
            return runner.drain()

        _counts, errors = self.parallel(6, work)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ran), sorted(want))  # every job once, none twice
        self.assertEqual({r[0] for r in store.conn.execute("SELECT status FROM job")}, {"done"})

    def test_a_dead_runners_job_is_taken_by_exactly_one_other(self):
        store = self.open()
        asset = store.mint_asset(owner="desk")
        job = store.enqueue(asset.id, "proxy", "proxy:1")
        dead = Runner(store, {"proxy": lambda *_: None}, name="dead", lease=0.01)
        self.assertIsNotNone(dead.claim())
        time.sleep(1.1)  # timestamps are whole seconds; let the lease lapse
        ran, lock = [], threading.Lock()

        def handler(_s, claimed):
            with lock:
                ran.append(claimed.id)

        def work(i):
            return Runner(self.open(), {"proxy": handler}, name=f"live{i}", target=self.target).drain()

        _c, errors = self.parallel(5, work)
        self.assertEqual(errors, [])
        self.assertEqual(ran, [job.id])
        self.assertEqual(store.get_job(job.id).status, "done")

    def test_add_item_race_gives_distinct_consecutive_positions(self):
        store = self.open()
        show = rundown.open_show(store, "Show")

        def work(i):
            return rundown.add_item(self.open(), show, f"item-{i}")

        _ids, errors = self.parallel(12, work)
        self.assertEqual(errors, [])
        self.assertEqual([i.position for i in rundown.items(store, show)], list(range(1, 13)))

    def test_add_cue_race_gives_distinct_consecutive_positions(self):
        store = self.open()
        show = rundown.open_show(store, "Show")
        item = rundown.add_item(store, show, "one")

        def work(i):
            return cues.add_cue(self.open(), item, "other", f"cmd.{i}", "{}")

        _ids, errors = self.parallel(10, work)
        self.assertEqual(errors, [])
        positions = [r[0] for r in store.conn.execute("SELECT position FROM cue ORDER BY position")]
        self.assertEqual(positions, list(range(1, 11)))

    def test_opposed_reorders_do_not_deadlock_or_mix(self):
        store = self.open()
        show, ids = self.make_show(store, 6)
        forward, backward = ids, list(reversed(ids))

        def work(i):
            mine = self.open()
            for _ in range(20):
                rundown.reorder(mine, show, forward if i == 0 else backward)

        _r, errors = self.parallel(2, work)
        self.assertEqual(errors, [])
        order = [i.id for i in rundown.items(store, show)]
        self.assertIn(order, (forward, backward))  # one reorder won whole; no interleaved order

    def test_concurrent_go_takes_each_item_once(self):
        store = self.open()
        show, ids = self.make_show(store, 8)

        def work(_i):
            return playout.go(self.open(), show)

        targets, errors = self.parallel(5, work)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(targets), sorted(ids[:5]))  # five goes, five different items
        status = {i.id: i.status for i in rundown.items(store, show)}
        self.assertEqual([status[i] for i in ids], ["aired"] * 4 + ["on air"] + ["ready"] * 3)
        rows = {r["id"]: r for r in store.conn.execute("SELECT id, as_run_in, as_run_out FROM item")}
        for n, item in enumerate(ids[:5]):
            self.assertIsNotNone(rows[item]["as_run_in"], f"item {n} has no as-run in")
            self.assertEqual(rows[item]["as_run_out"] is not None, n < 4, f"item {n} as-run out")

    def test_concurrent_takes_leave_exactly_one_item_on_air(self):
        store = self.open()
        show, ids = self.make_show(store, 6)

        def work(i):
            rundown.take(self.open(), ids[i])

        _r, errors = self.parallel(6, work)
        self.assertEqual(errors, [])
        on_air = [i.id for i in rundown.items(store, show) if i.status == "on air"]
        self.assertEqual(len(on_air), 1)

    def test_go_and_hold_at_once_never_half_apply(self):
        store = self.open()
        show, ids = self.make_show(store, 4)

        def work(i):
            mine = self.open()
            if i == 0:
                playout.hold(mine, show, "stop")
                return "hold"
            try:
                return playout.go(mine, show)
            except playout.HeldError:
                return "held"

        results, errors = self.parallel(4, work)
        self.assertEqual(errors, [])
        taken = [r for r in results if r in ids]
        self.assertEqual(len(taken), len(set(taken)))
        on_air = [i for i in rundown.items(store, show) if i.status == "on air"]
        self.assertEqual(len(on_air), 1 if taken else 0)
        self.assertIsNotNone(playout.hold_state(store, show))

    def test_concurrent_holds_keep_the_first(self):
        store = self.open()
        show, _ids = self.make_show(store, 2)

        def work(i):
            return playout.hold(self.open(), show, f"reason-{i}")

        holds, errors = self.parallel(8, work)
        self.assertEqual(errors, [])
        stored = playout.hold_state(store, show)
        self.assertEqual({(h.reason, h.held_at) for h in holds}, {(stored.reason, stored.held_at)})

    def test_taking_in_one_show_leaves_another_shows_item_on_air(self):
        store = self.open()
        show_a, a = self.make_show(store, 2)
        show_b, b = self.make_show(store, 2)
        rundown.take(store, a[0])
        rundown.take(store, b[0])
        self.assertEqual(playout.on_air(store, show_a), a[0])
        self.assertEqual(playout.on_air(store, show_b), b[0])

    def test_a_transaction_that_raises_leaves_nothing_behind(self):
        store = self.open()
        show = rundown.open_show(store, "Show")
        with self.assertRaises(RuntimeError):
            with store.transaction(f"show:{show}"):
                store.conn.execute("UPDATE show SET title = 'changed' WHERE id = ?", (show,))
                raise RuntimeError("boom")
        self.assertEqual(store.conn.execute("SELECT title FROM show WHERE id = ?", (show,)).fetchone()[0], "Show")
        self.assertFalse(store.conn.in_transaction)

    def test_a_commit_inside_a_transaction_waits_for_the_end(self):
        store = self.open()
        show = rundown.open_show(store, "Show")
        other = self.open()
        with store.transaction():
            store.conn.execute("UPDATE show SET title = 'inside' WHERE id = ?", (show,))
            store.conn.commit()  # what take() does at its own end; must not publish half a unit
            self.assertEqual(other.conn.execute("SELECT title FROM show WHERE id = ?", (show,)).fetchone()[0], "Show")
        self.assertEqual(other.conn.execute("SELECT title FROM show WHERE id = ?", (show,)).fetchone()[0], "inside")


class SqliteMixin:
    postgres_target = False

    def setUp(self):
        self.opened = []
        self.tmp = tempfile.TemporaryDirectory()
        self.target = str(Path(self.tmp.name) / "volt.db")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.close_all)


class SqliteHygiene(SqliteMixin, Hygiene, unittest.TestCase):
    pass


class SqliteRaces(SqliteMixin, Races, unittest.TestCase):
    pass


class PgMixin:
    postgres_target = True

    def setUp(self):
        super().setUp()
        self.opened = self.stores
        self.target = self.dsn
        self.addCleanup(self.close_all)


class PostgresHygiene(PgMixin, Hygiene, PgCase):
    pass


class PostgresRaces(PgMixin, Races, PgCase):
    pass


@unittest.skipUnless(DSN and have_pool(), "needs VOLT_TEST_PG_DSN and psycopg_pool")
class PostgresPool(PgCase):
    def pool(self, **kw):
        kw.setdefault("max_size", 3)
        kw.setdefault("min_size", 1)
        kw.setdefault("timeout", 5.0)
        return pg.configure_pool(self.dsn, **kw)

    def backends(self):
        with self.admin().cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND application_name = 'volt-pool' AND pid != pg_backend_pid()")
            return cur.fetchone()[0]

    def test_requests_share_a_bounded_set_of_connections(self):
        self.store()  # schema first, unpooled
        pool = self.pool(max_size=3)

        def work(i):
            store = Store(self.dsn, init=False)
            try:
                store.mint_asset(owner="desk", show_name=f"s{i}")
                return store.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            finally:
                store.close()

        pids, errors = run_threads(12, work)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(set(pids)), 3)
        self.assertEqual(self.store(init=False).conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 12)
        self.assertLessEqual(pool.stats()["pool_size"], 3)

    def test_schema_is_applied_once_at_startup_not_per_checkout(self):
        self.store()
        self.pool()
        statements = []
        original = pg.PgConnection.apply_schema
        pg.PgConnection.apply_schema = lambda self_: statements.append(1) or original(self_)
        self.addCleanup(setattr, pg.PgConnection, "apply_schema", original)
        for _ in range(5):
            Store(self.dsn, init=False).close()
        self.assertEqual(statements, [])

    def test_a_connection_the_server_dropped_is_replaced_not_handed_out(self):
        self.store()
        self.pool(max_size=2, min_size=2)
        first = Store(self.dsn, init=False)
        pid = first.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        first.close()
        admin = self.admin()
        # Kill every pooled backend (the pool's idle connections), as a server restart or idle-kill would.
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE pid != pg_backend_pid() AND application_name = 'volt-pool'")
        time.sleep(0.3)
        second = Store(self.dsn, init=False)
        try:
            self.assertEqual(second.conn.execute("SELECT 1").fetchone()[0], 1)
            second.mint_asset(owner="desk")
            self.assertNotEqual(second.conn.execute("SELECT pg_backend_pid()").fetchone()[0], pid)
        finally:
            second.close()

    def test_a_dropped_connection_mid_use_is_discarded_on_return(self):
        self.store()
        self.pool(max_size=1, min_size=1)
        store = Store(self.dsn, init=False)
        pid = store.conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        self.admin().execute("SELECT pg_terminate_backend(%s)", (pid,))
        time.sleep(0.2)
        import psycopg
        with self.assertRaises(psycopg.OperationalError):
            store.conn.execute("SELECT 1")
        store.close()
        again = Store(self.dsn, init=False)  # the single slot was reconnected, not left broken
        try:
            self.assertEqual(again.conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            again.close()

    def test_a_returned_connection_is_clean(self):
        self.store()
        self.pool(max_size=1, min_size=1)
        store = Store(self.dsn, init=False)
        store.conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s', 'x', now())")  # never committed
        store.close()
        nxt = Store(self.dsn, init=False)
        try:
            self.assertFalse(nxt.conn.in_transaction)
            self.assertIsNone(nxt.conn.execute("SELECT id FROM show WHERE id = 's'").fetchone())
        finally:
            nxt.close()

    def test_waiting_for_a_free_connection_times_out_loudly(self):
        self.store()
        self.pool(max_size=1, min_size=1, timeout=0.5)
        held = Store(self.dsn, init=False)
        try:
            from psycopg_pool import PoolTimeout
            started = time.monotonic()
            with self.assertRaises(PoolTimeout):
                Store(self.dsn, init=False)
            self.assertLess(time.monotonic() - started, 5)
        finally:
            held.close()
        Store(self.dsn, init=False).close()  # and the slot is usable again

    def test_a_failed_bootstrap_returns_its_connection_to_the_pool(self):
        self.pool(max_size=1, min_size=1, timeout=1.0)
        original = pg.SCHEMA_FILE
        bad = Path(tempfile.mkdtemp()) / "bad.sql"
        bad.write_text("THIS IS NOT SQL;")
        pg.SCHEMA_FILE = bad
        try:
            with self.assertRaises(Exception):
                Store(self.dsn)
        finally:
            pg.SCHEMA_FILE = original
        Store(self.dsn).close()  # would time out if the failed Store had kept the only slot

    def test_closing_the_pools_closes_every_connection(self):
        self.store()
        self.pool(max_size=2, min_size=2)
        Store(self.dsn, init=False).close()
        pg.close_pools()
        self.assertIsNone(pg.pool_for(self.dsn))
        time.sleep(0.5)
        self.assertEqual(self.backends(), 0)

    def test_reconfiguring_replaces_the_pool(self):
        self.store()
        first = self.pool(max_size=2)
        second = self.pool(max_size=4)
        self.assertIs(pg.pool_for(self.dsn), second)
        self.assertIsNot(first, second)
        Store(self.dsn, init=False).close()

    def test_a_store_close_is_idempotent(self):
        self.store()
        self.pool(max_size=1, min_size=1)
        store = Store(self.dsn, init=False)
        store.close()
        store.close()
        Store(self.dsn, init=False).close()

    def test_concurrent_writes_through_the_pool_stay_correct(self):
        self.store()
        self.pool(max_size=4, min_size=2)
        show = rundown.open_show(self.store(init=False), "Show")

        def work(i):
            store = Store(self.dsn, init=False)
            try:
                return rundown.add_item(store, show, f"i{i}")
            finally:
                store.close()

        _ids, errors = run_threads(16, work)
        self.assertEqual(errors, [])
        self.assertEqual([i.position for i in rundown.items(self.store(init=False), show)], list(range(1, 17)))


class PoolConfig(unittest.TestCase):
    def test_off_unless_asked_for(self):
        self.assertEqual(pg.pool_options({}), {})
        self.assertEqual(pg.pool_options({"VOLT_PG_POOL_MAX": "0"}), {})

    def test_environment_sets_size_and_timeouts(self):
        env = {"VOLT_PG_POOL_MAX": "8", "VOLT_PG_POOL_MIN": "2", "VOLT_PG_POOL_TIMEOUT": "7.5",
               "VOLT_PG_POOL_MAX_IDLE": "60", "VOLT_PG_POOL_MAX_LIFETIME": "900"}
        self.assertEqual(pg.pool_options(env), {"max_size": 8, "min_size": 2, "timeout": 7.5,
                                                "max_idle": 60.0, "max_lifetime": 900.0})

    def test_an_explicit_option_beats_the_environment(self):
        self.assertEqual(pg.pool_options({"VOLT_PG_POOL_MAX": "8"}, max_size=3)["max_size"], 3)
        self.assertEqual(pg.pool_options({}, max_size=3), {"max_size": 3})

    def test_sqlite_never_gets_a_pool(self):
        self.assertIsNone(pg.configure_pool_from_env("volt.db", {"VOLT_PG_POOL_MAX": "8"}))

    def test_a_pool_without_psycopg_pool_says_what_to_install(self):
        import sys
        from unittest import mock
        with mock.patch.dict(sys.modules, {"psycopg_pool": None}):
            with self.assertRaises(RuntimeError) as ctx:
                pg.Pool("postgresql://x@localhost/none")
        self.assertIn("psycopg_pool", str(ctx.exception))

    def test_sqlite_path_never_imports_psycopg_pool(self):
        import sys
        had = "psycopg_pool" in sys.modules
        Store().close()
        self.assertEqual("psycopg_pool" in sys.modules, had)


class ReadClassification(unittest.TestCase):
    def test_what_ends_a_transaction_after_itself(self):
        plain = pg._is_plain_read
        self.assertTrue(plain("SELECT * FROM asset WHERE id = ?"))
        self.assertTrue(plain("  select count(*) from job"))
        self.assertFalse(plain("SELECT id FROM job FOR UPDATE SKIP LOCKED"))
        self.assertFalse(plain("INSERT INTO asset VALUES (?)"))
        self.assertFalse(plain("UPDATE job SET status = 'x' RETURNING id"))
        self.assertFalse(plain("SELECT pg_advisory_xact_lock(1)"))
        self.assertFalse(plain("WITH x AS (DELETE FROM job RETURNING 1) SELECT * FROM x"))


if __name__ == "__main__":
    unittest.main()
