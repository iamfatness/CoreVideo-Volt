"""Data safety: version-skew guard, backup/restore/verify, and asset delete (tombstone + purge).

SQLite always. The Postgres cases run when VOLT_TEST_PG_DSN is set; the pg_dump/pg_restore ones also need those tools
(PATH, the usual install folders, or VOLT_PG_DUMP / VOLT_PG_RESTORE)."""

import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from volt import backup, migrations, retention, runner as runner_mod, serve, storage
from volt.rundown import add_item, attach, open_show
from volt.runner import Config, Runner, default_handlers
from volt.store import HoldError, Store

PG_DSN = os.environ.get("VOLT_TEST_PG_DSN")


def _tools_present() -> bool:
    try:
        backup.find_pg_tool("pg_dump")
        backup.find_pg_tool("pg_restore")
    except backup.BackupError:
        return False
    return True


class Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def db(self, name="volt.db"):
        store = Store(str(self.dir / name))
        self.addCleanup(lambda: store.conn.close() if _open(store) else None)
        return store


def _open(store) -> bool:
    try:
        store.conn.execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


def populate(store: Store) -> dict:
    """A small but complete library: two assets, essences, spans, a show with an item, a job."""
    a = store.mint_asset(owner="desk", show_name="final-four", camera="iso-1", source_key="k1")
    b = store.mint_asset(owner="desk", show_name="final-four", source_key="k2")
    e1 = store.add_essence(a.id, "hi-res", "/media/a.mxf", checksum="abc", frame_rate="25")
    store.add_essence(a.id, "proxy", "/proxy/a.mp4")
    store.add_essence(b.id, "hi-res", "/media/b.mxf")
    s1 = store.add_span(a.id, "clip", "01:00:00:00", "01:00:05:00", text="hello", label="intro")
    store.add_span(a.id, "transcript", "01:00:00:00", "01:00:01:00", text="welcome")
    show = open_show(store, "Friday")
    item = add_item(store, show, "Open")
    store.enqueue(a.id, "proxy", "proxy-v1")
    return {"a": a.id, "b": b.id, "e1": e1, "s1": s1, "show": show, "item": item}


def dump(path: str) -> dict:
    """Every table's rows, sorted, for comparing two databases."""
    conn = sqlite3.connect(path)
    try:
        out = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            out[name] = sorted(map(repr, conn.execute(f'SELECT * FROM "{name}"').fetchall()))
        return out
    finally:
        conn.close()


# ---- version-skew guard --------------------------------------------------------------------------------

class VersionSkew(Tmp):
    def test_a_newer_database_is_refused_naming_both_versions(self):
        path = str(self.dir / "volt.db")
        Store(path).close()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (99, 'future', 'x')")
        conn.commit()
        conn.close()
        with self.assertRaises(migrations.SchemaTooNewError) as ctx:
            Store(path)
        text = str(ctx.exception)
        self.assertIn("version 99", text)
        self.assertIn(f"version {migrations.known_version()}", text)
        self.assertEqual((ctx.exception.database, ctx.exception.code), (99, migrations.known_version()))

    def test_a_refused_open_changes_nothing(self):
        path = str(self.dir / "volt.db")
        Store(path).close()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (99, 'future', 'x')")
        conn.commit()
        conn.close()
        before = dump(path)
        with self.assertRaises(migrations.SchemaTooNewError):
            Store(path)
        self.assertEqual(dump(path), before)

    def test_an_older_database_is_migrated_as_before(self):
        path = str(self.dir / "volt.db")
        Store(path).close()
        conn = sqlite3.connect(path)
        conn.execute("DELETE FROM schema_version WHERE version >= 9")
        conn.commit()
        conn.close()
        store = Store(path)
        self.assertEqual(migrations.current_version(migrations.SqliteBackend(store.conn)), migrations.known_version())
        store.close()

    def test_serve_refuses_to_start(self):
        path = str(self.dir / "volt.db")
        Store(path).close()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (99, 'future', 'x')")
        conn.commit()
        conn.close()
        with mock.patch.object(serve, "DB_PATH", path), mock.patch.object(serve, "TOKEN", None), \
                mock.patch.dict(os.environ, {"VOLT_PORT": "0"}):
            with self.assertRaises(SystemExit) as ctx, mock.patch("sys.stderr") as err:
                serve.main([])
        self.assertEqual(ctx.exception.code, serve.EX_CONFIG)  # 78: `volt run` does not retry it
        self.assertIn("version 99", "".join(c.args[0] for c in err.write.call_args_list))

    def test_the_runner_refuses_to_start(self):
        path = str(self.dir / "volt.db")
        Store(path).close()
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (99, 'future', 'x')")
        conn.commit()
        conn.close()
        with mock.patch("sys.stderr") as err:
            code = runner_mod.main([path, "--once"])
        self.assertEqual(code, 78)
        self.assertIn("version 99", "".join(c.args[0] for c in err.write.call_args_list))


# ---- backup and restore --------------------------------------------------------------------------------

