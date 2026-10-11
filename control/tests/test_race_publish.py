"""Race 1: two writers publishing one span (volt/publish.py). Two connections, forced interleaving."""

import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

from volt import deliverables
from volt.deliverables import DeliverableError
from volt.publish import publish_files
from volt.runner import Config, Runner, default_handlers
from volt.storage import StoredObject
from volt.store import Store

from tests.racekit import Bg, PgBase, SqliteBase, _cleared_span


# ---- race 1: publish_files from two writers -----------------------------------------------------

class MemBackend:
    """A non-local backend: put_file copies bytes into a dict, so the scratch file is deleted after upload."""

    id = "mem"

    def __init__(self):
        self.objects = {}
        self.puts = []
        self.lock = threading.Lock()

    def put_file(self, local_path, key):
        data = Path(local_path).read_bytes()
        with self.lock:
            self.objects[key] = data
            self.puts.append(key)
        return StoredObject(self.id, key, f"mem://{key}", len(data), "0" * 64)

    def url(self, key, expires=None):
        return f"mem://{key}"

    def exists(self, key):
        return key in self.objects

    def delete(self, key):
        self.objects.pop(key, None)


class FakeRender:
    """Stands in for deliverables.render: writes the cut where the real one does, records every call."""

    def __init__(self):
        self.calls = []
        self.hook = None       # called once, after the file is written and before render returns
        self.fail = set()      # kinds that raise DeliverableError
        self.lock = threading.Lock()

    def __call__(self, store, span, kind, out_dir):
        with self.lock:
            self.calls.append(kind)
        if kind in self.fail:
            raise DeliverableError("simulated render failure")
        dest = Path(out_dir) / span["asset_id"] / f"{span['id']}-{kind}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"cut-" + kind.encode())
        hook, self.hook = self.hook, None
        if hook:
            hook(kind, dest)
        return dest.resolve().as_uri()


