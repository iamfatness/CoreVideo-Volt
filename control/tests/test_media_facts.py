"""Embedded start timecode and codec facts, read at ingest and wrap (found by the dress rehearsal: both stayed NULL,
so a file that starts at 01:00:00:00 had its shots, words and NLE clips placed at zero)."""

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt import nle, shots, timecode
from volt.ingest import ingest_file
from volt.record import record_start, wrap
from volt.speech import FakeEngine, Word, transcribe
from volt.store import Store


def _timecoded(path: Path, rate: str, tc: str, audio: bool = True) -> None:
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=160x90:r={rate}:d=2"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440:r=48000:d=2", "-c:a", "aac", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-timecode", tc, str(path)]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _plain(path: Path) -> None:
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class EmbeddedTimecodeAndCodecs(unittest.TestCase):
    def _ingest(self, root, name, rate, tc, audio=True):
        clip = root / name
        _timecoded(clip, rate, tc, audio)
        store = Store()
        r = ingest_file(store, clip, root / "q", owner="desk", source_key=f"card:{name}")
        return store, r, store.conn.execute("SELECT * FROM essence WHERE id = ?", (r.essence_id,)).fetchone()

    def test_2997_drop_frame_start_places_shots_words_and_nle_clips(self):
        with TemporaryDirectory() as tmp:
            store, r, row = self._ingest(Path(tmp), "df.mov", "30000/1001", "01:00:00;00")
            self.assertEqual((row["timecode_start"], row["timecode_source"]), ("01:00:00;00", "tmcd_track"))
            self.assertEqual((row["frame_rate"], row["codec"], row["pix_fmt"], row["audio_codec"],
                              row["audio_sample_rate"], row["audio_layout"]),
                             ("30000/1001", "h264", "yuv420p", "aac", 48000, "2"))
            self.assertTrue(row["video_profile"])
            f = shots.facts(store, r.asset_id)
            self.assertTrue(f.drop)
            self.assertEqual(f.start, 107892)  # an hour of drop-frame labels is 107892 real frames
            store.set_cleared(r.asset_id, True)
            transcribe(store, r.asset_id, FakeEngine([Word("hello", 1.0, 1.5)]))
            span = store.conn.execute("SELECT tc_in, frame_in FROM span WHERE asset_id = ? AND kind = 'transcript'",
                                      (r.asset_id,)).fetchone()
            self.assertEqual((span["tc_in"], span["frame_in"]), ("01:00:01;00", 107892 + 30))
            src, _f = nle._source(store, r.asset_id, "hi-res", [], {})
            self.assertEqual((src.start, src.drop), (107892, True))

    def test_25fps_non_zero_start(self):
        with TemporaryDirectory() as tmp:
            store, r, row = self._ingest(Path(tmp), "pal.mp4", "25", "10:00:00:00")
            self.assertEqual((row["timecode_start"], row["timecode_source"]), ("10:00:00:00", "tmcd_track"))
            f = shots.facts(store, r.asset_id)
            self.assertEqual((f.start, f.drop), (10 * 3600 * 25, False))
            self.assertEqual(nle._source(store, r.asset_id, "hi-res", [], {})[0].start, 900000)

    def test_video_only_file_has_no_audio_facts_but_keeps_its_timecode(self):
        with TemporaryDirectory() as tmp:
            _store, _r, row = self._ingest(Path(tmp), "mute.mp4", "25", "01:02:03:04", audio=False)
            self.assertEqual(row["timecode_start"], "01:02:03:04")
            self.assertEqual((row["audio_codec"], row["audio_sample_rate"], row["audio_layout"]), (None, None, None))

    def test_matroska_format_tag_is_found(self):
        with TemporaryDirectory() as tmp:
            _store, _r, row = self._ingest(Path(tmp), "m.mkv", "25", "10:00:00:00", audio=False)
            self.assertEqual((row["timecode_start"], row["timecode_source"]), ("10:00:00:00", "format_tag"))

    def test_no_embedded_timecode_keeps_the_old_behaviour_and_records_that(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "plain.mp4"
            _plain(clip)
            store = Store()
            r = ingest_file(store, clip, root / "q", owner="desk", source_key="card:plain")
            row = store.conn.execute("SELECT timecode_start, timecode_source, codec FROM essence WHERE id = ?",
                                     (r.essence_id,)).fetchone()
            self.assertIsNone(row["timecode_start"])  # readers take NULL as 00:00:00:00, as before
            self.assertEqual((row["timecode_source"], row["codec"]), ("none", "h264"))
            self.assertEqual(shots.facts(store, r.asset_id).start, 0)

    def test_an_unplaceable_embedded_label_is_ignored_and_recorded_as_invalid(self):
        def label(tc):
            return {"timecode": tc, "timecode_from": "tmcd_track"}
        self.assertEqual(shots.start_timecode(label("01:00:00;00"), "25"), (None, "invalid"))  # no drop-frame at 25
        self.assertEqual(shots.start_timecode(label("01:00:00:99"), "25"), (None, "invalid"))
        self.assertEqual(shots.start_timecode(label("01:00:00;02"), "30000/1001"), ("01:00:00;02", "tmcd_track"))
        self.assertEqual(shots.start_timecode(label("01:01:00;00"), "30000/1001"), (None, "invalid"))  # frames 0-1 skipped
        self.assertEqual(shots.start_timecode({}, "25"), (None, "none"))

    def test_wrap_reads_them_too_and_a_recorded_start_is_never_replaced(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "rec.mp4"
            _timecoded(clip, "25", "05:00:00:00")
            store = Store()
            started = record_start(store, "cv:tc-1", clip, owner="desk")
            wrap(store, started.asset_id)
            row = store.conn.execute("SELECT timecode_start, timecode_source FROM essence WHERE id = ?",
                                     (started.essence_id,)).fetchone()
            self.assertEqual((row["timecode_start"], row["timecode_source"]), ("05:00:00:00", "tmcd_track"))
            other = store.mint_asset(owner="desk")
            e = store.add_essence(other.id, "hi-res", str(clip), timecode_start="09:00:00:00")
            shots.record_media_facts(store, e, clip)
            self.assertEqual(
                store.conn.execute("SELECT timecode_start FROM essence WHERE id = ?", (e,)).fetchone()[0], "09:00:00:00")

    def test_variable_frame_rate_length_is_still_counted_on_the_essence_rate(self):
        # Fragmented MP4 carries no nb_frames (as CoreVideo recordings), so the length is duration x rate. Dropping
        # frames makes the average rate far lower than the 60 fps stream rate; the count must use 60.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "vfr.mp4"
            subprocess.check_call(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=60:d=3", "-vf",
                 "select='lt(mod(n,7),5)',setpts=N/(60*TB)", "-fps_mode", "vfr", "-c:v", "libx264", "-pix_fmt",
                 "yuv420p", "-movflags", "frag_keyframe+empty_moov", str(clip)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            store = Store()
            r = ingest_file(store, clip, root / "q", owner="desk", source_key="card:vfr")
            row = store.conn.execute("SELECT frame_rate, duration_frames FROM essence WHERE id = ?",
                                     (r.essence_id,)).fetchone()
            seconds = float(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(clip)],
                text=True).strip())
            self.assertEqual(row["frame_rate"], "60")
            self.assertEqual(row["duration_frames"], timecode.seconds_to_frames(seconds, 60))


if __name__ == "__main__":
    unittest.main()
