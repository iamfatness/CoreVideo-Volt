"""Shot and clip spans (slice 1 of docs/specs/shot-spans.md): the rules, the frame columns, the backfill.

The behaviour is written once as mixins and run on SQLite always and on Postgres when VOLT_TEST_PG_DSN is set."""

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from volt import library, operator_state, shots
from volt.ingest import ingest_file
from volt.record import record_start, wrap
from volt.rundown import add_item, attach
from volt.runner import Config, Runner, default_handlers
from volt.shots import ShotError
from volt.speech import FakeEngine, Word, transcribe
from volt.store import Store

from tests.pgutil import PgCase


def _clip(path: Path, seconds=3, rate=25) -> None:
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=160x90:r={rate}:d={seconds}", "-f", "lavfi", "-i",
         f"sine=f=440:d={seconds}", "-c:a", "aac", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Behaviour:
    """Provides self.open_store(). Each test gets its own database."""

    def setUp(self):
        super().setUp()
        self.store = self.open_store()
        self.addCleanup(self.store.close)

    def asset(self, rate="25", start="01:00:00:00", duration=None, open_file=False, cleared=False):
        asset = self.store.mint_asset(owner="desk", camera="iso-3")
        if cleared:
            self.store.set_cleared(asset.id, True)
        self.store.add_essence(asset.id, role="hi-res", location="media/a.mxf", frame_rate=rate,
                               timecode_start=start, open_file=open_file, duration_frames=duration)
        return asset

    def count(self, asset, kind=None):
        sql, args = "SELECT COUNT(*) FROM span WHERE asset_id = ?", [asset.id]
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        return self.store.conn.execute(sql, args).fetchone()[0]

    def code(self, fn, *a, **kw):
        with self.assertRaises(ShotError) as cm:
            fn(*a, **kw)
        return cm.exception.code

    def attach_to_item(self, span_id):
        show = self.store.conn.execute("SELECT id FROM show LIMIT 1").fetchone()
        if show is None:
            self.store.conn.execute("INSERT INTO show (id, title, created_at) VALUES ('s1', 'Show', '2026-01-01T00:00:00Z')")
            self.store.conn.commit()
        item_id = add_item(self.store, "s1", "slug")
        attach(self.store, item_id, span_id)
        return item_id

    def add_version(self, asset, span_id):
        self.store.conn.execute(
            "INSERT INTO version (id, asset_id, span_id, kind, status, created_at) VALUES (?, ?, ?, 'house', 'done', '2026-01-01T00:00:00Z')",
            (f"v-{span_id}", asset.id, span_id))
        self.store.conn.commit()


