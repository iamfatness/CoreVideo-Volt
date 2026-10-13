"""Shot detection, split and merge (slice 3 of docs/specs/shot-spans.md).

Clips with known cuts are built with ffmpeg (different test patterns butted end to end at exact frame counts, at
25 fps and at 29.97 drop-frame), then run through the real handler. The behaviour is written once as mixins and
run on SQLite always and on Postgres when VOLT_TEST_PG_DSN is set."""

import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

from volt import shots, timecode
from volt.rundown import add_item, attach
from volt.runner import Config, Runner, default_handlers
from volt.shots import ShotError
from volt.store import Store

from tests.pgutil import PgCase

PATTERNS = ("testsrc", "smptebars", "rgbtestsrc", "yuvtestsrc")


def make_clip(path: Path, rate: str, lengths, drop_start=None) -> list[int]:
    """Butt `lengths` frames of different test patterns end to end. Returns the cut frames (the start of every
    pattern after the first), i.e. the answer detection has to find."""
    inputs, chain = [], []
    for i, n in enumerate(lengths):
        pattern = PATTERNS[i % len(PATTERNS)]
        inputs += ["-f", "lavfi", "-i", f"{pattern}=s=160x90:r={rate}:d={n / float(Fraction(rate)) + 2:.3f}"]
        chain.append(f"[{i}:v]trim=end_frame={n},setpts=PTS-STARTPTS,format=yuv420p[v{i}]")
    graph = ";".join(chain) + ";" + "".join(f"[v{i}]" for i in range(len(lengths))) + f"concat=n={len(lengths)}:v=1[o]"
    subprocess.check_call(
        ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[o]", "-r", rate,
         "-c:v", "libx264", "-g", "50", str(path)])
    cuts, at = [], 0
    for n in lengths[:-1]:
        at += n
        cuts.append(at)
    return cuts