class BackupRestore(Tmp):
    def test_backup_then_restore_to_a_fresh_database_is_identical(self):
        store = self.db()
        populate(store)
        manifest = backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        self.assertEqual(manifest["kind"], "sqlite")
        self.assertEqual(manifest["schema_version"], migrations.known_version())
        self.assertEqual(manifest["row_counts"]["asset"], 2)
        self.assertEqual(manifest["integrity_check"], "ok")
        self.assertEqual(len(manifest["sha256"]), 64)
        self.assertEqual(backup.verify_backup(self.dir / "bk")["sha256"], manifest["sha256"])
        report = backup.restore(self.dir / "bk", str(self.dir / "fresh.db"))
        self.assertIsNone(report["moved_aside"])
        self.assertEqual(dump(str(self.dir / "fresh.db")), dump(str(self.dir / "volt.db")))
        again = Store(str(self.dir / "fresh.db"))  # and it opens as a normal database
        self.assertEqual(again.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 2)
        again.close()

    def test_backup_while_a_writer_is_active_is_consistent(self):
        store = self.db()
        a = store.mint_asset(owner="desk")
        stop = threading.Event()
        errors = []

        def writer():
            w = Store(str(self.dir / "volt.db"))
            try:
                n = 0
                while not stop.is_set():
                    w.add_span(a.id, "clip", "01:00:00:00", "01:00:01:00", label=str(n))
                    n += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                w.close()

        t = threading.Thread(target=writer)
        t.start()
        try:
            for i in range(3):
                backup.backup(str(self.dir / "volt.db"), str(self.dir / f"bk{i}"))
                backup.verify_backup(self.dir / f"bk{i}")
        finally:
            stop.set()
            t.join()
        self.assertEqual(errors, [])

    def test_a_corrupt_backup_is_refused_and_nothing_is_written(self):
        populate(self.db())
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        data = self.dir / "bk" / backup.SQLITE_FILE
        raw = bytearray(data.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        data.write_bytes(bytes(raw))
        with self.assertRaisesRegex(backup.BackupError, "corrupt|SHA-256"):
            backup.verify_backup(self.dir / "bk")
        with self.assertRaises(backup.BackupError):
            backup.restore(self.dir / "bk", str(self.dir / "fresh.db"))
        self.assertFalse((self.dir / "fresh.db").exists())

    def test_a_truncated_or_manifestless_backup_is_refused(self):
        populate(self.db())
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        data = self.dir / "bk" / backup.SQLITE_FILE
        data.write_bytes(data.read_bytes()[:-100])
        with self.assertRaisesRegex(backup.BackupError, "bytes"):
            backup.verify_backup(self.dir / "bk")
        (self.dir / "bk" / backup.MANIFEST).unlink()
        with self.assertRaisesRegex(backup.BackupError, "not a complete backup"):
            backup.verify_backup(self.dir / "bk")

    def test_a_tampered_manifest_is_refused(self):
        populate(self.db())
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        path = self.dir / "bk" / backup.MANIFEST
        m = json.loads(path.read_text())
        m["row_counts"]["asset"] = 99
        path.write_text(json.dumps(m))
        with self.assertRaisesRegex(backup.BackupError, "row counts"):
            backup.verify_backup(self.dir / "bk")

    def test_restore_never_overwrites_a_database_with_data_without_the_flag(self):
        populate(self.db())
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        other = self.db("other.db")
        other.mint_asset(owner="precious")
        other.close()
        before = dump(str(self.dir / "other.db"))
        with self.assertRaisesRegex(backup.BackupError, "refusing to overwrite"):
            backup.restore(self.dir / "bk", str(self.dir / "other.db"))
        self.assertEqual(dump(str(self.dir / "other.db")), before)
        report = backup.restore(self.dir / "bk", str(self.dir / "other.db"), overwrite=True)
        self.assertTrue(Path(report["moved_aside"]).is_file())  # the old database is kept, not destroyed
        self.assertEqual(dump(str(self.dir / "other.db")), dump(str(self.dir / "volt.db")))
        self.assertEqual(dump(report["moved_aside"]), before)

    def test_a_garbage_target_is_not_overwritten_silently(self):
        populate(self.db())
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        (self.dir / "junk.db").write_bytes(b"this is not a database at all" * 10)
        with self.assertRaisesRegex(backup.BackupError, "not a readable"):
            backup.restore(self.dir / "bk", str(self.dir / "junk.db"))

    def test_an_empty_existing_database_can_be_restored_into(self):
        populate(self.db())
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        Store(str(self.dir / "empty.db")).close()  # schema only, no rows of volt data
        # schema_version has rows, so this counts as data: the safe reading
        with self.assertRaises(backup.BackupError):
            backup.restore(self.dir / "bk", str(self.dir / "empty.db"))
        (self.dir / "zero.db").write_bytes(b"")
        backup.restore(self.dir / "bk", str(self.dir / "zero.db"))
        self.assertEqual(dump(str(self.dir / "zero.db")), dump(str(self.dir / "volt.db")))

    def test_an_older_schema_is_migrated_forward_on_restore(self):
        populate(self.db())
        conn = sqlite3.connect(str(self.dir / "volt.db"))
        conn.execute("ALTER TABLE asset DROP COLUMN deleted_at")
        conn.execute("DROP TABLE purge_audit")
        conn.execute("DELETE FROM schema_version WHERE version >= 11")
        conn.commit()
        conn.close()
        manifest = backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        self.assertEqual(manifest["schema_version"], 10)
        report = backup.restore(self.dir / "bk", str(self.dir / "new.db"))
        self.assertEqual(report["migrated"], list(range(11, backup.migrations.LATEST + 1)))
        store = Store(str(self.dir / "new.db"))
        cols = {r[1] for r in store.conn.execute("PRAGMA table_info(asset)")}
        self.assertIn("deleted_at", cols)
        self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 2)
        store.close()

    def test_a_backup_newer_than_the_code_is_refused(self):
        populate(self.db())
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk"))
        path = self.dir / "bk" / backup.MANIFEST
        data = self.dir / "bk" / backup.SQLITE_FILE
        conn = sqlite3.connect(str(data))
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (99, 'future', 'x')")
        conn.commit()
        conn.close()
        m = json.loads(path.read_text())
        m["schema_version"] = 99
        m["row_counts"] = {k: v for k, v in m["row_counts"].items()}
        m["row_counts"]["schema_version"] += 1
        m["sha256"], m["size"] = backup._sha256(data)
        path.write_text(json.dumps(m))
        with self.assertRaises(migrations.SchemaTooNewError):
            backup.restore(self.dir / "bk", str(self.dir / "fresh.db"))
        self.assertFalse((self.dir / "fresh.db").exists())

    def test_backup_refuses_a_missing_source_and_a_used_destination(self):
        with self.assertRaisesRegex(backup.BackupError, "no SQLite database"):
            backup.backup(str(self.dir / "nope.db"), str(self.dir / "bk"))
        self.assertFalse((self.dir / "bk" / backup.MANIFEST).exists())
        populate(self.db())
        (self.dir / "used").mkdir()
        (self.dir / "used" / "x").write_text("keep me")
        with self.assertRaisesRegex(backup.BackupError, "never overwrites"):
            backup.backup(str(self.dir / "volt.db"), str(self.dir / "used"))
        self.assertEqual((self.dir / "used" / "x").read_text(), "keep me")

    def test_cli(self):
        populate(self.db())
        self.assertEqual(backup.main(["backup", str(self.dir / "volt.db"), str(self.dir / "bk")]), 0)
        self.assertEqual(backup.main(["verify", str(self.dir / "bk")]), 0)
        self.assertEqual(backup.main(["restore", str(self.dir / "bk"), str(self.dir / "r.db")]), 0)
        self.assertEqual(backup.main(["restore", str(self.dir / "bk"), str(self.dir / "r.db")]), 2)  # now has data

    def test_backup_writes_media_json_listing_the_closed_hi_res(self):
        store = self.db()
        a = store.mint_asset(owner="desk")
        closed = store.add_essence(a.id, "hi-res", "/media/closed.mxf", checksum="abc")
        store.add_essence(a.id, "hi-res", "/media/open.mxf", open_file=True, checksum="def")
        dest = self.dir / "bk"
        backup.backup(str(self.dir / "volt.db"), str(dest))
        media = json.loads((dest / "media.json").read_text(encoding="utf-8"))
        self.assertEqual(media["essences"], [
            {"id": closed, "role": "hi-res", "location": "/media/closed.mxf", "checksum": "abc"},
        ])
        self.assertEqual(media["versions"], [])

    def test_backup_media_json_lists_done_versions_not_pending(self):
        store = self.db()
        a = store.mint_asset(owner="desk")
        s = store.add_span(a.id, "clip", "01:00:00:00", "01:00:01:00")
        store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at, storage_backend, storage_key,"
            " storage_sha256) VALUES (?, ?, ?, ?, ?, 'now', ?, ?, ?)",
            ("v-done", a.id, s, "house", "done", "s3", "a/cut.mp4", "aa" * 32),
        )
        store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at, storage_backend, storage_key,"
            " storage_sha256) VALUES (?, ?, ?, ?, ?, 'now', ?, ?, ?)",
            ("v-wait", a.id, s, "ott", "pending", "s3", "a/wait.mp4", "bb" * 32),
        )
        store.conn.commit()
        dest = self.dir / "bk"
        backup.backup(str(self.dir / "volt.db"), str(dest))
        media = json.loads((dest / "media.json").read_text(encoding="utf-8"))
        self.assertEqual(media["essences"], [])
        self.assertEqual(media["versions"], [
            {"id": "v-done", "backend": "s3", "key": "a/cut.mp4", "sha256": "aa" * 32},
        ])

    def test_backup_with_media_copies_local_files_under_owned_dirs(self):
        store = self.db()
        proxy, quarantine, publish = (self.dir / n for n in ("proxy", "quarantine", "publish"))
        for folder in (proxy, quarantine, publish):
            folder.mkdir()
        a = store.mint_asset(owner="desk")
        hi = quarantine / "take.mxf"
        px = proxy / f"{a.id}.mp4"
        hi.write_bytes(b"hi-res-bytes")
        px.write_bytes(b"proxy-bytes")
        eid_hi = store.add_essence(a.id, "hi-res", str(hi), checksum="c1")
        eid_px = store.add_essence(a.id, "proxy", str(px), checksum="c2")
        s = store.add_span(a.id, "clip", "01:00:00:00", "01:00:01:00")
        pub_file = publish / a.id
        pub_file.mkdir()
        local_obj = pub_file / "house.mp4"
        local_obj.write_bytes(b"published")
        store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at, storage_backend, storage_key,"
            " storage_sha256) VALUES (?, ?, ?, ?, 'done', 'now', 'local', ?, ?)",
            ("v1", a.id, s, "house", f"{a.id}/house.mp4", "dd" * 32),
        )
        store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at, storage_backend, storage_key,"
            " storage_sha256) VALUES (?, ?, ?, ?, 'done', 'now', 's3', ?, ?)",
            ("v2", a.id, s, "ott", "bucket/key.mp4", "ee" * 32),
        )
        store.conn.commit()
        dest = self.dir / "bk"
        env = {
            "VOLT_PROXY_DIR": str(proxy),
            "VOLT_QUARANTINE_DIR": str(quarantine),
            "VOLT_PUBLISH_DIR": str(publish),
        }
        with mock.patch.dict(os.environ, env):
            self.assertEqual(
                backup.main(["backup", "--with-media", str(self.dir / "volt.db"), str(dest)]), 0)
        copied_hi = dest / "media" / a.id / hi.name
        copied_px = dest / "media" / a.id / px.name
        copied_pub = dest / "media" / a.id / "house.mp4"
        self.assertEqual(copied_hi.read_bytes(), b"hi-res-bytes")
        self.assertEqual(copied_px.read_bytes(), b"proxy-bytes")
        self.assertEqual(copied_pub.read_bytes(), b"published")
        self.assertFalse(any(dest.rglob("key.mp4")))
        media = json.loads((dest / "media.json").read_text(encoding="utf-8"))
        self.assertEqual({e["id"] for e in media["essences"]}, {eid_hi, eid_px})
        self.assertEqual({v["id"] for v in media["versions"]}, {"v1", "v2"})
        self.assertEqual(media.get("missing"), [])

    def test_backup_with_media_lists_missing_sources_and_still_succeeds(self):
        store = self.db()
        proxy = self.dir / "proxy"
        proxy.mkdir()
        a = store.mint_asset(owner="desk")
        gone = proxy / "gone.mp4"
        eid = store.add_essence(a.id, "proxy", str(gone), checksum="zz")
        dest = self.dir / "bk"
        backup.backup(
            str(self.dir / "volt.db"), str(dest), with_media=True, proxy_dir=proxy,
            quarantine_dir=self.dir / "q", publish_dir=self.dir / "p")
        self.assertTrue((dest / backup.MANIFEST).is_file())
        media = json.loads((dest / "media.json").read_text(encoding="utf-8"))
        self.assertEqual(media["essences"][0]["id"], eid)
        self.assertTrue(any(str(gone) in str(item) or item == str(gone) for item in media["missing"]))

    def test_restore_ignores_missing_or_unreadable_media_json(self):
        populate(self.db())
        dest = self.dir / "bk"
        backup.backup(str(self.dir / "volt.db"), str(dest))
        if (dest / "media.json").is_file():
            (dest / "media.json").unlink()
        report = backup.restore(dest, str(self.dir / "old.db"))
        self.assertEqual(report["row_counts"]["asset"], 2)
        backup.backup(str(self.dir / "volt.db"), str(self.dir / "bk2"))
        (self.dir / "bk2" / "media.json").write_text("not json {", encoding="utf-8")
        report = backup.restore(self.dir / "bk2", str(self.dir / "old2.db"))
        self.assertEqual(report["row_counts"]["asset"], 2)


