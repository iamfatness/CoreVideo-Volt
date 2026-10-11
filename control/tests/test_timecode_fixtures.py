"""Real 29.97 drop-frame media. The clip's start is 01:00:59;20, so its labels cross a minute boundary where
`;00` and `;01` do not exist. Every frame is a flat grey whose luma is 16 + 4 * (frame index), so the cut can
be checked by reading pixels back, not by trusting timestamps."""

import json
import shutil
import subprocess
import unittest
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from volt import deliverables
from volt.publish import publish_files
from volt.speech import FakeEngine, Word, transcribe
from volt.store import Store
from volt.timecode import frames_to_seconds

RATE = Fraction(30000, 1001)
START = "01:00:59;20"
FRAMES = 60

needs_ffmpeg = unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")


def _make_clip(path: Path, start=START, rate="30000/1001") -> None:
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s=64x64:r={rate},geq=lum='16+4*N':cb=128:cr=128",
         "-f", "lavfi", "-i", "sine=f=440:d=3", "-frames:v", str(FRAMES), "-r", rate,
         "-c:v", "libx264", "-crf", "10", "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-timecode", start, str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _frame_indices(path: Path) -> list[int]:
    raw = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-vf", "extractplanes=y", "-f", "rawvideo", "-"])
    size = 64 * 64
    return [round((sum(raw[i:i + size]) / size - 16) / 4) for i in range(0, len(raw), size)]


def _store_asset(root: Path, start=START, rate="30000/1001"):
    clip = root / "ntsc.mp4"
    _make_clip(clip, start, rate)
    store = Store()
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    store.add_essence(asset.id, role="hi-res", location=str(clip), frame_rate=rate, timecode_start=start)
    return store, asset, clip