class Create(Behaviour):
    def test_a_shot_and_a_clip_are_stored_with_labels_and_frames(self):
        a = self.asset()
        shot = shots.create(self.store, a.id, "shot", "01:00:02:00", "01:00:04:12", label="  Bench  ")
        self.assertEqual((shot["kind"], shot["origin"], shot["label"]), ("shot", "manual", "Bench"))
        self.assertEqual((shot["tcIn"], shot["tcOut"]), ("01:00:02:00", "01:00:04:12"))
        self.assertEqual(shot["frameIn"], 3600 * 25 + 50)
        self.assertEqual(shot["frameOut"], 3600 * 25 + 112)
        self.assertEqual(shot["usedBy"], {"items": 0, "versions": 0})
        clip = shots.create(self.store, a.id, "clip", "01:00:00:00", "01:00:01:00", text="hi", origin="transcript")
        self.assertEqual((clip["kind"], clip["origin"], clip["text"]), ("clip", "transcript", "hi"))
        self.assertEqual(shots.get(self.store, clip["id"]), clip)

    def test_marking_is_allowed_on_any_asset_uncleared_or_on_legal_hold(self):
        a = self.asset(cleared=False)
        self.store.set_hold(a.id, True)
        shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:01:00")
        shots.create(self.store, a.id, "clip", "01:00:00:10", "01:00:01:10")
        self.assertEqual(self.count(a), 2)

    def test_open_essence_is_marked_with_a_lower_bound_only(self):
        a = self.asset(open_file=True)
        shots.create(self.store, a.id, "clip", "01:00:00:00", "05:00:00:00")
        self.assertEqual(self.code(shots.create, self.store, a.id, "clip", "00:59:59:24", "01:00:01:00"),
                         "outside_source")

    def test_refusals_write_nothing(self):
        a = self.asset(duration=250)  # ten seconds at 25 fps
        c = self.code
        s = self.store
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:02:00", "01:00:02:00"), "bad_input")
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:03:00", "01:00:02:00"), "bad_input")
        self.assertEqual(c(shots.create, s, a.id, "shot", "00:59:59:24", "01:00:01:00"), "outside_source")
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:09:00", "01:00:10:01"), "outside_source")
        shots.create(s, a.id, "shot", "01:00:09:00", "01:00:10:00")  # exactly the end is fine
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:00:25", "01:00:02:00"), "bad_timecode")
        self.assertEqual(c(shots.create, s, a.id, "shot", "nonsense", "01:00:02:00"), "bad_timecode")
        self.assertEqual(c(shots.create, s, a.id, "marker", "01:00:00:00", "01:00:01:00"), "bad_input")
        self.assertEqual(c(shots.create, s, a.id, "transcript", "01:00:00:00", "01:00:01:00"), "bad_input")
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:00:00", "01:00:01:00", origin="guess"), "bad_input")
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:00:00", "01:00:01:00", run_id="j1"), "bad_input")
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:00:00", "01:00:01:00", label="x" * 201), "bad_input")
        self.assertEqual(c(shots.create, s, a.id, "shot", "01:00:00:00", "01:00:01:00", label=7), "bad_input")
        self.assertEqual(c(shots.create, s, "no-such-asset", "shot", "01:00:00:00", "01:00:01:00"), "not_found")
        self.assertEqual(self.count(a), 1)

    def test_an_asset_without_a_usable_timebase_is_refused(self):
        bare = self.store.mint_asset(owner="desk")
        self.assertEqual(self.code(shots.create, self.store, bare.id, "clip", "00:00:00:00", "00:00:01:00"), "bad_input")
        norate = self.store.mint_asset(owner="desk")
        self.store.add_essence(norate.id, role="hi-res", location="x.mxf")
        self.assertEqual(self.code(shots.create, self.store, norate.id, "clip", "00:00:00:00", "00:00:01:00"), "bad_input")
        broken = self.store.mint_asset(owner="desk")
        self.store.add_essence(broken.id, role="hi-res", location="x.mxf", frame_rate="25", timecode_start="01:00:00;00")
        self.assertEqual(self.code(shots.create, self.store, broken.id, "clip", "01:00:00:00", "01:00:01:00"), "bad_input")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM span").fetchone()[0], 0)

    def test_the_proxy_is_not_the_timebase(self):
        a = self.asset()
        self.store.add_essence(a.id, role="proxy", location="p.mp4", frame_rate="50", timecode_start="00:00:00:00")
        s = shots.create(self.store, a.id, "clip", "01:00:01:00", "01:00:02:00")
        self.assertEqual(s["frameOut"] - s["frameIn"], 25)


