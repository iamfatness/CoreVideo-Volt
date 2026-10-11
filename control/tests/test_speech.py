import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt.library import search
from volt.speech import (FakeEngine, SpeechError, Word, WhisperCliEngine, seconds_to_tc,
                         transcribe, transcribe_pending)
from volt.store import Store


def _clip(path: Path, audio=True) -> None:
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=3"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _asset(store, root, audio=True, open_file=False, tc_start="01:00:00:00"):
    clip = root / "iso.mp4"
    _clip(clip, audio)
    asset = store.mint_asset(owner="desk")
    store.add_essence(asset.id, role="hi-res", location=str(clip), open_file=open_file,
                      frame_rate="25", timecode_start=tc_start)
    return asset


WORDS = [Word("they", 0.2, 0.4), Word("steal", 0.4, 0.9), Word("home", 1.0, 1.0)]


def _job(store, asset):
    return store.conn.execute("SELECT status, error FROM job WHERE asset_id = ? AND type = 'asr'",
                              (asset.id,)).fetchone()


class Timecode(unittest.TestCase):
    def test_round_trip_at_25(self):
        self.assertEqual(seconds_to_tc(3600 + 1.48, 25), "01:00:01:12")
        self.assertEqual(seconds_to_tc(0, 25), "00:00:00:00")


class Transcribe(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_words_land_on_source_timecode_and_search_finds_them(self):
        asset = _asset(self.store, self.root, tc_start="01:12:08:00")
        self.assertEqual(transcribe(self.store, asset.id, FakeEngine(WORDS)), 3)
        hit = search(self.store, "steal")[0]
        self.assertEqual((hit.asset_id, hit.tc_in, hit.tc_out), (asset.id, "01:12:08:10", "01:12:08:22"))
        self.assertEqual(_job(self.store, asset)["status"], "done")

    def test_zero_length_word_still_has_a_frame(self):
        asset = _asset(self.store, self.root)
        transcribe(self.store, asset.id, FakeEngine(WORDS))
        hit = search(self.store, "home")[0]
        self.assertEqual((hit.tc_in, hit.tc_out), ("01:00:01:00", "01:00:01:01"))

    def test_second_run_does_not_re_run_the_engine_or_duplicate(self):
        asset = _asset(self.store, self.root)
        engine = FakeEngine(WORDS)
        transcribe(self.store, asset.id, engine)
        self.assertEqual(transcribe(self.store, asset.id, engine), 3)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(len(search(self.store, "steal")), 1)

    def test_open_asset_is_refused_and_untouched(self):
        asset = _asset(self.store, self.root, open_file=True)
        with self.assertRaisesRegex(SpeechError, "still recording"):
            transcribe(self.store, asset.id, FakeEngine(WORDS))
        self.assertIsNone(_job(self.store, asset))

    def test_no_audio_fails_the_job_with_the_reason(self):
        asset = _asset(self.store, self.root, audio=False)
        with self.assertRaisesRegex(SpeechError, "no audio"):
            transcribe(self.store, asset.id, FakeEngine(WORDS))
        self.assertEqual(_job(self.store, asset)["status"], "failed")
        self.assertIn("no audio", _job(self.store, asset)["error"])

    def test_engine_failure_fails_job_and_retry_succeeds(self):
        asset = _asset(self.store, self.root)
        with self.assertRaises(SpeechError):
            transcribe(self.store, asset.id, FakeEngine(error="model missing"))
        self.assertEqual(_job(self.store, asset)["error"], "model missing")
        self.assertEqual(search(self.store, "steal"), [])
        self.assertEqual(transcribe(self.store, asset.id, FakeEngine(WORDS)), 3)
        self.assertEqual(_job(self.store, asset)["status"], "done")

    def test_pending_runs_closed_assets_and_survives_a_failure(self):
        good = _asset(self.store, self.root)
        for name in ("b", "c"):
            (self.root / name).mkdir()
        bad = _asset(self.store, self.root / "b", audio=False)
        still_open = _asset(self.store, self.root / "c", open_file=True)
        out = transcribe_pending(self.store, FakeEngine(WORDS))
        self.assertEqual(out[good.id], 3)
        self.assertTrue(str(out[bad.id]).startswith("failed:"))
        self.assertNotIn(still_open.id, out)


class WhisperCli(unittest.TestCase):
    def test_reads_word_timestamps_from_the_engine_json(self):
        with TemporaryDirectory() as tmp:
            stub = Path(tmp) / "stub.py"
            stub.write_text(
                "import json, sys, pathlib\n"
                "a = sys.argv; wav = pathlib.Path(a[1]); out = pathlib.Path(a[a.index('--output_dir') + 1])\n"
                "(out / (wav.stem + '.json')).write_text(json.dumps({'segments': [{'words': [\n"
                "  {'word': ' they', 'start': 0.2, 'end': 0.4}, {'word': ' steal', 'start': 0.4, 'end': 0.9}]}]}))\n")
            wav = Path(tmp) / "a.wav"
            wav.write_bytes(b"")
            words = WhisperCliEngine(exe=[sys.executable, str(stub)]).transcribe(wav)
            self.assertEqual([(w.text, w.start, w.end) for w in words], [("they", 0.2, 0.4), ("steal", 0.4, 0.9)])

    def test_missing_engine_is_a_speech_error(self):
        with self.assertRaises(SpeechError):
            WhisperCliEngine(exe="definitely-not-installed-asr").transcribe(Path("a.wav"))

    def test_failing_engine_reports_its_stderr(self):
        with TemporaryDirectory() as tmp:
            stub = Path(tmp) / "bad.py"
            stub.write_text("import sys; sys.stderr.write('no model'); sys.exit(3)\n")
            with self.assertRaisesRegex(SpeechError, "no model"):
                WhisperCliEngine(exe=[sys.executable, str(stub)]).transcribe(Path("a.wav"))


if __name__ == "__main__":
    unittest.main()