class PostgresTools(unittest.TestCase):
    def test_missing_tools_are_a_clear_refusal(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch("shutil.which", return_value=None), \
                mock.patch("glob.glob", return_value=[]):
            os.environ.pop("VOLT_PG_DUMP", None)
            with self.assertRaisesRegex(backup.BackupError, "pg_dump was not found on PATH"):
                backup.backup("postgresql://u:secret@localhost:1/db", str(Path(tempfile.gettempdir()) / "never-made-bk"))
        self.assertFalse((Path(tempfile.gettempdir()) / "never-made-bk" / backup.MANIFEST).exists())

    def test_the_password_stays_out_of_the_command_line_and_the_manifest_source(self):
        args, env, secret = backup._pg_args_env("postgresql://bob:s3cr%40t@db.example:6543/vlt?sslmode=require")
        self.assertNotIn("s3cr@t", " ".join(args))
        self.assertEqual((env["PGPASSWORD"], env["PGSSLMODE"], secret), ("s3cr@t", "require", "s3cr@t"))
        self.assertNotIn("s3cr", backup._redact("postgresql://bob:s3cr%40t@db.example:6543/vlt"))


@unittest.skipUnless(PG_DSN and _tools_present(), "needs VOLT_TEST_PG_DSN and pg_dump/pg_restore")
class PostgresBackup(unittest.TestCase):
    def _dsn(self, db):
        head, _, tail = PG_DSN.rpartition("/")
        return f"{head}/{db}"

    def setUp(self):
        import psycopg
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.names = []
        self.psycopg = psycopg
        self.addCleanup(self._drop)

    def _create(self, suffix):
        import uuid
        name = "bk_" + uuid.uuid4().hex[:10] + suffix
        with self.psycopg.connect(PG_DSN, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{name}"')
        self.names.append(name)
        return self._dsn(name)

    def _drop(self):
        with self.psycopg.connect(PG_DSN, autocommit=True) as admin:
            for name in self.names:
                admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def test_round_trip_refusal_and_corruption(self):
        src, dst = self._create("src"), self._create("dst")
        store = Store(src)
        populate(store)
        store.close()
        bk = Path(self.tmp.name) / "bk"
        manifest = backup.backup(src, str(bk))
        self.assertEqual(manifest["kind"], "postgres")
        self.assertEqual(manifest["row_counts"]["asset"], 2)
        self.assertNotIn("pw", json.dumps(manifest))
        backup.verify_backup(bk)
        report = backup.restore(bk, dst)
        self.assertEqual(report["row_counts"]["asset"], 2)
        a, b = Store(src, init=False), Store(dst, init=False)
        try:
            for table in manifest["row_counts"]:
                q = f'SELECT * FROM "{table}" ORDER BY 1, 2'
                self.assertEqual([tuple(r) for r in a.conn.execute(q).fetchall()],
                                 [tuple(r) for r in b.conn.execute(q).fetchall()], table)
        finally:
            a.close()
            b.close()
        with self.assertRaisesRegex(backup.BackupError, "refusing to overwrite"):
            backup.restore(bk, dst)
        backup.restore(bk, dst, overwrite=True)
        data = bk / backup.PG_FILE
        raw = bytearray(data.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        data.write_bytes(bytes(raw))
        with self.assertRaises(backup.BackupError):
            backup.restore(bk, self._create("bad"))


# ---- storage verification ----------------------------------------------------------------------------

class VerifyStorage(Tmp):
    def test_reports_missing_empty_mismatched_and_orphaned_and_writes_nothing(self):
        store = self.db()
        pub = self.dir / "pub"
        backend = storage.LocalBackend(pub)
        a = store.mint_asset(owner="desk")
        good, gone, empty, bad = (self.dir / n for n in ("good.mxf", "gone.mxf", "empty.mxf", "bad.mxf"))
        good.write_bytes(b"x" * 100)
        empty.write_bytes(b"")
        bad.write_bytes(b"y" * 100)
        import hashlib
        store.add_essence(a.id, "hi-res", str(good), checksum=hashlib.sha256(b"x" * 100).hexdigest())
        store.add_essence(a.id, "proxy", str(gone))
        store.add_essence(a.id, "ref", str(empty))
        store.add_essence(a.id, "ref2", str(bad), checksum="0" * 64)
        s = store.add_span(a.id, "clip", "01:00:00:00", "01:00:01:00")
        src = self.dir / "cut.mp4"
        src.write_bytes(b"cut" * 50)
        obj = backend.put_file(src, f"{a.id}/{s}-house.mp4")
        ok_v = ("INSERT INTO version (id, asset_id, span_id, kind, status, created_at, storage_backend, storage_key,"
                " storage_sha256) VALUES (?, ?, ?, ?, 'done', 'now', ?, ?, ?)")
        store.conn.execute(ok_v, ("v1", a.id, s, "house", "local", obj.key, obj.sha256))
        store.conn.execute(ok_v, ("v2", a.id, s, "ott", "local", f"{a.id}/{s}-ott.mp4", "f" * 64))  # never uploaded
        store.conn.execute(ok_v, ("v3", a.id, s, "social", "local", obj.key, "e" * 64))  # recorded sha is wrong
        store.conn.commit()
        (pub / "stray").mkdir()
        (pub / "stray" / "left-behind.mp4").write_bytes(b"orphan")
        before = dump(str(self.dir / "volt.db"))
        rep = backup.verify_storage(store, backend, deep=True)
        self.assertFalse(rep.ok)
        self.assertEqual([w for w, _ in rep.missing].count("version v2 of asset " + a.id), 1)
        self.assertTrue(any(str(gone) == where for _, where in rep.missing))
        self.assertEqual(rep.empty, [str(empty)])
        self.assertEqual(sorted(w for w, _, _ in rep.mismatched),
                         sorted([f"essence ref2 of asset {a.id}", f"version v3 of asset {a.id}"]))
        self.assertEqual(rep.orphaned, ["local:stray/left-behind.mp4"])
        self.assertEqual(dump(str(self.dir / "volt.db")), before)  # read only
        self.assertTrue(good.exists() and bad.exists())

    def test_a_clean_library_is_ok(self):
        store = self.db()
        a = store.mint_asset(owner="desk")
        f = self.dir / "m.mxf"
        f.write_bytes(b"data")
        store.add_essence(a.id, "hi-res", str(f))
        rep = backup.verify_storage(store, storage.LocalBackend(self.dir / "pub"))
        self.assertTrue(rep.ok, rep.lines())
        self.assertEqual(rep.checked, 1)

    def test_orphans_in_the_owned_folders(self):
        store = self.db()
        a = store.mint_asset(owner="desk")
        proxy = self.dir / "proxy"
        proxy.mkdir()
        (proxy / f"{a.id}.mp4").write_bytes(b"p")
        (proxy / "stray.mp4").write_bytes(b"s")
        store.add_essence(a.id, "proxy", str(proxy / f"{a.id}.mp4"))
        rep = backup.verify_storage(store, None, proxy_dir=proxy)
        self.assertEqual(rep.orphaned, [str(proxy / "stray.mp4")])

    def test_cli_exit_code(self):
        store = self.db()
        a = store.mint_asset(owner="desk")
        store.add_essence(a.id, "hi-res", str(self.dir / "absent.mxf"))
        store.close()
        with mock.patch.dict(os.environ, {"VOLT_PUBLISH_DIR": str(self.dir / "pub")}):
            self.assertEqual(backup.main(["verify-storage", str(self.dir / "volt.db")]), 1)


# ---- delete: tombstone, restore, purge -----------------------------------------------------------------

def _later(days):
    return datetime.now(timezone.utc) + timedelta(days=days)


class Library(Tmp):
    """An asset with an owned proxy, an owned quarantine copy, a thumbnail dir and an external (registered) hi-res."""

    def setUp(self):
        super().setUp()
        self.store = self.db()
        self.proxy, self.quar, self.out, self.ext = (self.dir / n for n in ("proxy", "quarantine", "out", "external"))
        for d in (self.proxy, self.quar, self.out, self.ext):
            d.mkdir()
        self.roots = retention.OwnedRoots(self.proxy, self.quar, self.out)

    def asset(self, external=True, quarantine=False, retention_class="show"):
        a = self.store.mint_asset(owner="desk", retention_class=retention_class)
        files = {}
        if external:
            files["external"] = self.ext / f"{a.id}-recording.mxf"   # even named for the asset: still not owned
            files["external"].write_bytes(b"core wrote this")
            self.store.add_essence(a.id, "hi-res", str(files["external"]))
        if quarantine:
            files["quarantine"] = self.quar / f"{a.id}-card.mp4"
            files["quarantine"].write_bytes(b"card copy")
            self.store.add_essence(a.id, "hi-res", str(files["quarantine"]))
        files["proxy"] = self.proxy / f"{a.id}.mp4"
        files["proxy"].write_bytes(b"proxy")
        self.store.add_essence(a.id, "proxy", str(files["proxy"]))
        (self.out / "thumbs" / a.id).mkdir(parents=True)
        (self.out / "thumbs" / a.id / "s-1.jpg").write_bytes(b"jpg")
        self.store.add_span(a.id, "transcript", "01:00:00:00", "01:00:01:00", text="needle")
        return a.id, files

    def run_delete(self, asset_id):
        job_id = self.store.delete_asset(asset_id)
        r = Runner(self.store, default_handlers(Config()), name="t")
        r.drain()
        return self.store.conn.execute("SELECT status, error FROM job WHERE id = ?", (job_id,)).fetchone()

    def count(self, table="asset"):
        return self.store.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class Tombstone(Library):
    def test_the_runner_delete_job_soft_deletes_and_hides(self):
        from volt import library, operator_state, session
        aid, files = self.asset(quarantine=True)
        self.assertEqual(len(library.search(self.store, "needle")), 1)
        job = self.run_delete(aid)
        self.assertEqual(job["status"], "done")
        self.assertIsNotNone(self.store.get_asset(aid).deleted_at)
        self.assertEqual(library.search(self.store, "needle"), [])
        self.assertEqual([r.id for r in session.list_assets(self.store)], [])
        self.assertEqual(operator_state.assets(self.store), [])
        self.assertTrue(all(p.exists() for p in files.values()))  # soft delete touches no file
        self.assertEqual(self.count("essence"), 3)

    def test_a_tombstoned_asset_cannot_be_published(self):
        aid, _ = self.asset()
        self.store.set_cleared(aid, True)
        self.store.assert_publishable(aid)
        retention.tombstone(self.store, aid)
        with self.assertRaises(Exception):
            self.store.assert_publishable(aid)

    def test_restore_brings_it_back_and_it_can_be_deleted_again(self):
        from volt import library
        aid, _ = self.asset()
        self.run_delete(aid)
        self.assertTrue(retention.restore(self.store, aid))
        self.assertIsNone(self.store.get_asset(aid).deleted_at)
        self.assertEqual(len(library.search(self.store, "needle")), 1)
        self.assertFalse(retention.restore(self.store, aid))  # not deleted now
        self.assertEqual(self.run_delete(aid)["status"], "done")  # the same delete key works a second time
        self.assertIsNotNone(self.store.get_asset(aid).deleted_at)

    def test_every_safeguard_fails_the_job_with_a_reason_and_deletes_nothing(self):
        cases = {}
        held, _ = self.asset()
        self.store.set_hold(held, True)
        cases["legal hold"] = held
        perm, _ = self.asset(retention_class="permanent")
        cases["never deleted"] = perm
        open_a, _ = self.asset()
        self.store.add_essence(open_a, "hi-res", str(self.ext / "growing.mxf"), open_file=True)
        cases["still recording"] = open_a
        on_rundown, _ = self.asset()
        span = self.store.add_span(on_rundown, "clip", "01:00:00:00", "01:00:02:00")
        attach(self.store, add_item(self.store, open_show(self.store, "S"), "Open"), span)
        cases["rundown item"] = on_rundown
        published, _ = self.asset()
        pspan = self.store.add_span(published, "clip", "01:00:00:00", "01:00:02:00")
        self.store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at) VALUES ('pv', ?, ?, 'house', 'queued', 'n')",
            (published, pspan))
        self.store.conn.commit()
        cases["publish version"] = published
        for reason, aid in cases.items():
            if reason == "legal hold":
                with self.assertRaises(HoldError):   # refused even before it is queued
                    self.store.delete_asset(aid)
                continue
            job = self.run_delete(aid)
            self.assertEqual(job["status"], "failed", reason)
            self.assertIn(reason, job["error"])
            self.assertIsNone(self.store.get_asset(aid).deleted_at, reason)
        # a hold set after queueing is caught by the handler too
        late, _ = self.asset()
        job_id = self.store.delete_asset(late)
        self.store.set_hold(late, True)
        Runner(self.store, default_handlers(Config()), name="t").drain()
        row = self.store.conn.execute("SELECT status, error FROM job WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("legal hold", row["error"])

    def test_all_reasons_are_listed(self):
        aid, _ = self.asset(retention_class="permanent")
        self.store.set_hold(aid, True)
        with self.assertRaises(retention.DeleteRefused) as ctx:
            retention.tombstone(self.store, aid)
        self.assertEqual(len(ctx.exception.reasons), 2)


class Purge(Library):
    def tombstoned(self, **kw):
        aid, files = self.asset(**kw)
        retention.tombstone(self.store, aid, now="2020-01-01T00:00:00Z")
        return aid, files

    def audit(self, aid):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT kind, outcome, location, detail FROM purge_audit WHERE asset_id = ? ORDER BY purged_at, id", (aid,))]

    def test_purge_removes_owned_files_and_rows_and_never_the_registered_file(self):
        aid, files = self.tombstoned(quarantine=True)
        report = retention.purge_due(self.store, self.roots, now=_later(31))
        self.assertEqual(report.count("purged"), 1)
        self.assertFalse(files["proxy"].exists())
        self.assertFalse(files["quarantine"].exists())
        self.assertFalse((self.out / "thumbs" / aid).exists())
        self.assertTrue(files["external"].exists())            # the registered recording survives, byte for byte
        self.assertEqual(files["external"].read_bytes(), b"core wrote this")
        self.assertEqual((self.count("asset"), self.count("essence"), self.count("span"), self.count("job")), (0, 0, 0, 0))
        rows = self.audit(aid)
        by = {(r["kind"], r["outcome"]) for r in rows}
        self.assertIn(("essence", "deleted"), by)
        self.assertIn(("essence", "left_in_place"), by)
        self.assertIn(("thumbs", "deleted"), by)
        summary = [r for r in rows if r["kind"] == "asset"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(json.loads(summary[0]["detail"])["owner"], "desk")
        left = [r for r in rows if r["outcome"] == "left_in_place"][0]
        self.assertEqual(left["location"], str(files["external"]))

    def test_nothing_is_purged_inside_the_window(self):
        aid, files = self.tombstoned()
        self.assertEqual(retention.purge_due(self.store, self.roots, now=datetime(2020, 1, 20, tzinfo=timezone.utc)).results, [])
        self.assertTrue(files["proxy"].exists())
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.count("purge_audit"), 0)

    def test_window_is_configurable_per_class_by_env(self):
        self.assertEqual(retention.retention_days("show", {}), 30)
        self.assertEqual(retention.retention_days("show", {"VOLT_PURGE_AFTER_DAYS": "7"}), 7)
        env = {"VOLT_PURGE_AFTER_DAYS": "7", "VOLT_PURGE_AFTER_DAYS_NEWS": "90"}
        self.assertEqual(retention.retention_days("news", env), 90)
        self.assertEqual(retention.retention_days("show", env), 7)
        with self.assertRaises(ValueError):
            retention.retention_days("show", {"VOLT_PURGE_AFTER_DAYS": "soon"})
        aid, _ = self.tombstoned()
        self.assertEqual(retention.due(self.store, datetime(2020, 1, 9, tzinfo=timezone.utc), env), [aid])
        self.assertEqual(retention.due(self.store, datetime(2020, 1, 6, tzinfo=timezone.utc), env), [])

    def test_a_partial_failure_keeps_the_rows_and_the_next_pass_finishes(self):
        aid, files = self.tombstoned(quarantine=True)
        real = retention._unlink
        calls = {"n": 0}

        def flaky(path):
            if path.name.endswith("card.mp4") and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("disk went away")
            return real(path)

        with mock.patch.object(retention, "_unlink", flaky):
            first = retention.purge_asset(self.store, aid, self.roots)
        self.assertEqual(first.status, "partial")
        self.assertIn("disk went away", first.failed[0])
        self.assertEqual(self.count("asset"), 1)               # still there, still tombstoned, so it is retried
        self.assertTrue(files["quarantine"].exists())
        second = retention.purge_asset(self.store, aid, self.roots)
        self.assertEqual(second.status, "purged")
        self.assertFalse(files["quarantine"].exists())
        self.assertTrue(files["external"].exists())
        self.assertEqual(self.count("asset"), 0)
        outcomes = [r["outcome"] for r in self.audit(aid) if r["kind"] == "essence"]
        self.assertIn("failed", outcomes)
        self.assertIn("already_missing", outcomes)             # the proxy removed in pass one is not an error in pass two

    def test_purge_is_idempotent_when_files_are_already_gone(self):
        aid, files = self.tombstoned()
        files["proxy"].unlink()
        self.assertEqual(retention.purge_asset(self.store, aid, self.roots).status, "purged")
        with self.assertRaises(KeyError):                       # nothing left to purge a second time
            retention.purge_asset(self.store, aid, self.roots)
        self.assertEqual(retention.purge_due(self.store, self.roots, now=_later(60)).results, [])

    def test_a_hold_or_attachment_added_after_the_tombstone_stops_the_purge(self):
        aid, files = self.tombstoned()
        self.store.set_hold(aid, True)
        res = retention.purge_asset(self.store, aid, self.roots)
        self.assertEqual((res.status, res.reasons), ("refused", ["legal hold"]))
        self.assertTrue(files["proxy"].exists())
        self.store.set_hold(aid, False)
        span = self.store.add_span(aid, "clip", "01:00:00:00", "01:00:02:00")
        attach(self.store, add_item(self.store, open_show(self.store, "S"), "Open"), span)
        self.assertEqual(retention.purge_asset(self.store, aid, self.roots).status, "refused")
        self.assertTrue(files["proxy"].exists())
        self.assertEqual(self.count("asset"), 1)

    def test_a_running_job_stops_the_purge(self):
        aid, files = self.tombstoned()
        self.store.conn.execute("UPDATE job SET status = 'running' WHERE asset_id = ?", (aid,))
        self.store.enqueue(aid, "proxy", "p")
        self.store.conn.execute("UPDATE job SET status = 'running' WHERE asset_id = ?", (aid,))
        self.store.conn.commit()
        res = retention.purge_asset(self.store, aid, self.roots)
        self.assertEqual(res.status, "refused")
        self.assertTrue(files["proxy"].exists())

    def test_a_live_asset_is_never_purged(self):
        aid, files = self.asset()
        self.assertEqual(retention.purge_asset(self.store, aid, self.roots).status, "skipped")
        self.assertTrue(files["proxy"].exists())

    def test_only_files_inside_owned_roots_and_named_for_the_asset_are_deleted(self):
        aid, _ = self.asset(external=False)
        # a proxy row whose path points at someone else's file, outside the proxy dir
        theirs = self.ext / "important-master.mov"
        theirs.write_bytes(b"theirs")
        self.store.add_essence(aid, "proxy2", str(theirs))
        # a path inside the proxy dir but not named for the asset
        other = self.proxy / "another-assets-proxy.mp4"
        other.write_bytes(b"other")
        self.store.add_essence(aid, "proxy3", str(other))
        # a ".." escape
        sneaky = self.proxy / ".." / "external" / f"{aid}-x.mp4"
        (self.ext / f"{aid}-x.mp4").write_bytes(b"x")
        self.store.add_essence(aid, "proxy4", str(sneaky))
        retention.tombstone(self.store, aid, now="2020-01-01T00:00:00Z")
        self.assertEqual(retention.purge_asset(self.store, aid, self.roots).status, "purged")
        self.assertTrue(theirs.exists() and other.exists() and (self.ext / f"{aid}-x.mp4").exists())

    def test_with_no_owned_roots_configured_nothing_is_deleted(self):
        aid, files = self.tombstoned(quarantine=True)
        res = retention.purge_asset(self.store, aid, retention.OwnedRoots())
        self.assertEqual(res.status, "purged")                  # rows go, files stay: nothing is owned
        self.assertTrue(all(p.exists() for p in files.values()))

    def test_a_file_shared_with_another_asset_is_left(self):
        aid, files = self.tombstoned()
        other = self.store.mint_asset(owner="desk")
        self.store.add_essence(other.id, "proxy", str(files["proxy"]))
        retention.purge_asset(self.store, aid, self.roots)
        self.assertTrue(files["proxy"].exists())

    def test_published_versions_go_through_the_storage_seam_only_when_asked(self):
        aid, files = self.asset()
        backend = storage.LocalBackend(self.dir / "pub")
        span = self.store.add_span(aid, "clip", "01:00:00:00", "01:00:02:00")
        cut = self.dir / "cut.mp4"
        cut.write_bytes(b"cut")
        obj = backend.put_file(cut, f"{aid}/{span}-house.mp4")
        self.store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at, storage_backend, storage_key) "
            "VALUES ('v1', ?, ?, 'house', 'done', 'n', 'local', ?)", (aid, span, obj.key))
        self.store.conn.commit()
        self.assertIn("publish version", retention.blockers(self.store, aid)[0])
        with self.assertRaises(retention.DeleteRefused):
            retention.tombstone(self.store, aid)
        self.store.conn.execute("UPDATE asset SET deleted_at = '2020-01-01T00:00:00Z' WHERE id = ?", (aid,))
        self.store.conn.commit()
        self.assertEqual(retention.purge_asset(self.store, aid, self.roots).status, "refused")  # default refuses
        self.assertTrue(backend.exists(obj.key))
        # a backend that cannot delete: partial, nothing lost
        class Down(storage.LocalBackend):
            def delete(self, key):
                raise storage.StorageError("provider is down")
        bad = retention.OwnedRoots(self.proxy, self.quar, self.out, backend=Down(self.dir / "pub"))
        res = retention.purge_asset(self.store, aid, bad, allow_versions=True)
        self.assertEqual(res.status, "partial")
        self.assertEqual(self.count("asset"), 1)
        good = retention.OwnedRoots(self.proxy, self.quar, self.out, backend=backend)
        self.assertEqual(retention.purge_asset(self.store, aid, good, allow_versions=True).status, "purged")
        self.assertFalse(backend.exists(obj.key))
        self.assertTrue(files["external"].exists())
        self.assertEqual(self.count("version"), 0)
        # a different configured backend is never asked to delete someone else's object
        aid2, _ = self.asset()
        span2 = self.store.add_span(aid2, "clip", "01:00:00:00", "01:00:02:00")
        self.store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at, storage_backend, storage_key) "
            "VALUES ('v2', ?, ?, 'house', 'done', 'n', 's3:other-bucket', 'k')", (aid2, span2))
        self.store.conn.execute("UPDATE asset SET deleted_at = '2020-01-01T00:00:00Z' WHERE id = ?", (aid2,))
        self.store.conn.commit()
        res = retention.purge_asset(self.store, aid2, good, allow_versions=True)
        self.assertEqual(res.status, "partial")
        self.assertIn("not the configured storage", res.failed[0])

    def test_the_periodic_purge_runs_in_the_runner(self):
        aid, files = self.tombstoned()
        cfg = Config(proxy_dir=self.proxy, out_dir=self.out)
        r = Runner(self.store, default_handlers(cfg), name="t")
        r.periodic.append(runner_mod.Periodic("purge", 1, runner_mod.purge_task(cfg)))
        with mock.patch.object(retention, "datetime", wraps=datetime) as dt:
            dt.now.return_value = _later(40)
            out = r.run_periodic(force=True)
        self.assertEqual(out["purge"].count("purged"), 1)
        self.assertFalse(files["proxy"].exists())
        self.assertTrue(files["external"].exists())

    def test_cli_list_restore_purge(self):
        aid, files = self.tombstoned()
        self.store.close()
        db = str(self.dir / "volt.db")
        self.assertEqual(retention.main([db, "list"]), 0)
        self.assertEqual(retention.main([db, "purge", "--proxy-dir", str(self.proxy), "--older-than-days", "9999"]), 0)
        self.assertTrue(files["proxy"].exists())
        self.assertEqual(retention.main([db, "purge", "--proxy-dir", str(self.proxy), "--older-than-days", "0"]), 0)
        self.assertFalse(files["proxy"].exists())
        self.assertTrue(files["external"].exists())