class DropFrame(Behaviour):
    def test_drop_frame_labels_use_the_semicolon_and_skip_labels_that_do_not_exist(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00")
        # 01:00:59;29 is followed by 01:01:00;02: ;00 and ;01 do not exist in a non-tenth minute.
        s = shots.create(self.store, a.id, "shot", "01:00:59:29", "01:01:00:02")  # ':' accepted, stored as ';'
        self.assertEqual((s["tcIn"], s["tcOut"]), ("01:00:59;29", "01:01:00;02"))
        self.assertEqual(s["frameOut"] - s["frameIn"], 1)
        self.assertEqual(self.code(shots.create, self.store, a.id, "shot", "01:01:00;00", "01:01:05;00"), "bad_timecode")
        self.assertEqual(self.code(shots.create, self.store, a.id, "clip", "01:01:00;01", "01:01:05;00"), "bad_timecode")
        # in a tenth minute the label exists
        t = shots.create(self.store, a.id, "clip", "01:10:00;00", "01:10:00;10")
        self.assertEqual(t["frameOut"] - t["frameIn"], 10)

    def test_a_minute_of_drop_frame_is_1798_frames_and_ten_minutes_17982(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00")
        one = shots.create(self.store, a.id, "clip", "01:00:00;00", "01:01:00;02")
        self.assertEqual(one["frameOut"] - one["frameIn"], 1800)
        ten = shots.create(self.store, a.id, "clip", "01:00:00;00", "01:10:00;00")
        self.assertEqual(ten["frameOut"] - ten["frameIn"], 17982)
        self.assertEqual(ten["tcOut"], "01:10:00;00")

    def test_a_non_drop_start_at_29_97_counts_labels_straight(self):
        a = self.asset(rate="30000/1001", start="01:00:00:00")
        s = shots.create(self.store, a.id, "clip", "01:00:00:00", "01:01:00:00")
        self.assertEqual(s["frameOut"] - s["frameIn"], 1800)
        self.assertEqual(s["tcOut"], "01:01:00:00")

    def test_drop_frame_end_of_source_is_checked_in_frames(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00", duration=1800)
        shots.create(self.store, a.id, "clip", "01:00:00;00", "01:01:00;02")  # frame 1800 exactly
        self.assertEqual(self.code(shots.create, self.store, a.id, "clip", "01:00:00;00", "01:01:00;03"),
                         "outside_source")


class Overlap(Behaviour):
    def test_shots_may_touch_but_not_overlap_and_a_refusal_changes_nothing(self):
        a = self.asset()
        first = shots.create(self.store, a.id, "shot", "01:00:02:00", "01:00:04:00")
        shots.create(self.store, a.id, "shot", "01:00:04:00", "01:00:06:00")   # adjacent after
        shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:02:00")   # adjacent before
        self.assertEqual(self.count(a, "shot"), 3)
        for tcs in (("01:00:03:00", "01:00:05:00"), ("01:00:01:00", "01:00:02:01"), ("01:00:02:10", "01:00:02:20"),
                    ("01:00:01:00", "01:00:07:00")):
            self.assertEqual(self.code(shots.create, self.store, a.id, "shot", *tcs), "overlaps_shot", tcs)
        self.assertEqual(self.count(a, "shot"), 3)
        self.assertEqual(shots.get(self.store, first["id"]), first)

    def test_clips_overlap_anything_and_shots_ignore_clips_and_other_assets(self):
        a, b = self.asset(), self.asset()
        shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:10:00")
        shots.create(self.store, a.id, "clip", "01:00:02:00", "01:00:05:00")
        shots.create(self.store, a.id, "clip", "01:00:03:00", "01:00:06:00")
        shots.create(self.store, b.id, "shot", "01:00:00:00", "01:00:10:00")
        c = shots.create(self.store, a.id, "clip", "01:00:00:00", "01:00:10:00")
        self.assertIsNone(shots.overlaps_shot(self.store, a.id, 0, 1))
        self.assertEqual(c["kind"], "clip")
        self.assertEqual(self.code(shots.create, self.store, a.id, "shot", "01:00:05:00", "01:00:06:00"), "overlaps_shot")

    def test_a_transcript_span_never_blocks_a_shot(self):
        a = self.asset()
        library.index_words(self.store, a.id, [("hello", "01:00:00:00", "01:00:01:00")])
        shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:02:00")


class Trim(Behaviour):
    def test_trim_moves_either_end_and_keeps_labels_canonical(self):
        a = self.asset()
        s = shots.create(self.store, a.id, "shot", "01:00:02:00", "01:00:04:00")
        t = shots.trim(self.store, s["id"], tc_out="01:00:05:00")
        self.assertEqual((t["tcIn"], t["tcOut"], t["frameOut"] - t["frameIn"]), ("01:00:02:00", "01:00:05:00", 75))
        t = shots.trim(self.store, s["id"], tc_in="01:00:01:00")
        self.assertEqual((t["tcIn"], t["frameOut"] - t["frameIn"]), ("01:00:01:00", 100))

    def test_trim_may_grow_into_its_own_old_range_but_not_over_a_neighbour(self):
        a = self.asset()
        s = shots.create(self.store, a.id, "shot", "01:00:02:00", "01:00:04:00")
        n = shots.create(self.store, a.id, "shot", "01:00:06:00", "01:00:08:00")
        shots.trim(self.store, s["id"], tc_out="01:00:06:00")  # touches the neighbour, fine
        before = shots.get(self.store, s["id"])
        self.assertEqual(self.code(shots.trim, self.store, s["id"], tc_out="01:00:06:01"), "overlaps_shot")
        self.assertEqual(self.code(shots.trim, self.store, n["id"], tc_in="01:00:05:00"), "overlaps_shot")
        self.assertEqual(shots.get(self.store, s["id"]), before)

    def test_trim_refusals(self):
        a = self.asset(duration=250)
        s = shots.create(self.store, a.id, "clip", "01:00:02:00", "01:00:04:00")
        c = self.code
        self.assertEqual(c(shots.trim, self.store, s["id"]), "bad_input")
        self.assertEqual(c(shots.trim, self.store, s["id"], tc_in="01:00:05:00"), "bad_input")
        self.assertEqual(c(shots.trim, self.store, s["id"], tc_in="00:59:00:00"), "outside_source")
        self.assertEqual(c(shots.trim, self.store, s["id"], tc_out="01:00:11:00"), "outside_source")
        self.assertEqual(c(shots.trim, self.store, s["id"], tc_out="01:00:03:30"), "bad_timecode")
        self.assertEqual(c(shots.trim, self.store, "nope", tc_out="01:00:05:00"), "not_found")
        self.assertEqual(shots.get(self.store, s["id"])["tcOut"], "01:00:04:00")

    def test_a_span_an_item_or_a_version_uses_cannot_be_trimmed_but_can_be_renamed(self):
        a = self.asset()
        on_item = shots.create(self.store, a.id, "clip", "01:00:02:00", "01:00:04:00")
        on_version = shots.create(self.store, a.id, "shot", "01:00:06:00", "01:00:08:00")
        self.attach_to_item(on_item["id"])
        self.add_version(a, on_version["id"])
        for span in (on_item, on_version):
            before = shots.get(self.store, span["id"])
            self.assertEqual(self.code(shots.trim, self.store, span["id"], tc_out="01:00:09:00"), "span_in_use")
            self.assertEqual(self.code(shots.trim, self.store, span["id"], tc_in="01:00:01:00"), "span_in_use")
            self.assertEqual(shots.get(self.store, span["id"]), before)
            renamed = shots.label(self.store, span["id"], "Keeper")
            self.assertEqual((renamed["label"], renamed["tcIn"], renamed["tcOut"]),
                             ("Keeper", span["tcIn"], span["tcOut"]))
        self.assertEqual(shots.get(self.store, on_item["id"])["usedBy"], {"items": 1, "versions": 0})
        self.assertEqual(shots.get(self.store, on_version["id"])["usedBy"], {"items": 0, "versions": 1})

    def test_a_transcript_span_cannot_be_trimmed(self):
        a = self.asset()
        library.index_words(self.store, a.id, [("hello", "01:00:00:00", "01:00:01:00")])
        word = self.store.conn.execute("SELECT id FROM span WHERE asset_id = ?", (a.id,)).fetchone()["id"]
        self.assertEqual(self.code(shots.trim, self.store, word, tc_out="01:00:02:00"), "not_editable")


class Label(Behaviour):
    def test_label_set_clear_and_limits(self):
        a = self.asset()
        s = shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:01:00")
        self.assertEqual(shots.label(self.store, s["id"], "Bench reaction")["label"], "Bench reaction")
        self.assertIsNone(shots.label(self.store, s["id"], None)["label"])
        shots.label(self.store, s["id"], "x")
        self.assertIsNone(shots.label(self.store, s["id"], "   ")["label"])
        self.assertEqual(shots.label(self.store, s["id"], "y" * 200)["label"], "y" * 200)
        self.assertEqual(self.code(shots.label, self.store, s["id"], "y" * 201), "bad_input")
        self.assertEqual(self.code(shots.label, self.store, s["id"], 5), "bad_input")
        self.assertEqual(self.code(shots.label, self.store, "nope", "x"), "not_found")
        self.assertEqual(shots.get(self.store, s["id"])["label"], "y" * 200)


class Delete(Behaviour):
    def test_delete_a_shot_and_a_clip(self):
        a = self.asset()
        s = shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:01:00")
        c = shots.create(self.store, a.id, "clip", "01:00:00:00", "01:00:01:00")
        shots.delete(self.store, s["id"])
        shots.delete(self.store, c["id"])
        self.assertEqual(self.count(a), 0)
        self.assertEqual(self.code(shots.delete, self.store, s["id"]), "not_found")
        # the freed range can be marked again
        shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:01:00")

    def test_a_span_in_use_is_not_deleted(self):
        a = self.asset()
        on_item = shots.create(self.store, a.id, "clip", "01:00:02:00", "01:00:04:00")
        on_version = shots.create(self.store, a.id, "shot", "01:00:06:00", "01:00:08:00")
        self.attach_to_item(on_item["id"])
        self.add_version(a, on_version["id"])
        self.assertEqual(self.code(shots.delete, self.store, on_item["id"]), "span_in_use")
        self.assertEqual(self.code(shots.delete, self.store, on_version["id"]), "span_in_use")
        self.assertEqual(self.count(a), 2)

    def test_transcript_spans_are_never_deleted_here(self):
        a = self.asset()
        library.index_words(self.store, a.id, [("hello", "01:00:00:00", "01:00:01:00")])
        word = self.store.conn.execute("SELECT id FROM span WHERE asset_id = ?", (a.id,)).fetchone()["id"]
        self.assertEqual(self.code(shots.delete, self.store, word), "not_deletable")
        self.assertEqual(self.count(a, "transcript"), 1)


class Kinds(Behaviour):
    def seed(self):
        a = self.asset()
        library.index_words(self.store, a.id, [("one", "01:00:00:00", "01:00:00:10"), ("two", "01:00:00:10", "01:00:00:20")])
        s = shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:05:00")
        c = shots.create(self.store, a.id, "clip", "01:00:01:00", "01:00:02:00")
        return s["id"], c["id"]

    def test_default_lists_every_kind_as_before(self):
        self.seed()
        rows = operator_state.spans(self.store)
        self.assertEqual(sorted(r["kind"] for r in rows), ["clip", "shot", "transcript", "transcript"])
        self.assertEqual(set(rows[0]), {"id", "assetId", "kind", "tcIn", "tcOut", "text", "camera", "blockedBy", "versions"})
        self.assertEqual(operator_state.spans(self.store), operator_state.spans(self.store, None))

    def test_a_kind_filter_narrows_and_an_empty_one_lists_nothing(self):
        shot_id, clip_id = self.seed()
        self.assertEqual([r["id"] for r in operator_state.spans(self.store, ["shot"])], [shot_id])
        self.assertEqual({r["id"] for r in operator_state.spans(self.store, ("shot", "clip"))}, {shot_id, clip_id})
        self.assertEqual({r["kind"] for r in operator_state.spans(self.store, ["transcript"])}, {"transcript"})
        self.assertEqual(operator_state.spans(self.store, ["marker"]), [])
        self.assertEqual(operator_state.spans(self.store, []), [])

    def test_search_and_the_default_state_do_not_see_shots_as_words(self):
        self.seed()
        self.assertEqual([h.text for h in library.search(self.store, "one")], ["one"])


class Backfill(Behaviour):
    def old_words(self, a, rows):
        for text, tc_in, tc_out in rows:  # as written before migration 9: no frame columns
            self.store.conn.execute(
                "INSERT INTO span (id, asset_id, kind, tc_in, tc_out, text, created_at) VALUES (?, ?, 'transcript', ?, ?, ?, '2026-01-01T00:00:00Z')",
                (f"w-{text}", a.id, tc_in, tc_out, text))
        self.store.conn.commit()

    def run_jobs(self):
        runner = Runner(self.store, default_handlers(Config()), name="r1")
        return runner.drain()

    def test_the_runner_registers_the_job_type(self):
        self.assertIn("span-frames", default_handlers(Config()))

    def test_old_spans_are_filled_once_and_the_job_is_idempotent(self):
        a = self.asset()
        self.old_words(a, [("a", "01:00:00:00", "01:00:00:10"), ("b", "01:00:01:00", "01:00:01:12")])
        self.assertEqual(shots.pending_frames(self.store, a.id), 2)
        job = shots.request_span_frames(self.store, a.id)
        self.assertEqual((job.type, job.idempotency_key), ("span-frames", "span-frames-v1"))
        self.assertEqual(shots.request_span_frames(self.store, a.id).id, job.id)  # one job per asset
        self.assertEqual(self.run_jobs(), {"done": 1})
        rows = {r["text"]: (r["frame_in"], r["frame_out"]) for r in
                self.store.conn.execute("SELECT text, frame_in, frame_out FROM span WHERE asset_id = ?", (a.id,))}
        self.assertEqual(rows, {"a": (90000, 90010), "b": (90025, 90037)})
        self.assertEqual(shots.pending_frames(self.store, a.id), 0)
        self.assertIsNone(shots.request_span_frames(self.store, a.id))  # nothing left to do: no job
        self.assertEqual(shots.backfill_frames(self.store, a.id), {"filled": 0, "skipped": 0})  # and a repeat is a no-op
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM job WHERE type = 'span-frames'").fetchone()[0], 1)

    def test_a_rerun_never_overwrites_a_filled_row(self):
        a = self.asset()
        self.old_words(a, [("a", "01:00:00:00", "01:00:00:10")])
        self.store.conn.execute("UPDATE span SET frame_in = 5, frame_out = 6")
        self.store.conn.commit()
        self.assertEqual(shots.backfill_frames(self.store, a.id), {"filled": 0, "skipped": 0})
        self.assertEqual(self.store.conn.execute("SELECT frame_in, frame_out FROM span").fetchone()[:], (5, 6))

    def test_drop_frame_assets_backfill_on_the_drop_frame_count(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00")
        self.old_words(a, [("a", "01:00:59;29", "01:01:00;02")])
        shots.backfill_frames(self.store, a.id)
        row = self.store.conn.execute("SELECT frame_in, frame_out FROM span").fetchone()
        self.assertEqual(row["frame_out"] - row["frame_in"], 1)

    def test_a_label_that_does_not_exist_is_skipped_and_counted_not_guessed(self):
        a = self.asset(rate="30000/1001", start="01:00:00;00")
        self.old_words(a, [("bad", "01:01:00;00", "01:01:00;05"), ("ok", "01:02:00;02", "01:02:00;05")])
        self.assertEqual(shots.backfill_frames(self.store, a.id), {"filled": 1, "skipped": 1})
        self.assertEqual(shots.pending_frames(self.store, a.id), 1)

    def test_an_asset_with_no_timebase_fails_the_job_permanently(self):
        a = self.store.mint_asset(owner="desk")
        self.store.add_essence(a.id, role="hi-res", location="no/such/file.mxf")
        self.old_words(a, [("a", "01:00:00:00", "01:00:00:10")])
        job = shots.request_span_frames(self.store, a.id)
        self.assertEqual(self.run_jobs(), {"failed": 1})
        row = self.store.conn.execute("SELECT status, error FROM job WHERE id = ?", (job.id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("frame rate", row["error"])

    def test_new_words_are_written_with_frames(self):
        a = self.asset()
        library.index_words(self.store, a.id, [("hello", "01:00:00:00", "01:00:00:10")])
        row = self.store.conn.execute("SELECT origin, frame_in, frame_out FROM span").fetchone()
        self.assertEqual((row["origin"], row["frame_in"], row["frame_out"]), ("transcript", 90000, 90010))


class SpeechRerun(Behaviour):
    """The hazard the spec flagged: a speech re-run deletes the asset's transcript spans, and item.span_id and
    version.span_id reference spans."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        clip = Path(self.tmp.name) / "iso.mp4"
        _clip(clip)
        self.asset_ = self.store.mint_asset(owner="desk")
        self.store.set_cleared(self.asset_.id, True)
        self.store.add_essence(self.asset_.id, role="hi-res", location=str(clip), frame_rate="25",
                               timecode_start="01:00:00:00")

    def words(self, *texts):
        return [Word(t, 0.2 + i * 0.5, 0.6 + i * 0.5) for i, t in enumerate(texts)]

    def engine(self, model, *texts):
        return FakeEngine(self.words(*texts), model=model)

    def test_rerun_with_a_word_span_on_an_item_or_version_neither_fails_nor_orphans_it(self):
        a = self.asset_
        transcribe(self.store, a.id, self.engine("tiny.en", "they", "steal", "home"))
        steal = self.store.conn.execute("SELECT id, tc_in, tc_out FROM span WHERE text = 'steal'").fetchone()
        home = self.store.conn.execute("SELECT id FROM span WHERE text = 'home'").fetchone()
        item_id = self.attach_to_item(steal["id"])
        self.add_version(a, home["id"])
        # a different engine output is a new run, which replaces the transcript
        transcribe(self.store, a.id, self.engine("large-v3", "they", "steal", "the", "ball"))
        item = self.store.conn.execute("SELECT span_id FROM item WHERE id = ?", (item_id,)).fetchone()
        self.assertEqual(item["span_id"], steal["id"])
        for span_id in (steal["id"], home["id"]):
            kept = self.store.conn.execute("SELECT kind, asset_id, tc_in FROM span WHERE id = ?", (span_id,)).fetchone()
            self.assertIsNotNone(kept, "the referenced span was deleted: its item or version is orphaned")
            self.assertEqual(kept["kind"], "clip")  # pinned, and no longer a transcript word
        self.assertEqual(self.store.conn.execute("SELECT tc_in, tc_out FROM span WHERE id = ?", (steal["id"],)).fetchone()[:],
                         (steal["tc_in"], steal["tc_out"]))
        # nothing dangles
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM item WHERE span_id IS NOT NULL AND span_id NOT IN (SELECT id FROM span)").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM version WHERE span_id NOT IN (SELECT id FROM span)").fetchone()[0], 0)
        # the new transcript is complete and the pinned words are not searched twice
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM span WHERE kind = 'transcript'").fetchone()[0], 4)
        self.assertEqual(len(library.search(self.store, "steal")), 1)
        # and the pinned span now behaves as a clip: its range is protected
        self.assertEqual(self.code(shots.delete, self.store, steal["id"]), "span_in_use")

    def test_rerun_without_references_still_replaces_everything(self):
        a = self.asset_
        transcribe(self.store, a.id, self.engine("tiny.en", "one", "two"))
        transcribe(self.store, a.id, self.engine("large-v3", "three"))
        self.assertEqual([r["text"] for r in self.store.conn.execute("SELECT text FROM span WHERE asset_id = ?", (a.id,))],
                         ["three"])

    def test_transcribed_words_carry_frames(self):
        a = self.asset_
        transcribe(self.store, a.id, self.engine("tiny.en", "one"))
        row = self.store.conn.execute("SELECT origin, frame_in, frame_out FROM span").fetchone()
        self.assertEqual((row["origin"], row["frame_in"], row["frame_out"]), ("transcript", 90005, 90015))


class Duration(Behaviour):
    def test_ingest_records_the_frame_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "iso.mp4"
            _clip(src, seconds=2)
            result = ingest_file(self.store, src, Path(tmp) / "q", owner="desk")
            row = self.store.conn.execute("SELECT duration_frames FROM essence WHERE id = ?", (result.essence_id,)).fetchone()
            self.assertEqual(row["duration_frames"], 50)

    def test_wrap_records_the_frame_count_and_an_open_essence_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "iso-3.mp4"
            _clip(src, seconds=2)
            started = record_start(self.store, "cv:iso-3", src, owner="desk")
            get = lambda: self.store.conn.execute(
                "SELECT duration_frames, open FROM essence WHERE id = ?", (started.essence_id,)).fetchone()
            self.assertIsNone(get()["duration_frames"])
            wrap(self.store, started.asset_id)
            row = get()
            self.assertEqual((row["duration_frames"], bool(row["open"])), (50, False))

    def test_an_unreadable_file_leaves_the_length_unknown(self):
        self.assertIsNone(shots.probe_duration_frames("no/such/file.mp4"))
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "x.mp4"
            junk.write_bytes(b"not media")
            self.assertIsNone(shots.probe_duration_frames(junk))

    def test_wrap_essence_without_a_count_keeps_what_was_there(self):
        a = self.asset(duration=100, open_file=True)
        eid = self.store.conn.execute("SELECT id FROM essence").fetchone()["id"]
        self.store.wrap_essence(eid, "abc")
        self.assertEqual(self.store.conn.execute("SELECT duration_frames FROM essence").fetchone()[0], 100)
        self.store.wrap_essence(eid, "abc", duration_frames=120)
        self.assertEqual(self.store.conn.execute("SELECT duration_frames FROM essence").fetchone()[0], 120)


class Races(Behaviour):
    """Two operators marking one asset: the overlap rule holds. Needs a database more than one connection can open."""

    def shared_target(self):
        raise NotImplementedError

    def test_many_writers_marking_the_same_range_leave_exactly_one_shot(self):
        target = self.shared_target()
        a = self.asset()
        self.store.conn.commit()
        results, lock = [], threading.Lock()
        barrier = threading.Barrier(6)

        def work(i):
            store = Store(target)
            try:
                barrier.wait()
                try:
                    shots.create(store, a.id, "shot", "01:00:00:00", f"01:00:0{1 + i % 3}:00")
                    outcome = "ok"
                except ShotError as exc:
                    outcome = exc.code
            except Exception as exc:  # noqa: BLE001
                outcome = repr(exc)
            finally:
                store.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), ["ok"] + ["overlaps_shot"] * 5)
        self.assertEqual(self.count(a, "shot"), 1)

    def test_a_trim_racing_a_create_cannot_produce_overlap(self):
        target = self.shared_target()
        a = self.asset()
        left = shots.create(self.store, a.id, "shot", "01:00:00:00", "01:00:02:00")
        shots.create(self.store, a.id, "shot", "01:00:05:00", "01:00:07:00")
        self.store.conn.commit()
        barrier = threading.Barrier(2)
        results = []

        def trimmer():
            s = Store(target)
            try:
                barrier.wait()
                shots.trim(s, left["id"], tc_out="01:00:04:00")
                results.append("trim")
            except ShotError as exc:
                results.append(exc.code)
            finally:
                s.close()

        def creator():
            s = Store(target)
            try:
                barrier.wait()
                shots.create(s, a.id, "shot", "01:00:03:00", "01:00:04:00")
                results.append("create")
            except ShotError as exc:
                results.append(exc.code)
            finally:
                s.close()

        ts = [threading.Thread(target=trimmer), threading.Thread(target=creator)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        rows = self.store.conn.execute(
            "SELECT frame_in, frame_out FROM span WHERE asset_id = ? AND kind = 'shot' ORDER BY frame_in", (a.id,)).fetchall()
        for (_, out), (nxt, _) in zip(rows, rows[1:]):
            self.assertLessEqual(out, nxt, f"overlap after {results}")
        self.assertEqual(sorted(results).count("overlaps_shot"), 1, results)


ALL = (Create, DropFrame, Overlap, Trim, Label, Delete, Kinds, Backfill, SpeechRerun, Duration)


class _Sqlite:
    def open_store(self):
        return Store()


class _SqliteFile(_Sqlite):
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
    g["SqliteRaces"] = type("SqliteRaces", (_SqliteFile, Races, unittest.TestCase), {})
    g["PostgresRaces"] = type("PostgresRaces", (_Pg, Races, PgCase), {})


_build()
# the abstract mixins must not be collected on their own
del Behaviour, Create, DropFrame, Overlap, Trim, Label, Delete, Kinds, Backfill, SpeechRerun, Duration, Races


if __name__ == "__main__":
    unittest.main()
