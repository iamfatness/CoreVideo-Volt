"""The `volt` command, preflight, and the supervisor behind `volt run`."""

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import volt
from volt import cli, preflight, supervisor
from volt.store import Store

CONTROL = Path(volt.__file__).resolve().parents[1]
ROOT = CONTROL.parent
IS_WINDOWS = os.name == "nt"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def py(code: str) -> list:
    return [sys.executable, "-c", textwrap.dedent(code)]


class Version(unittest.TestCase):
    def test_version_file_module_and_command_agree(self):
        file_version = (ROOT / "VERSION").read_text().strip()
        self.assertEqual(file_version, volt.__version__)
        out = subprocess.run([sys.executable, "-m", "volt", "version"], cwd=CONTROL, capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.startswith(f"volt {volt.__version__} "), out.stdout)

    def test_changelog_names_the_current_version(self):
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{volt.__version__}]", text)

    def test_pyproject_is_stdlib_only_with_the_extras_and_the_entry_point(self):
        import tomllib
        data = tomllib.loads((CONTROL / "pyproject.toml").read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(project["requires-python"], ">=3.12")
        self.assertTrue({"speech", "s3", "azure", "gcs", "postgres"} <= set(project["optional-dependencies"]))
        self.assertEqual(project["scripts"]["volt"], "volt.cli:main")
        self.assertEqual(data["tool"]["setuptools"]["dynamic"]["version"], {"attr": "volt.__version__"})


class Commands(unittest.TestCase):
    def run_cmd(self, *args, timeout=60, env=None):
        full = dict(os.environ, **(env or {}))
        return subprocess.run([sys.executable, "-m", *args], cwd=CONTROL, capture_output=True, text=True,
                              timeout=timeout, env=full)

    def test_serve_help_prints_help_and_does_not_start_a_server(self):
        out = self.run_cmd("volt.serve", "--help")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("usage:", out.stdout)
        for flag in ("--host", "--port", "--db"):
            self.assertIn(flag, out.stdout)
        self.assertNotIn("Volt session on", out.stdout)

    def test_volt_serve_help_and_unknown_flag(self):
        self.assertEqual(self.run_cmd("volt", "serve", "--help").returncode, 0)
        bad = self.run_cmd("volt", "serve", "--nope")
        self.assertEqual(bad.returncode, 2)

    def test_unknown_subcommand_and_usage(self):
        self.assertEqual(self.run_cmd("volt", "bogus").returncode, 2)
        out = self.run_cmd("volt", "--help")
        self.assertEqual(out.returncode, 0)
        for word in ("serve", "runner", "clock", "run", "check", "version", "watch"):
            self.assertIn(word, out.stdout)

    def test_refusing_a_public_bind_without_a_token_is_a_config_error(self):
        env = {"VOLT_TOKEN": ""}
        out = self.run_cmd("volt", "serve", "--host", "0.0.0.0", "--port", "0", env=env)
        self.assertEqual(out.returncode, supervisor.EX_CONFIG)
        self.assertIn("VOLT_TOKEN", out.stderr)

    def test_runner_and_clock_help_through_the_command(self):
        for name in ("runner", "clock"):
            out = self.run_cmd("volt", name, "--help")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("usage:", out.stdout)


class BackupAndSkew(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "v.db")
        Store(self.db).close()

    def volt(self, *args, env=None):
        full = {k: v for k, v in os.environ.items() if k != "VOLT_DB"}
        full.update(env or {})
        return subprocess.run([sys.executable, "-m", "volt", *args], cwd=CONTROL, capture_output=True, text=True,
                              timeout=120, env=full)

    def test_backup_verify_restore_through_the_volt_command(self):
        out = self.volt("backup", self.db, str(self.dir / "bk"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.volt("verify", str(self.dir / "bk")).returncode, 0)
        target = str(self.dir / "restored.db")
        out = self.volt("restore", str(self.dir / "bk"), target)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(Path(target).exists())
        self.assertEqual(self.volt("verify-storage", target).returncode, 0)

    def _make_newer(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (999, 'future', 'now')")
        conn.commit()
        conn.close()

    def test_serve_runner_and_clock_refuse_a_newer_database_with_exit_78(self):
        self._make_newer()
        env = {"VOLT_DB": self.db, "VOLT_ROOM_URL": "http://127.0.0.1:1"}
        for args in (("serve", "--port", "0"), ("runner", "--once"), ("clock", "--once")):
            out = self.volt(*args, env=env)
            self.assertEqual(out.returncode, supervisor.EX_CONFIG, (args, out.stderr))
            self.assertIn("schema", out.stderr)

    def test_check_describes_purge_and_backup_and_rejects_bad_purge_settings(self):
        base = {"VOLT_MIN_FREE_MB": "1", "VOLT_PURGE_EVERY": "3600", "VOLT_PURGE_AFTER_DAYS": "14"}
        results = {r.name: r for r in preflight.run_checks(env=base, db=self.db, port=0)}
        self.assertIn("14 day", results["purge"].detail)
        self.assertEqual(results["backup"].status, preflight.OK)
        bad = {r.name: r for r in preflight.run_checks(env={**base, "VOLT_PURGE_EVERY": "soon"}, db=self.db, port=0)}
        self.assertEqual(bad["purge"].status, preflight.FAIL)


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def checks(self, env=None, **kw):
        base = {"VOLT_PROXY_DIR": str(self.dir / "p"), "VOLT_OUT": str(self.dir / "out"), "VOLT_MIN_FREE_MB": "1"}
        base.update(env or {})
        return {r.name: r for r in preflight.run_checks(env=base, **kw)}

    def test_new_database_and_free_port_pass(self):
        r = self.checks(db=str(self.dir / "v.db"), port=free_port())
        self.assertNotIn(preflight.FAIL, {x.status for x in r.values()}, {k: v.detail for k, v in r.items()})
        self.assertEqual(r["python"].status, preflight.OK)
        self.assertEqual(r["port"].status, preflight.OK)

    def test_migrated_old_and_newer_databases(self):
        db = str(self.dir / "v.db")
        Store(db).close()
        self.assertEqual(self.checks(db=db, port=0)["database"].status, preflight.OK)
        import sqlite3
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM schema_version WHERE version = (SELECT MAX(version) FROM schema_version)")
        conn.commit()
        conn.close()
        self.assertEqual(self.checks(db=db, port=0)["database"].status, preflight.WARN)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO schema_version (version, name, applied_at) VALUES (999, 'future', 'now')")
        conn.commit()
        conn.close()
        newer = self.checks(db=db, port=0)["database"]
        self.assertEqual(newer.status, preflight.FAIL)
        self.assertIn("newer", newer.detail)

    def test_check_does_not_create_or_change_the_database(self):
        db = self.dir / "v.db"
        self.checks(db=str(db), port=0)
        self.assertFalse(db.exists())

    def test_port_in_use_fails(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            r = self.checks(db=str(self.dir / "v.db"), port=s.getsockname()[1])
        self.assertEqual(r["port"].status, preflight.FAIL)

    def test_missing_ffmpeg_fails(self):
        with mock.patch("volt.preflight.shutil.which", return_value=None):
            r = self.checks(db=str(self.dir / "v.db"), port=0)
        self.assertEqual(r["ffmpeg"].status, preflight.FAIL)
        self.assertEqual(r["ffprobe"].status, preflight.FAIL)

    def test_bad_storage_config_and_open_bind_fail(self):
        r = self.checks(env={"VOLT_STORAGE": "s3"}, db=str(self.dir / "v.db"), port=0)
        self.assertEqual(r["storage (s3)"].status, preflight.FAIL)
        r = self.checks(env={"VOLT_TOKEN": ""}, db=str(self.dir / "v.db"), host="0.0.0.0", port=0)
        self.assertEqual(r["access"].status, preflight.FAIL)

    def test_unwritable_database_directory_fails(self):
        r = self.checks(db=str(self.dir / "nope" / "v.db"), port=0)
        self.assertEqual(r["database"].status, preflight.FAIL)

    def test_secrets_are_never_printed(self):
        secret_token = "operator-token-abcdef-123456"
        dsn = "postgresql://volt:pgpass-777777@127.0.0.1:1/volt"  # nothing listens on port 1
        with mock.patch.dict(os.environ, {"PGCONNECT_TIMEOUT": "2"}):
            results = preflight.run_checks(env={
                "VOLT_TOKEN": secret_token, "VOLT_ROOM_TOKEN": "room-token-zzzzzz", "VOLT_ROOM_URL": "http://x",
                "VOLT_DB": dsn, "VOLT_MIN_FREE_MB": "1"}, port=0)
        text = preflight.render(results) + json.dumps([r.as_dict() for r in results])
        for secret in (secret_token, "room-token-zzzzzz", "pgpass-777777"):
            self.assertNotIn(secret, text)
        self.assertEqual({r.name: r for r in results}["database"].status, preflight.FAIL)

    def test_command_exit_codes_and_json(self):
        out = subprocess.run([sys.executable, "-m", "volt", "check", "--db", str(self.dir / "v.db"), "--port", "0",
                              "--json"], cwd=CONTROL, capture_output=True, text=True, timeout=120,
                             env=dict(os.environ, VOLT_MIN_FREE_MB="1"))
        names = [r["name"] for r in json.loads(out.stdout)]
        self.assertIn("database", names)
        self.assertEqual(out.returncode, 1 if any(r["status"] == "fail" for r in json.loads(out.stdout)) else 0)

    def test_missing_ingest_root_warns_and_fails_when_watch_is_on(self):
        missing = str(self.dir / "no-such-inbox")
        r = self.checks(env={"VOLT_INGEST_ROOTS": missing}, db=str(self.dir / "v.db"), port=0)
        self.assertEqual(r["ingest roots"].status, preflight.WARN)
        self.assertIn(missing, r["ingest roots"].detail)
        r = self.checks(
            env={"VOLT_INGEST_ROOTS": missing, "VOLT_WATCH": "1"},
            db=str(self.dir / "v.db"), port=0)
        self.assertEqual(r["ingest roots"].status, preflight.FAIL)
        present = self.dir / "inbox"
        present.mkdir()
        r = self.checks(env={"VOLT_INGEST_ROOTS": str(present)}, db=str(self.dir / "v.db"), port=0)
        self.assertEqual(r["ingest roots"].status, preflight.OK)
        r = self.checks(db=str(self.dir / "v.db"), port=0)
        self.assertNotIn("ingest roots", r)


class WatchCommand(unittest.TestCase):
    def test_volt_watch_once_scans_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "inbox"
            inbox.mkdir()
            clip = inbox / "card.mov"
            clip.write_bytes(b"clip")
            db = str(root / "v.db")
            Store(db).close()
            env = dict(os.environ, VOLT_INGEST_ROOTS=str(inbox), VOLT_QUARANTINE=str(root / "q"),
                       VOLT_WATCH_STABLE_SECONDS="0")
            out = subprocess.run(
                [sys.executable, "-m", "volt", "watch", "--once", db],
                cwd=CONTROL, capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(out.returncode, 0, out.stderr)
            store = Store(db, init=False)
            jobs = store.conn.execute("SELECT COUNT(*) FROM job WHERE type = 'ingest'").fetchone()[0]
            store.close()
            self.assertEqual(jobs, 1)


class SupervisorUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def start(self, sup):
        result = {}
        thread = threading.Thread(target=lambda: result.update(code=sup.run()), daemon=True)
        thread.start()
        self.addCleanup(lambda: (sup.stop(), thread.join(30)))
        return thread, result

    def wait_for(self, predicate, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_crashing_child_is_restarted_with_growing_backoff(self):
        count = self.dir / "count"
        child = supervisor.Child("flaky", py(f"""
            import sys
            open({str(count)!r}, 'a').write('x')
            sys.exit(3)"""))
        sup = supervisor.Supervisor([child], backoff_initial=0.05, backoff_max=0.2, stable_after=60, grace=5, tick=0.01)
        thread, result = self.start(sup)
        self.assertTrue(self.wait_for(lambda: count.exists() and len(count.read_text()) >= 4))
        sup.stop()
        thread.join(30)
        self.assertEqual(result["code"], 0)
        self.assertGreaterEqual(child.restarts, 3)
        self.assertEqual(child.backoff, 0.2)  # 0.05, 0.1, 0.2, capped

    def test_backoff_resets_after_a_stable_run(self):
        child = supervisor.Child("steady", py("import time; time.sleep(0.3)"))
        sup = supervisor.Supervisor([child], backoff_initial=0.05, backoff_max=5, stable_after=0.1, grace=5, tick=0.01)
        thread, _ = self.start(sup)
        self.assertTrue(self.wait_for(lambda: child.restarts >= 3))
        sup.stop()
        thread.join(30)
        self.assertEqual(child.backoff, 0.05)

    def test_a_config_error_stops_everything_and_is_not_retried(self):
        marker = self.dir / "starts"
        bad = supervisor.Child("serve", py(f"""
            import sys
            open({str(marker)!r}, 'a').write('x')
            sys.exit({supervisor.EX_CONFIG})"""))
        other = supervisor.Child("runner", py("import time; time.sleep(60)"))
        sup = supervisor.Supervisor([bad, other], backoff_initial=0.05, grace=5, tick=0.01)
        thread, result = self.start(sup)
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["code"], supervisor.EX_CONFIG)
        self.assertEqual(marker.read_text(), "x")
        self.assertTrue(other.proc is None or other.proc.poll() is not None)

    def test_stop_ends_every_child(self):
        kids = [supervisor.Child(f"k{i}", py("import time; time.sleep(60)")) for i in range(2)]
        sup = supervisor.Supervisor(kids, grace=10, tick=0.01)
        thread, result = self.start(sup)
        self.assertTrue(self.wait_for(lambda: all(k.proc is not None for k in kids)))
        procs = [k.proc for k in kids]
        started = time.monotonic()
        sup.stop()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(all(p.poll() is not None for p in procs))
        self.assertEqual(result["code"], 0)

    def test_a_child_that_ignores_the_stop_request_is_killed_after_the_grace(self):
        ignore = "signal.SIGBREAK" if IS_WINDOWS else "signal.SIGTERM"
        child = supervisor.Child("stubborn", py(f"""
            import signal, time
            signal.signal({ignore}, signal.SIG_IGN)
            time.sleep(60)"""))
        sup = supervisor.Supervisor([child], grace=1.0, tick=0.01)
        thread, _ = self.start(sup)
        self.assertTrue(self.wait_for(lambda: child.proc is not None))
        time.sleep(1.0)  # let the interpreter install its handler
        proc = child.proc
        started = time.monotonic()
        sup.stop()
        thread.join(30)
        self.assertIsNotNone(proc.poll())
        self.assertLess(time.monotonic() - started, 20)

    def test_children_for_run(self):
        args = type("A", (), dict(host="127.0.0.1", port=1234, access_log=True, only=None))()
        env = {"VOLT_DB": "postgresql://u:topsecret@h/d"}
        kids = supervisor.build_children(args, env)
        self.assertEqual([k.name for k in kids], ["serve", "runner"])  # no VOLT_ROOM_URL: no clock
        for k in kids:
            self.assertNotIn("topsecret", " ".join(k.argv))
            self.assertEqual(k.env["VOLT_DB"], env["VOLT_DB"])
        self.assertIn("--access-log", kids[0].argv)
        env["VOLT_ROOM_URL"] = "http://127.0.0.1:1"
        self.assertEqual([k.name for k in supervisor.build_children(args, env)], ["serve", "runner", "clock"])
        args.only = "runner"
        self.assertEqual([k.name for k in supervisor.build_children(args, env)], ["runner"])


class RunEndToEnd(unittest.TestCase):
    """Starts the real `volt run` (serve and runner as child processes) on a free port and a temp database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.logs = self.dir / "logs"
        self.port = free_port()
        env = dict(os.environ, VOLT_PROXY_DIR=str(self.dir / "proxy"), VOLT_OUT=str(self.dir / "out"),
                   VOLT_MIN_FREE_MB="1", VOLT_PYTHON_UNBUFFERED="1")
        for name in ("VOLT_TOKEN", "VOLT_ROOM_URL", "VOLT_DB", "VOLT_LOG_DIR"):
            env.pop(name, None)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
        self.output = open(self.dir / "run.out", "wb")
        self.addCleanup(self.output.close)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "volt", "run", "--db", str(self.dir / "v.db"), "--port", str(self.port),
             "--log-dir", str(self.logs), "--stop-grace", "10"],
            cwd=CONTROL, env=env, stdout=self.output, stderr=subprocess.STDOUT, creationflags=flags)
        self.addCleanup(self._cleanup)

    def _child_pids(self):
        text = (self.logs / "run.log").read_text(encoding="utf-8") if (self.logs / "run.log").exists() else ""
        return [int(m) for m in re.findall(r"started child=\w+ pid=(\d+)", text)]

    def _cleanup(self):
        # Only processes this test started: the supervisor and the pids it logged.
        for pid in self._child_pids():
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if self.proc.poll() is None:
            supervisor.kill_tree(self.proc)
        self.proc.wait(timeout=30)
        time.sleep(0.5)  # let Windows release the log files before the temp directory is removed

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def wait_for(self, predicate, timeout=60.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                if predicate():
                    return True
            except (OSError, ValueError):
                pass
            if self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def log_text(self, name):
        path = self.logs / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def stop_supervisor(self):
        if IS_WINDOWS:
            try:
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError:
                self.proc.terminate()
        else:
            self.proc.send_signal(signal.SIGTERM)

    def test_run_serves_beats_restarts_a_killed_child_and_stops_cleanly(self):
        self.assertTrue(self.wait_for(lambda: self.get("/health")[0] == 200),
                        (self.dir / "run.out").read_text(errors="replace"))
        body = json.loads(self.get("/health")[1])
        self.assertEqual(body["version"], volt.__version__)

        def runner_alive():
            report = json.loads(self.get("/api/health")[1])
            return report["runners"]["alive"] == 1 and report["status"] == "ok"
        self.assertTrue(self.wait_for(runner_alive), self.log_text("runner.log") + self.log_text("run.log") + (self.dir / "run.out").read_text(errors="replace"))
        for name in ("run.log", "serve.log", "runner.log"):
            self.assertTrue((self.logs / name).exists(), name)

        def runner_names():
            return {r["name"] for r in json.loads(self.get("/api/health")[1])["runners"]["runners"]}
        before = runner_names()
        runner_pid = self._child_pids()[-1]  # started last: the runner
        os.kill(runner_pid, signal.SIGTERM)
        self.assertTrue(self.wait_for(lambda: "restarting in" in self.log_text("run.log")), self.log_text("run.log"))
        self.assertTrue(self.wait_for(lambda: len(self._child_pids()) >= 3), self.log_text("run.log"))
        self.assertNotEqual(self._child_pids()[-1], runner_pid)

        def new_runner_beating():  # a runner the health report has not seen before, heartbeating now
            rows = json.loads(self.get("/api/health")[1])["runners"]["runners"]
            return any(r["name"] not in before and r["alive"] for r in rows)
        self.assertTrue(self.wait_for(new_runner_beating), self.log_text("run.log"))

        self.stop_supervisor()
        try:
            code = self.proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("volt run did not stop")
        run_log = self.log_text("run.log")
        self.assertEqual(code, 0, run_log)
        if not IS_WINDOWS:
            self.assertIn("stopped child=serve", run_log)
            self.assertIn("stopped child=runner", run_log)
        self.assertIn("volt run ended", run_log)
        self.assertIn("serve stopped", self.log_text("serve.log"))
        with self.assertRaises(OSError):
            self.get("/health")


if __name__ == "__main__":
    unittest.main()
