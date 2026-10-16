import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt import shots
from volt.ingest import ingest_file
from volt.store import Store


def _clip(path: Path) -> None:
    subprocess.check_call(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class IngestContract(unittest.TestCase):
    # Found by the dress rehearsal (docs/dress-rehearsal.md): ingest recorded no frame rate and queued no
    # proxy or transcript, so a freshly ingested asset could not be marked, detected or exported.
    def test_ingest_records_the_frame_rate_and_queues_the_follow_on_jobs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            card = root / "A002.mp4"
            _clip(card)
            store = Store()
            r = ingest_file(store, card, root / "quarantine", owner="desk", source_key="card:A002")
            row = store.conn.execute("SELECT frame_rate FROM essence WHERE id = ?", (r.essence_id,)).fetchone()
            self.assertEqual(row["frame_rate"], "25")
            self.assertIsNone(store.conn.execute(  # _clip has no audio: no layout is invented
                "SELECT audio_layout FROM essence WHERE id = ?", (r.essence_id,)).fetchone()["audio_layout"])
            self.assertEqual(shots.facts(store, r.asset_id).rate, 25)  # marking no longer refuses the asset
            keys = sorted(j["idempotency_key"] for j in store.conn.execute(
                "SELECT idempotency_key FROM job WHERE asset_id = ? AND type IN ('proxy', 'asr')", (r.asset_id,)))
            self.assertEqual(keys, [f"asr:{r.asset_id}", f"proxy-final:{r.asset_id}"])
            ingest_file(store, card, root / "quarantine", owner="desk", source_key="card:A002")  # replay
            self.assertEqual(store.conn.execute(
                "SELECT COUNT(*) FROM job WHERE type IN ('proxy', 'asr')").fetchone()[0], 2)

    def test_unreadable_file_queues_no_follow_on_jobs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "bad.mp4"
            bad.write_bytes(b"not media")
            store = Store()
            ingest_file(store, bad, root / "quarantine", owner="desk", source_key="card:bad2")
            self.assertEqual(store.conn.execute(
                "SELECT COUNT(*) FROM job WHERE type IN ('proxy', 'asr')").fetchone()[0], 0)

    def test_wrap_records_the_frame_rate_of_a_recording(self):
        from volt.record import record_start, wrap
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "rec.mp4"
            _clip(clip)
            store = Store()
            started = record_start(store, "cv:rate-1", clip, owner="desk")
            self.assertIsNone(store.conn.execute(
                "SELECT frame_rate FROM essence WHERE id = ?", (started.essence_id,)).fetchone()["frame_rate"])
            wrap(store, started.asset_id)
            self.assertEqual(store.conn.execute(
                "SELECT frame_rate FROM essence WHERE id = ?", (started.essence_id,)).fetchone()["frame_rate"], "25")

    def test_a_recorded_frame_rate_is_not_overwritten(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "r.mp4"
            _clip(clip)
            store = Store()
            a = store.mint_asset(owner="desk")
            e = store.add_essence(a.id, "hi-res", str(clip), frame_rate="30000/1001")
            self.assertEqual(shots.record_media_facts(store, e, clip), "30000/1001")

    def test_card_mints_once_and_replay_does_not(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            card = root / "A001.mp4"
            _clip(card)
            store = Store()
            first = ingest_file(store, card, root / "quarantine", owner="desk", source_key="card:A001")
            self.assertTrue(first.minted)
            self.assertEqual(first.status, "done")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
            second = ingest_file(store, card, root / "quarantine", owner="desk", source_key="card:A001")
            self.assertFalse(second.minted)
            self.assertEqual(second.asset_id, first.asset_id)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0], 1)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0], 1)

    def test_unreadable_file_stays_in_quarantine(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "broken.mp4"
            bad.write_bytes(b"not a media file")
            store = Store()
            result = ingest_file(store, bad, root / "quarantine", owner="desk", source_key="card:bad")
            self.assertEqual(result.status, "quarantine")
            self.assertEqual(result.essence_id, "")
            job = store.conn.execute("SELECT status, error FROM job WHERE id = ?", (result.job_id,)).fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM essence").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class AudioAndLength(unittest.TestCase):
    def test_ingest_records_the_channel_count_and_counts_the_length_on_the_essence_rate(self):
        from unittest import mock
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "av.mp4"
            subprocess.check_call(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.4:r=30",
                 "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.4", "-c:v", "libx264",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            store = Store()
            with mock.patch.object(shots, "probe_duration_frames", wraps=shots.probe_duration_frames) as spy:
                r = ingest_file(store, clip, root / "q", owner="desk", source_key="card:av")
            self.assertEqual(spy.call_args.args[1], "30")  # the rate is passed, not left to the stream average
            row = store.conn.execute("SELECT audio_layout, frame_rate FROM essence WHERE id = ?",
                                     (r.essence_id,)).fetchone()
            self.assertEqual((row["audio_layout"], row["frame_rate"]), ("2", "30"))
