"""Logging setup and redaction (docs/operations.md, Logs)."""

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from volt import logs


class Redaction(unittest.TestCase):
    def test_secrets_never_survive(self):
        secret_cases = {
            "VOLT_TOKEN=abcdefgh12345678 started": "abcdefgh12345678",
            "dsn postgresql://volt:hunter2pw@db.example:5432/volt ok": "hunter2pw",
            "Authorization: Bearer abc.def-ghi_123": "abc.def-ghi_123",
            "sent header Bearer zzzzzzzzzzzz": "zzzzzzzzzzzz",
            "{'token': 'tok_live_9999'}": "tok_live_9999",
            '{"password": "p4ssw0rd!"}': "p4ssw0rd!",
            "GET /x?sig=SIGVALUE123&se=2026 HTTP/1.1": "SIGVALUE123",
            "AccountKey=QUJDREVGRw==;EndpointSuffix=core": "QUJDREVGRw==",
            "key AKIAABCDEFGHIJKLMNOP seen": "AKIAABCDEFGHIJKLMNOP",
            "host=db user=u password=pw-secret-1": "pw-secret-1",
            "X-Amz-Signature=deadbeefcafe": "deadbeefcafe",
            "aws_secret_access_key=wJalrXUtnFEMI": "wJalrXUtnFEMI",
        }
        for text, secret in secret_cases.items():
            with self.subTest(text=text):
                out = logs.redact(text)
                self.assertNotIn(secret, out)
                self.assertIn(logs.MASK, out)

    def test_ordinary_text_is_untouched(self):
        for text in ("no token: loopback only", "serve started port=8765 db=C:\\volt\\volt.db",
                     "job start id=1 type=proxy attempt=1", "(token required)", "token_type=x"):
            with self.subTest(text=text):
                self.assertEqual(logs.redact(text), text)

    def test_the_configured_token_is_blanked_even_in_odd_places(self):
        env = {"VOLT_TOKEN": "a-very-long-operator-token", "VOLT_DB": "postgres://u:dbpassword@h/d"}
        out = logs.redact("weird /path/a-very-long-operator-token/dbpassword end", env)
        self.assertNotIn("a-very-long-operator-token", out)
        self.assertNotIn("dbpassword", out)

    def test_idempotent(self):
        once = logs.redact("password=abc123456")
        self.assertEqual(logs.redact(once), once)

    def test_dsn_helper(self):
        shown = logs.redact_dsn("postgresql://volt:secretpw@host:5432/volt")
        self.assertEqual(shown, "postgresql://volt:***@host:5432/volt")


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = logging.getLogger("volt")
        self._level, self._prop = self.root.level, self.root.propagate

    def tearDown(self):
        for handler in list(self.root.handlers):
            if getattr(handler, logs._MARK, False):
                self.root.removeHandler(handler)
                handler.close()
        self.root.setLevel(self._level)
        self.root.propagate = self._prop
        self.tmp.cleanup()

    def test_stderr_and_rotating_file_with_redaction(self):
        stream = io.StringIO()
        path = logs.setup("serve", log_dir=self.tmp.name, env={}, stream=stream)
        self.assertEqual(path, Path(self.tmp.name) / "serve.log")
        logging.getLogger("volt.serve").info("login token=supersecretvalue user=ann")
        try:
            raise RuntimeError("connect postgresql://u:pw123456@h/d failed")
        except RuntimeError:
            logging.getLogger("volt.serve").exception("boom")
        for handler in self.root.handlers:
            handler.flush()
        file_text = path.read_text(encoding="utf-8")
        for text in (stream.getvalue(), file_text):
            self.assertIn("user=ann", text)
            self.assertIn("level=INFO", text)
            self.assertIn("RuntimeError", text)
            self.assertNotIn("supersecretvalue", text)
            self.assertNotIn("pw123456", text)

    def test_json_format(self):
        stream = io.StringIO()
        logs.setup("runner", fmt="json", env={}, stream=stream)
        logging.getLogger("volt.runner").warning("hello Bearer abcdefghijkl")
        record = json.loads(stream.getvalue().strip().splitlines()[-1])
        self.assertEqual(record["level"], "WARNING")
        self.assertEqual(record["component"], "runner")
        self.assertNotIn("abcdefghijkl", record["msg"])

    def test_rotation_keeps_the_configured_backups(self):
        env = {"VOLT_LOG_MAX_MB": "0.001", "VOLT_LOG_BACKUPS": "2"}
        path = logs.setup("run", log_dir=self.tmp.name, env=env, stream=io.StringIO())
        for i in range(200):
            logging.getLogger("volt.run").info("line %d %s", i, "x" * 80)
        names = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.assertEqual(names, ["run.log", "run.log.1", "run.log.2"])
        self.assertTrue(path.exists())

    def test_level_and_second_call(self):
        stream = io.StringIO()
        logs.setup("serve", level="warning", env={}, stream=stream)
        logs.setup("serve", level="warning", env={}, stream=stream)  # replaces, does not double
        logging.getLogger("volt.x").info("quiet")
        logging.getLogger("volt.x").warning("loud")
        lines = stream.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("loud", lines[0])

    def test_bad_settings_are_refused(self):
        with self.assertRaises(ValueError):
            logs.setup("serve", level="loud", env={}, stream=io.StringIO())
        with self.assertRaises(ValueError):
            logs.setup("serve", fmt="xml", env={}, stream=io.StringIO())

    def test_access_log_is_opt_in(self):
        self.assertFalse(logs.access_log_enabled({}))
        self.assertTrue(logs.access_log_enabled({"VOLT_ACCESS_LOG": "1"}))


if __name__ == "__main__":
    unittest.main()