class PublishRaceTests:
    def _setup(self):
        self.a, self.b = self.new_store(), self.new_store()
        _, self.span = _cleared_span(self.a)
        self.backend = MemBackend()
        self.scratch = self.root / "scratch"
        self.render = FakeRender()
        patch = mock.patch.object(deliverables, "render", self.render)
        patch.start()
        self.addCleanup(patch.stop)

    def _publish(self, store):
        return publish_files(store, self.span, None, backend=self.backend, scratch=self.scratch)

    def _versions(self):
        rows = self.a.conn.execute("SELECT kind, status FROM version WHERE span_id = ?", (self.span,)).fetchall()
        return sorted((r["kind"], r["status"]) for r in rows)

    def test_a_failed_span_retried_by_two_writers_is_rendered_and_uploaded_once(self):
        self._setup()
        self.render.fail = {"ott"}
        self._publish(self.a)                       # house and social done, ott failed
        self.render.fail = set()
        self.render.calls.clear()
        self.backend.puts.clear()
        seen = {}

        def other_writer(kind, dest):               # the second writer arrives while the first is rendering ott
            seen["b"] = self._publish(self.b)
            seen["dest_survived"] = dest.exists()   # the first writer's scratch file, intact?

        self.render.hook = other_writer
        first = self._publish(self.a)
        self.assertEqual(self.render.calls, ["ott"], "the span was rendered by both writers")
        self.assertEqual(self.backend.puts.count(f"{self.a.conn.execute('SELECT asset_id FROM span').fetchone()[0]}/{self.span}-ott.mp4"), 1)
        self.assertTrue(seen["dest_survived"], "the second writer deleted or clobbered the first one's scratch file")
        self.assertEqual([v.status for v in first], ["done"] * 3)
        self.assertNotEqual({v.kind: v.status for v in seen["b"]}["ott"], "done")  # not claimed by the second writer
        self.assertEqual(self._versions(), [("house", "done"), ("ott", "done"), ("social", "done")])

    def test_a_fresh_span_published_by_two_writers_makes_one_row_and_one_render_per_kind(self):
        self._setup()
        # a render outlasts the 10 s busy timeout, so the second writer gets a short one
        second = {"bg": self.in_thread(self._publish, busy_ms=300)}

        def other_writer(kind, dest):
            second["bg"].wait(1.0)                  # blocked on a lock, or already done: either is fine

        self.render.hook = other_writer
        first = self._publish(self.a)
        second["bg"].finish()
        self.assertIsNone(second["bg"].error, f"the second writer crashed: {second['bg'].error!r}")
        self.assertEqual(sorted(self.render.calls), ["house", "ott", "social"], "a kind was rendered twice")
        self.assertEqual(sorted(k.rsplit("-", 1)[1] for k in self.backend.puts), ["house.mp4", "ott.mp4", "social.mp4"])
        self.assertEqual(self._versions(), [("house", "done"), ("ott", "done"), ("social", "done")])
        self.assertEqual(self.a.conn.execute("SELECT COUNT(*) FROM version").fetchone()[0], 3)
        self.assertTrue(all(v.status in ("done", "busy") for v in first + second["bg"].result))

    def test_a_done_version_is_skipped_and_a_failed_one_retries(self):
        self._setup()
        self.render.fail = {"social"}
        first = self._publish(self.a)
        self.assertEqual({v.kind: v.status for v in first}, {"house": "done", "ott": "done", "social": "failed"})
        self.render.fail = set()
        self.render.calls.clear()
        again = self._publish(self.b)               # a different connection, as another process would be
        self.assertEqual(self.render.calls, ["social"])
        self.assertEqual([v.status for v in again], ["done"] * 3)

    def test_a_lapsed_claim_is_taken_over_and_a_live_one_is_not(self):
        self._setup()
        asset_id = self.a.conn.execute("SELECT asset_id FROM span WHERE id = ?", (self.span,)).fetchone()[0]
        job = self.a.enqueue(asset_id, "publish", f"publish:{self.span}:house")
        self.a.conn.execute(
            "UPDATE job SET status = 'running', claimed_by = 'dead-writer', lease_expires_at = ? WHERE id = ?",
            ("2999-01-01T00:00:00Z", job.id))
        self.a.conn.commit()
        live = self._publish(self.b)
        self.assertNotIn("house", self.render.calls)
        self.assertEqual({v.kind: v.status for v in live}["house"], "busy")
        self.a.conn.execute("UPDATE job SET lease_expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00Z", job.id))
        self.a.conn.commit()
        lapsed = self._publish(self.b)
        self.assertEqual({v.kind: v.status for v in lapsed}["house"], "done")

    def test_the_runner_defers_a_publish_another_writer_holds(self):
        self._setup()
        asset_id = self.a.conn.execute("SELECT asset_id FROM span WHERE id = ?", (self.span,)).fetchone()[0]
        held = self.a.enqueue(asset_id, "publish", f"publish:{self.span}:house")
        self.a.conn.execute(
            "UPDATE job SET status = 'running', claimed_by = 'someone', lease_expires_at = ? WHERE id = ?",
            ("2999-01-01T00:00:00Z", held.id))
        self.a.conn.commit()
        from volt.runner import request_publish
        request_publish(self.b, self.span)
        runner = Runner(self.b, default_handlers(Config(storage=self.backend, storage_scratch=self.scratch)))
        counts = runner.drain()
        self.assertEqual(counts, {"deferred": 1})
        self.assertEqual(self.b.conn.execute(
            "SELECT status FROM job WHERE idempotency_key = ?", (f"publish-request:{self.span}",)).fetchone()[0],
            "queued")

    def test_two_renders_of_one_cut_never_share_an_output_file(self):
        """deliverables.render writes ffmpeg's output to a .part file; two renders must not pick the same one."""
        store = self.new_store()
        asset = store.mint_asset(owner="desk")
        src = self.root / "src.mp4"
        src.write_bytes(b"x")
        store.add_essence(asset.id, role="hi-res", location=str(src), frame_rate="25", timecode_start="01:00:00:00")
        span_id = store.add_span(asset.id, "transcript", "01:00:00:00", "01:00:02:00", text="w")
        span = store.conn.execute("SELECT id, asset_id, tc_in, tc_out FROM span WHERE id = ?", (span_id,)).fetchone()
        outs = []

        def fake_run(cmd, **kw):
            part = Path(cmd[-1])
            outs.append(part)
            part.write_bytes(b"cut")
            if len(outs) == 1:                       # a second render starts while the first one's ffmpeg is running
                deliverables.render(store, span, "house", self.root / "out")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(deliverables.subprocess, "run", fake_run), \
                mock.patch.object(deliverables, "_duration", lambda p: 1.0):
            deliverables.render(store, span, "house", self.root / "out")
        self.assertEqual(len(outs), 2)
        self.assertNotEqual(outs[0], outs[1], "two renders wrote ffmpeg output to the same file")


class PublishRaceSqlite(PublishRaceTests, SqliteBase):
    pass


class PublishRacePg(PublishRaceTests, PgBase):
    pass



if __name__ == "__main__":
    unittest.main()