@needs_ffmpeg
class DropFrameClip(unittest.TestCase):
    def test_fixture_is_real_29_97_with_a_drop_frame_start(self):
        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c.mp4"
            _make_clip(clip)
            out = json.loads(subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,r_frame_rate,nb_frames:stream_tags=timecode",
                 "-of", "json", str(clip)], text=True))
            video = next(s for s in out["streams"] if s["codec_type"] == "video")
            self.assertEqual(video["r_frame_rate"], "30000/1001")
            self.assertEqual(video["nb_frames"], str(FRAMES))
            self.assertEqual(video["tags"]["timecode"], START)  # the `;` is what ffmpeg wrote; Volt reads it
            self.assertEqual(_frame_indices(clip), list(range(FRAMES)))

    def test_cut_across_the_minute_boundary_starts_on_the_right_frame(self):
        # 01:01:00;02 is the 11th frame (index 10): 59;20..59;29 is ten frames, then ;00 and ;01 are skipped.
        # A non-drop reading would land on index 12.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, asset, _ = _store_asset(root)
            span = store.add_span(asset.id, "transcript", "01:01:00;02", "01:01:00;12", text="x")
            for kind in ("house", "ott", "social"):
                url = deliverables.render(store, _row(store, span), kind, root / "out")
                self.assertTrue(url.startswith("file:"))
            from urllib.parse import urlparse
            from urllib.request import url2pathname
            house = next((root / "out").rglob("*-house.mp4"))
            self.assertEqual(_frame_indices(house), list(range(10, 20)))
            versions = publish_files(store, span, root / "out2")
            self.assertEqual([v.status for v in versions], ["done"] * 3)
            for v in versions:
                self.assertEqual(len(_probe_frames(Path(url2pathname(urlparse(v.url).path)))), 10)

    def test_cut_from_the_very_first_frame_and_to_the_very_last(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, asset, _ = _store_asset(root)
            row = lambda a, b: _row(store, store.add_span(asset.id, "transcript", a, b, text="x"))
            first = deliverables.render(store, row(START, "01:00:59;25"), "house", root / "o1")
            last = deliverables.render(store, row("01:01:01;21", "01:01:01;22"), "house", root / "o2")
            self.assertEqual(_frame_indices(next((root / "o1").rglob("*.mp4"))), [0, 1, 2, 3, 4])
            self.assertEqual(_frame_indices(next((root / "o2").rglob("*.mp4"))), [59])

    def test_illegal_drop_frame_span_label_fails_the_version(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, asset, _ = _store_asset(root)
            span = store.add_span(asset.id, "transcript", "01:01:00;00", "01:01:00;10", text="x")
            with self.assertRaises(deliverables.DeliverableError) as ctx:
                deliverables.render(store, _row(store, span),
                                    "house", root / "out")
            self.assertIn("does not exist in drop-frame", str(ctx.exception))

    def test_a_span_before_the_clip_start_is_outside(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, asset, _ = _store_asset(root)
            span = store.add_span(asset.id, "transcript", "01:00:59;10", "01:00:59;15", text="x")
            with self.assertRaises(deliverables.DeliverableError):
                deliverables.render(store, _row(store, span),
                                    "house", root / "out")

    def test_speech_indexes_words_on_drop_frame_labels(self):
        words = [Word("alpha", 0.0, 0.1)]
        for index, text in ((10, "bravo"), (40, "charlie"), (59, "delta")):
            start = float(frames_to_seconds(index, RATE))
            words.append(Word(text, start, start + 0.05))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, asset, _ = _store_asset(root)
            self.assertEqual(transcribe(store, asset.id, FakeEngine(words)), 4)
            got = {r["text"]: (r["tc_in"], r["tc_out"]) for r in store.conn.execute(
                "SELECT text, tc_in, tc_out FROM span WHERE asset_id = ? AND kind = 'transcript'", (asset.id,))}
            self.assertEqual(got["alpha"][0], "01:00:59;20")
            self.assertEqual(got["bravo"][0], "01:01:00;02")  # index 10: ;00 and ;01 do not exist
            self.assertEqual(got["charlie"][0], "01:01:01;02")  # index 40
            self.assertEqual(got["delta"][0], "01:01:01;21")  # index 59: 10 to ;02, then 49 more = 1 s 19 f
            for tc_in, tc_out in got.values():
                self.assertIn(";", tc_in + tc_out)
            # and the label a word got is the label the cut understands: round trip through deliverables
            span = store.conn.execute("SELECT * FROM span WHERE text = 'bravo'").fetchone()
            deliverables.render(store, span, "house", root / "out")
            self.assertEqual(_frame_indices(next((root / "out").rglob("*.mp4")))[0], 10)

    def test_non_drop_29_97_start_is_still_counted_non_drop(self):
        # same media, start written with `:` = non-drop numbering at 29.97: 01:00:59:20 + 10 frames = 01:01:00:00
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, asset, _ = _store_asset(root, start="01:00:59:20")
            span = store.add_span(asset.id, "transcript", "01:01:00:00", "01:01:00:05", text="x")
            deliverables.render(store, _row(store, span),
                                "house", root / "out")
            self.assertEqual(_frame_indices(next((root / "out").rglob("*.mp4"))), list(range(10, 15)))


@needs_ffmpeg
class OtherRates(unittest.TestCase):
    """Same pixel-readback check at the other broadcast rates: (rate, clip start, span, first index)."""

    CASES = [
        ("60000/1001", "01:00:59;50", "01:01:00;04", "01:01:00;09", 10),  # 59.94 DF: ;00-;03 skipped
        ("24000/1001", "01:00:00:20", "01:00:01:00", "01:00:01:05", 4),    # 23.976 non-drop
        ("50", "01:00:00:45", "01:00:01:00", "01:00:01:05", 5),            # 50 non-drop
        ("25", "01:00:00:20", "01:00:01:00", "01:00:01:05", 5),            # 25 non-drop
    ]

    def test_cuts_start_on_the_right_frame(self):
        for rate, start, tc_in, tc_out, first in self.CASES:
            with self.subTest(rate=rate), TemporaryDirectory() as tmp:
                root = Path(tmp)
                store, asset, _ = _store_asset(root, start=start, rate=rate)
                span = store.add_span(asset.id, "transcript", tc_in, tc_out, text="x")
                deliverables.render(store, _row(store, span), "house", root / "out")
                self.assertEqual(_frame_indices(next((root / "out").rglob("*.mp4"))), list(range(first, first + 5)))


def _row(store, span_id):
    return store.conn.execute("SELECT * FROM span WHERE id = ?", (span_id,)).fetchone()


def _probe_frames(path: Path) -> list:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "json", str(path)], text=True)
    return list(range(int(json.loads(out)["streams"][0]["nb_read_frames"])))


if __name__ == "__main__":
    unittest.main()