class Behaviour:
    """Provides self.open_store(). Each test gets its own database and its own directory."""

    def setUp(self):
        super().setUp()
        self.store = self.open_store()
        self.addCleanup(self.store.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "out"
        self.config = Config(out_dir=self.out)

    # -- fixtures
    def asset(self, lengths=(60, 50, 70), rate="25", start="01:00:00:00", cleared=False, hold=False,
              open_file=False, name="a.mp4", duration="known"):
        path = self.dir / name
        self.cuts = make_clip(path, rate, lengths)
        asset = self.store.mint_asset(owner="desk", camera="iso-3")
        if cleared:
            self.store.set_cleared(asset.id, True)
        if hold:
            self.store.set_hold(asset.id, True)
        self.store.add_essence(asset.id, role="hi-res", location=str(path), frame_rate=rate, timecode_start=start,
                               open_file=open_file, duration_frames=sum(lengths) if duration == "known" else None)
        return asset

    def shot_rows(self, asset):
        return self.store.conn.execute(
            "SELECT id, frame_in, frame_out, tc_in, tc_out, origin, run_id FROM span "
            "WHERE asset_id = ? AND kind = 'shot' ORDER BY frame_in", (asset.id,)).fetchall()

    def bounds(self, asset, start_label="01:00:00:00", rate="25"):
        base = timecode.tc_to_frames(start_label, timecode.parse_rate(rate))
        return [(r["frame_in"] - base, r["frame_out"] - base) for r in self.shot_rows(asset)]

    def runner(self, name="r1", **kw):
        return Runner(self.store, default_handlers(self.config), name=name, **kw)

    def detect(self, asset, threshold=None, min_frames=None):
        job, done = shots.request_detect(self.store, asset.id, threshold, min_frames)
        outcome = self.runner().drain()
        return job, done, outcome

    def code(self, fn, *a, **kw):
        with self.assertRaises(ShotError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def runs(self, asset):
        return self.store.conn.execute("SELECT * FROM shot_run WHERE asset_id = ?", (asset.id,)).fetchall()

    def attach_to_item(self, span_id):
        if self.store.conn.execute("SELECT id FROM show LIMIT 1").fetchone() is None:
            self.store.conn.execute(
                "INSERT INTO show (id, title, created_at) VALUES ('s1', 'Show', '2026-01-01T00:00:00Z')")
            self.store.conn.commit()
        attach(self.store, add_item(self.store, "s1", "slug"), span_id)


class Detect(Behaviour):
    def test_the_runner_registers_the_job_type(self):
        self.assertIn("shots", default_handlers(Config()))

    def test_every_cut_is_found_on_the_frame_at_25_fps_and_none_is_invented(self):
        a = self.asset((60, 50, 70, 40))
        _, done, outcome = self.detect(a)
        self.assertFalse(done)
        self.assertEqual(outcome, {"done": 1})
        self.assertEqual(self.bounds(a), [(0, 60), (60, 110), (110, 180), (180, 220)])
        self.assertEqual(self.cuts, [60, 110, 180])
        first, last = self.shot_rows(a)[0], self.shot_rows(a)[-1]
        self.assertEqual((first["tc_in"], last["tc_out"]), ("01:00:00:00", "01:00:08:20"))
        self.assertEqual({r["origin"] for r in self.shot_rows(a)}, {"detect"})

    def test_a_clip_with_no_cuts_is_one_shot(self):
        a = self.asset((150,))
        self.detect(a)
        self.assertEqual(self.bounds(a), [(0, 150)])

    def test_29_97_drop_frame_cuts_land_on_the_right_labels_across_a_minute(self):
        # 1802 frames in, the first label of the new minute is ;04 because ;00 and ;01 do not exist
        a = self.asset((1802, 60, 40), rate="30000/1001", start="01:00:00;00")
        self.detect(a)
        rows = self.shot_rows(a)
        base = timecode.tc_to_frames("01:00:00;00", timecode.parse_rate("30000/1001"))
        self.assertEqual([(r["frame_in"] - base, r["frame_out"] - base) for r in rows],
                         [(0, 1802), (1802, 1862), (1862, 1902)])
        self.assertEqual(rows[1]["tc_in"], "01:01:00;04")  # worked by hand, not by the function under test
        self.assertEqual(rows[0]["tc_in"], "01:00:00;00")
        self.assertEqual(rows[0]["tc_out"], "01:01:00;04")
        self.assertEqual(rows[2]["tc_out"], "01:01:03;14")  # 102 frames past the minute: 3 s 12 f, plus the two skipped labels

    def test_a_flash_shorter_than_min_frames_is_not_a_shot(self):
        a = self.asset((60, 5, 70))
        self.detect(a, min_frames=12)
        self.assertEqual(len(self.bounds(a)), 2)  # the 5-frame flash is merged into the one before it
        b = self.asset((60, 5, 70), name="b.mp4")
        self.detect(b, min_frames=3)
        self.assertEqual(self.bounds(b), [(0, 60), (60, 65), (65, 135)])

    def test_merge_short_rule(self):
        # the first shot has no previous one: it merges forward
        self.assertEqual(shots.merge_short([4, 40], 100, 12), [40])
        # a short middle shot joins the previous; the cut after it stays
        self.assertEqual(shots.merge_short([40, 45, 100], 200, 12), [45, 100])
        # a short last shot joins the previous one
        self.assertEqual(shots.merge_short([40, 95], 100, 12), [40])
        # cuts at or past the ends and repeats are ignored
        self.assertEqual(shots.merge_short([0, 40, 40, 100, 130], 100, 12), [40])
        self.assertEqual(shots.merge_short([], 100, 12), [])
        # exactly min_frames long is long enough
        self.assertEqual(shots.merge_short([12], 100, 12), [12])

    def test_a_high_threshold_finds_fewer_cuts_and_the_run_says_what_it_used(self):
        a = self.asset((60, 50, 70))
        self.detect(a, threshold=0.999)
        run = self.runs(a)[0]
        self.assertEqual((run["engine"], run["threshold"], run["min_frames"]), ("ffmpeg-scene", 0.999, 12))
        self.assertEqual(run["shot_count"], len(self.shot_rows(a)))
        settings = json.loads(run["settings"])
        self.assertEqual((settings["threshold"], settings["minFrames"], settings["frames"]), (0.999, 12, 180))
        self.assertEqual(settings["source"], "hi-res")
        self.assertTrue(run["engine_version"])

    def test_same_settings_twice_is_one_run_and_no_duplicates(self):
        a = self.asset()
        job, done, _ = self.detect(a)
        again, done_again = shots.request_detect(self.store, a.id)
        self.assertEqual((again.id, done_again), (job.id, True))
        self.assertEqual(self.runner().drain(), {})  # nothing to claim
        self.assertEqual(len(self.shot_rows(a)), 3)
        self.assertEqual(len(self.runs(a)), 1)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM job WHERE type = 'shots' AND asset_id = ?", (a.id,)).fetchone()[0], 1)

    def test_replaying_the_handler_for_the_same_job_does_not_duplicate(self):
        a = self.asset()
        job, _, _ = self.detect(a)
        for _ in range(2):  # at-least-once delivery: the same job may run again
            shots.detect(self.store, a.id, job_id=job.id, out_dir=self.out)
        self.assertEqual(len(self.shot_rows(a)), 3)
        self.assertEqual(len(self.runs(a)), 1)

    def test_a_different_setting_is_a_new_run_that_replaces_only_its_own_output(self):
        a = self.asset((60, 50, 70, 40))
        self.detect(a)
        old = {r["id"] for r in self.shot_rows(a)}
        # an attached detected shot survives a re-run; the unattached ones are replaced by the new run's
        used = self.shot_rows(a)[1]["id"]
        self.attach_to_item(used)
        job, done, outcome = self.detect(a, min_frames=20)
        self.assertEqual((done, outcome), (False, {"done": 1}))
        rows = self.shot_rows(a)
        self.assertIn(used, {r["id"] for r in rows})
        self.assertEqual(old & {r["id"] for r in rows}, {used})  # everything else was replaced
        self.assertEqual(self.bounds(a), [(0, 60), (60, 110), (110, 180), (180, 220)])  # still tiles, no overlap
        self.assertEqual(len(self.runs(a)), 2)
        settings = json.loads(self.store.conn.execute(
            "SELECT settings FROM shot_run WHERE job_id = ?", (job.id,)).fetchone()["settings"])
        self.assertEqual((settings["replaced"], settings["kept"]), (3, {"manual": 0, "detect": 1}))

    def test_manual_shots_and_clips_survive_and_detection_fills_around_them(self):
        a = self.asset((60, 50, 70))
        manual = shots.create(self.store, a.id, "shot", "01:00:01:00", "01:00:02:00", label="mine")
        clip = shots.create(self.store, a.id, "clip", "01:00:00:10", "01:00:05:00")
        self.detect(a)
        rows = self.shot_rows(a)
        self.assertIn(manual["id"], {r["id"] for r in rows})
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM span WHERE id = ?", (clip["id"],)).fetchone()[0], 1)
        for (_, out), (nxt, _) in zip(self.bounds(a), self.bounds(a)[1:]):
            self.assertLessEqual(out, nxt)  # never an overlap
        covered = sum(o - i for i, o in self.bounds(a))
        self.assertEqual(covered, 180)  # and still the whole file
        self.assertEqual({r["origin"] for r in rows}, {"detect", "manual"})
        settings = json.loads(self.runs(a)[0]["settings"])
        self.assertEqual(settings["kept"], {"manual": 1, "detect": 0})

    def test_a_legal_hold_and_an_uncleared_asset_are_detected_like_any_other(self):
        held = self.asset(hold=True, cleared=False)
        cleared_held = self.asset(hold=True, cleared=True, name="b.mp4")
        for a in (held, cleared_held):
            _, _, outcome = self.detect(a)
            self.assertEqual(outcome, {"done": 1})
            self.assertEqual(len(self.shot_rows(a)), 3)

    def test_an_open_essence_is_deferred_not_failed_and_no_attempt_is_used(self):
        a = self.asset(open_file=True)
        self.assertEqual(self.code(shots.request_detect, self.store, a.id), "essence_open")
        # a job queued before the file was open when it was queued must wait too
        job = self.store.enqueue(a.id, "shots", shots.detect_key(0.13, 12))
        self.assertEqual(self.runner().drain(), {"deferred": 1})
        row = self.store.conn.execute("SELECT status, attempt, run_after FROM job WHERE id = ?", (job.id,)).fetchone()
        self.assertEqual((row["status"], row["attempt"]), ("queued", 1))
        self.assertIsNotNone(row["run_after"])
        self.assertEqual(self.shot_rows(a), [])

    def test_missing_media_and_bad_timebase_fail_the_job_for_good(self):
        a = self.asset()
        Path(self.store.conn.execute("SELECT location FROM essence WHERE asset_id = ?", (a.id,)).fetchone()[0]).unlink()
        job, _ = shots.request_detect(self.store, a.id)
        self.assertEqual(self.runner().drain(), {"failed": 1})
        row = self.store.conn.execute("SELECT status, error FROM job WHERE id = ?", (job.id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("not on disk", row["error"])
        bare = self.store.mint_asset(owner="desk")
        self.store.add_essence(bare.id, role="hi-res", location=str(self.dir / "a.mp4"))  # no frame rate
        self.assertEqual(self.code(shots.request_detect, self.store, bare.id), "bad_input")
        self.assertEqual(self.code(shots.request_detect, self.store, "nope"), "not_found")

    def test_settings_are_validated_not_clamped(self):
        a = self.asset()
        for bad in ({"threshold": 0}, {"threshold": 1}, {"threshold": "0.3"}, {"threshold": True},
                    {"min_frames": 0}, {"min_frames": 2.5}, {"min_frames": 10**9}):
            self.assertEqual(self.code(shots.request_detect, self.store, a.id, **bad), "bad_input", bad)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM job WHERE type = 'shots'").fetchone()[0], 0)

    def test_the_job_key_carries_the_settings_and_changes_with_them(self):
        k1, k2 = shots.detect_key(0.13, 12), shots.detect_key(0.13, 13)
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(shots.detect_key(0.13, 12), shots.detect_key(0.14, 12))
        self.assertNotEqual(shots.detect_key(0.13, 12), shots.detect_key(0.13, 12, version="other"))
        self.assertEqual(shots.parse_detect_key(k1), (0.13, 12))
        for bad in ("shots-v1:abc", "asr:x", "shots-v1:abc:0.13:x", "shots-v1:abc:2:12"):
            self.assertEqual(self.code(shots.parse_detect_key, bad), "bad_input", bad)

    def test_a_hi_res_with_no_recorded_length_is_probed_and_the_length_kept(self):
        a = self.asset(duration="unknown")
        self.detect(a)
        self.assertEqual(self.store.conn.execute(
            "SELECT duration_frames FROM essence WHERE asset_id = ?", (a.id,)).fetchone()[0], 180)
        self.assertEqual(len(self.shot_rows(a)), 3)

    # -- thumbnails
    def test_a_thumbnail_is_written_per_shot_at_the_frame_rule_path(self):
        a = self.asset()
        self.detect(a)
        for r in self.shot_rows(a):
            frame = shots.thumb_frame(r["frame_in"], r["frame_out"])
            path = shots.thumb_path(self.out, a.id, r["id"], frame)
            self.assertTrue(path.is_file() and path.stat().st_size > 0, path)
            self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")  # a jpeg
        self.assertEqual(json.loads(self.runs(a)[0]["settings"])["thumbnails"], {"made": 3, "failed": 0})

    def test_the_frame_rule_and_path(self):
        self.assertEqual(shots.thumb_frame(100, 200), 110)     # 10% of 100
        self.assertEqual(shots.thumb_frame(100, 130), 103)     # 10% is 3
        self.assertEqual(shots.thumb_frame(100, 120), 102)     # at least 2 past the cut
        self.assertEqual(shots.thumb_frame(100, 102), 101)     # kept inside the span
        self.assertEqual(shots.thumb_frame(100, 101), 100)     # a one-frame span shows its only frame
        self.assertEqual(shots.thumb_path("/o", "A", "S", 7).as_posix(), "/o/thumbs/A/S-7.jpg")

    def test_a_thumbnail_failure_is_counted_and_does_not_undo_the_shots(self):
        a = self.asset()
        real = subprocess.run

        def run(cmd, *args, **kw):
            if "-frames:v" in cmd:
                raise subprocess.TimeoutExpired(cmd, 15)
            return real(cmd, *args, **kw)

        job, _ = shots.request_detect(self.store, a.id)
        shots.detect(self.store, a.id, job_id=job.id, out_dir=self.out, run=run)
        self.assertEqual(len(self.shot_rows(a)), 3)
        self.assertEqual(json.loads(self.runs(a)[0]["settings"])["thumbnails"], {"made": 0, "failed": 3})
        self.assertEqual(list((self.out / "thumbs" / a.id).glob("*")) if (self.out / "thumbs" / a.id).exists() else [], [])

    # -- the proxy
    def proxy(self, a, lengths, rate="25", name="p.mp4"):
        path = self.dir / name
        make_clip(path, rate, lengths)
        self.store.add_essence(a.id, role="proxy", location=str(path))
        return path

    def test_a_matching_proxy_is_read_instead_of_the_hi_res(self):
        a = self.asset((60, 50, 70))
        self.proxy(a, (60, 50, 70))
        self.detect(a)
        self.assertEqual(json.loads(self.runs(a)[0]["settings"])["source"], "proxy")
        self.assertEqual(self.bounds(a), [(0, 60), (60, 110), (110, 180)])

    def test_a_proxy_with_another_frame_count_or_rate_is_not_trusted(self):
        a = self.asset((60, 50, 70))
        self.proxy(a, (60, 50, 60))
        self.detect(a)
        settings = json.loads(self.runs(a)[0]["settings"])
        self.assertEqual(settings["source"], "hi-res")
        self.assertIn("frames", settings["sourceNote"])
        b = self.asset((60, 50, 70), name="b.mp4")
        self.proxy(b, (60, 50, 70), rate="30", name="q.mp4")
        self.detect(b)
        settings = json.loads(self.runs(b)[0]["settings"])
        self.assertEqual(settings["source"], "hi-res")
        self.assertIn("frame rate", settings["sourceNote"])

    def test_a_growing_or_missing_proxy_is_not_read(self):
        a = self.asset()
        path = self.proxy(a, (60, 50, 70))
        self.store.conn.execute("UPDATE essence SET open = TRUE WHERE asset_id = ? AND role = 'proxy'", (a.id,))
        self.store.conn.commit()
        self.detect(a)
        self.assertEqual(json.loads(self.runs(a)[0]["settings"])["source"], "hi-res")
        path.unlink()
        self.store.conn.execute("UPDATE essence SET open = FALSE WHERE asset_id = ? AND role = 'proxy'", (a.id,))
        self.store.conn.commit()
        self.detect(a, min_frames=13)
        latest = self.store.conn.execute("SELECT settings FROM shot_run WHERE min_frames = 13").fetchone()
        settings = json.loads(latest["settings"])
        self.assertEqual((settings["source"], settings["sourceNote"]), ("hi-res", "the proxy file is not on disk"))


class Recovery(Behaviour):
    """A crashed or slow runner must not leave a half run, and a runner that lost its lease must not write."""

    def clock(self, base):
        return lambda: base

    def test_a_runner_that_dies_mid_job_is_recovered_and_the_run_is_whole(self):
        a = self.asset()
        job, _ = shots.request_detect(self.store, a.id)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        dead = Runner(self.store, default_handlers(self.config), name="dead", lease=60, clock=lambda: t0)
        claimed = dead.claim()
        self.assertEqual((claimed.id, claimed.claimed_by), (job.id, "dead"))  # ...and it is never heard from again
        later = lambda: t0 + timedelta(seconds=120)  # noqa: E731
        live = Runner(self.store, default_handlers(self.config), name="live", lease=60, clock=later)
        self.assertEqual(live.drain(), {"done": 1})
        self.assertEqual(len(self.shot_rows(a)), 3)
        self.assertEqual(len(self.runs(a)), 1)
        row = self.store.conn.execute("SELECT status, attempt FROM job WHERE id = ?", (job.id,)).fetchone()
        self.assertEqual((row["status"], row["attempt"]), ("done", 2))  # the lost run counted as an attempt

    def test_a_write_that_fails_halfway_changes_nothing_and_the_retry_succeeds(self):
        a = self.asset()
        job, _ = shots.request_detect(self.store, a.id)
        real, calls = self.store.add_span, []

        def flaky(*args, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("disk went away")
            return real(*args, **kw)

        self.store.add_span = flaky
        runner = self.runner(backoff=0.0)
        self.assertEqual(runner.run_job(runner.claim()), "retry")
        self.assertEqual(self.shot_rows(a), [])      # the first span was rolled back with the rest
        self.assertEqual(self.runs(a), [])
        self.store.add_span = real
        self.assertEqual(runner.drain(), {"done": 1})
        self.assertEqual(len(self.shot_rows(a)), 3)
        self.assertEqual(len(self.runs(a)), 1)

    def test_a_runner_that_lost_its_lease_writes_nothing(self):
        a = self.asset()
        job, _ = shots.request_detect(self.store, a.id)
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        slow = Runner(self.store, default_handlers(self.config), name="slow", lease=60, clock=lambda: t0)
        claimed = slow.claim()
        # while "slow" is still scoring the file, its lease lapses and another runner takes the job
        quick = Runner(self.store, default_handlers(self.config), name="quick", lease=60,
                       clock=lambda: t0 + timedelta(seconds=120))
        took = quick.claim()
        self.assertEqual(took.id, job.id)
        result = shots.detect(self.store, a.id, job_id=job.id, claimed_by="slow", out_dir=self.out)
        self.assertEqual(result, {"lost": True})
        self.assertEqual(self.shot_rows(a), [])
        self.assertEqual(self.runs(a), [])
        # and through the runner: its outcome is not recorded either
        self.assertEqual(slow.run_job(claimed), "lost")
        self.assertEqual(self.shot_rows(a), [])
        result = shots.detect(self.store, a.id, job_id=job.id, claimed_by="quick", out_dir=self.out)
        self.assertEqual(result["shots"], 3)

    def test_a_job_that_is_not_running_is_not_written(self):
        a = self.asset()
        job, _ = shots.request_detect(self.store, a.id)  # queued, nobody holds it
        self.assertEqual(shots.detect(self.store, a.id, job_id=job.id, claimed_by="r1", out_dir=self.out), {"lost": True})
        self.assertEqual(self.shot_rows(a), [])


class Edits(Behaviour):
    def three(self):
        a = self.asset()
        self.detect(a)
        return a, [r["id"] for r in self.shot_rows(a)]

    # -- split
    def test_split_makes_two_shots_that_tile_the_old_one(self):
        a, ids = self.three()
        left, right = shots.split(self.store, ids[0], "01:00:01:10")
        self.assertEqual((left["id"], left["tcIn"], left["tcOut"]), (ids[0], "01:00:00:00", "01:00:01:10"))
        self.assertEqual((right["tcIn"], right["tcOut"]), ("01:00:01:10", "01:00:02:10"))
        self.assertNotEqual(right["id"], ids[0])
        self.assertEqual((left["frameOut"], right["frameIn"]), (right["frameIn"], left["frameOut"]))
        self.assertEqual((left["origin"], right["origin"], left["runId"], right["runId"]),
                         ("manual", "manual", None, None))
        self.assertEqual(len(self.shot_rows(a)), 4)

    def test_split_keeps_the_label_on_the_first_half_only(self):
        a, ids = self.three()
        shots.label(self.store, ids[0], "Opening")
        left, right = shots.split(self.store, ids[0], "01:00:01:00")
        self.assertEqual((left["label"], right["label"]), ("Opening", None))

    def test_split_refuses_the_edges_outside_and_other_kinds_and_changes_nothing(self):
        a, ids = self.three()
        for at in ("01:00:00:00", "01:00:02:10", "01:00:09:00", "00:59:00:00"):
            self.assertIn(self.code(shots.split, self.store, ids[0], at), ("bad_input", "bad_timecode"), at)
        self.assertEqual(self.code(shots.split, self.store, ids[0], "01:00:01:99"), "bad_timecode")
        clip = shots.create(self.store, a.id, "clip", "01:00:00:05", "01:00:00:20")
        self.assertEqual(self.code(shots.split, self.store, clip["id"], "01:00:00:10"), "not_editable")
        self.assertEqual(self.code(shots.split, self.store, "nope", "01:00:00:10"), "not_found")
        self.assertEqual(self.bounds(a), [(0, 60), (60, 110), (110, 180)])

    def test_split_of_a_shot_in_use_is_refused_and_changes_nothing(self):
        a, ids = self.three()
        self.attach_to_item(ids[1])
        self.assertEqual(self.code(shots.split, self.store, ids[1], "01:00:03:00"), "span_in_use")
        self.assertEqual(self.bounds(a), [(0, 60), (60, 110), (110, 180)])

    def test_split_on_a_drop_frame_essence_uses_drop_frame_labels(self):
        a = self.asset((1900,), rate="30000/1001", start="01:00:00;00")
        self.detect(a)
        sid = self.shot_rows(a)[0]["id"]
        left, right = shots.split(self.store, sid, "01:01:00;02")
        self.assertEqual(right["frameIn"] - left["frameIn"], 1800)
        self.assertEqual((left["tcOut"], right["tcIn"]), ("01:01:00;02", "01:01:00;02"))
        self.assertEqual(self.code(shots.split, self.store, left["id"], "01:01:00;00"), "bad_timecode")  # not a real label

    # -- merge
    def test_merge_joins_two_neighbours(self):
        a, ids = self.three()
        merged = shots.merge(self.store, ids[0], ids[1])
        self.assertEqual((merged["id"], merged["tcIn"], merged["tcOut"]), (ids[0], "01:00:00:00", "01:00:04:10"))
        self.assertEqual((merged["origin"], merged["runId"]), ("manual", None))
        self.assertEqual(self.bounds(a), [(0, 110), (110, 180)])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM span WHERE id = ?", (ids[1],)).fetchone()[0], 0)

    def test_merge_refusals_change_nothing(self):
        a, ids = self.three()
        self.assertEqual(self.code(shots.merge, self.store, ids[0], ids[2]), "not_adjacent")   # not the next one
        self.assertEqual(self.code(shots.merge, self.store, ids[1], ids[0]), "not_adjacent")   # the wrong way round
        self.assertEqual(self.code(shots.merge, self.store, ids[0], ids[0]), "not_adjacent")
        self.assertEqual(self.code(shots.merge, self.store, ids[0], "nope"), "not_found")
        shots.delete(self.store, ids[1])  # leaves a gap between the first and last
        self.assertEqual(self.code(shots.merge, self.store, ids[0], ids[2]), "not_adjacent")
        other = self.asset(name="o.mp4")
        self.detect(other)
        self.assertEqual(self.code(shots.merge, self.store, ids[0], self.shot_rows(other)[0]["id"]), "not_adjacent")
        clip = shots.create(self.store, a.id, "clip", "01:00:02:10", "01:00:03:00")
        self.assertEqual(self.code(shots.merge, self.store, ids[0], clip["id"]), "not_editable")
        self.assertEqual(self.bounds(a), [(0, 60), (110, 180)])

    def test_merge_with_either_shot_in_use_is_refused_and_nothing_half_happens(self):
        a, ids = self.three()
        for pinned in (ids[0], ids[1]):
            self.attach_to_item(pinned)
            self.assertEqual(self.code(shots.merge, self.store, ids[0], ids[1]), "span_in_use")
            self.assertEqual(self.bounds(a), [(0, 60), (60, 110), (110, 180)])
            self.store.conn.execute("DELETE FROM item")
            self.store.conn.commit()

    def test_a_split_or_merged_shot_is_not_taken_back_by_the_next_detection(self):
        a, ids = self.three()
        shots.merge(self.store, ids[0], ids[1])
        self.detect(a, min_frames=13)
        rows = {r["id"]: r for r in self.shot_rows(a)}
        self.assertIn(ids[0], rows)
        self.assertEqual((rows[ids[0]]["frame_in"], rows[ids[0]]["origin"]), (self.shot_rows(a)[0]["frame_in"], "manual"))
        self.assertEqual(sum(r["frame_out"] - r["frame_in"] for r in rows.values()), 180)

    def test_two_splits_of_the_same_shot_at_the_same_frame_cannot_both_win(self):
        a, ids = self.three()
        if not hasattr(self, "shared_target"):
            self.skipTest("needs a database more than one connection can open")
        target, results, lock = self.shared_target(), [], threading.Lock()
        self.store.conn.commit()
        barrier = threading.Barrier(2)

        def work():
            s = Store(target)
            try:
                barrier.wait()
                shots.split(s, ids[0], "01:00:01:00")
                outcome = "ok"
            except ShotError as exc:
                outcome = exc.code
            finally:
                s.close()
            with lock:
                results.append(outcome)

        ts = [threading.Thread(target=work) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(results), ["bad_input", "ok"])
        self.assertEqual(self.bounds(a), [(0, 25), (25, 60), (60, 110), (110, 180)])


ALL = (Detect, Recovery, Edits)


class _Sqlite:
    def open_store(self):
        self.tmp_db = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_db.cleanup)
        self.db_path = str(Path(self.tmp_db.name) / "v.db")
        return Store(self.db_path)

    def shared_target(self):
        return self.db_path


class _Pg:
    def open_store(self):
        return self.store()

    def shared_target(self):
        return self.dsn


def _build():
    g = globals()
    for base in ALL:
        g[f"Sqlite{base.__name__}"] = type(f"Sqlite{base.__name__}", (_Sqlite, base, unittest.TestCase), {})
        g[f"Postgres{base.__name__}"] = type(f"Postgres{base.__name__}", (_Pg, base, PgCase), {})


_build()
del Behaviour, Detect, Recovery, Edits  # the abstract mixins must not be collected on their own


if __name__ == "__main__":
    unittest.main()