# ---- the operator API ---------------------------------------------------------------------------------

class DeleteApi(Library):
    def setUp(self):
        super().setUp()
        self._saved = (serve.DB_PATH, serve.TOKEN)
        serve.DB_PATH, serve.TOKEN = str(self.dir / "volt.db"), None
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()
        serve.DB_PATH, serve.TOKEN = self._saved

    def post(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=15)
        try:
            conn.request("POST", path, body=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            conn.close()

    def test_delete_then_restore(self):
        aid, files = self.asset()
        status, body = self.post(f"/api/assets/{aid}/delete")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"] and not body["alreadyDeleted"] and body["deletedAt"])
        self.assertEqual(self.post(f"/api/assets/{aid}/delete")[1]["alreadyDeleted"], True)
        self.assertTrue(files["proxy"].exists())
        status, body = self.post(f"/api/assets/{aid}/restore")
        self.assertEqual(status, 200, body)
        status, body = self.post(f"/api/assets/{aid}/restore")
        self.assertEqual((status, body["error"]["code"]), (409, "not_deleted"))

    def test_refusals_and_unknowns(self):
        aid, _ = self.asset()
        self.store.set_hold(aid, True)
        status, body = self.post(f"/api/assets/{aid}/delete")
        self.assertEqual((status, body["error"]["code"]), (409, "delete_refused"))
        self.assertEqual(body["reasons"], ["legal hold"])
        self.assertEqual(self.post("/api/assets/nope/delete")[0], 404)
        self.assertEqual(self.post("/api/assets/nope/restore")[0], 404)
        status, body = self.post(f"/api/assets/{aid}/delete", {"force": True})
        self.assertEqual((status, body["error"]["code"]), (422, "bad_input"))
        self.assertIsNone(self.store.get_asset(aid).deleted_at)


if __name__ == "__main__":
    unittest.main()
