import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from volt.library import search
from volt.speech import (FakeEngine, asr_outcome, FasterWhisperEngine, SpeechError, Word, apply_corrections,
                         parse_corrections, seconds_to_tc, select_vocabulary, show_vocabulary,
                         transcribe, transcribe_pending)
from volt.store import Store


def _clip(path: Path, audio=True) -> None:
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=3"]
    if audio == "silent":
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-c:a", "aac", "-shortest"]
    elif audio:
        cmd += ["-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _asset(store, root, audio=True, open_file=False, tc_start="01:00:00:00", cleared=True):
    clip = root / "iso.mp4"
    _clip(clip, audio)
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, cleared)
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

    def test_no_audio_is_a_no_speech_outcome_not_a_failure(self):
        asset = _asset(self.store, self.root, audio=False)
        self.assertEqual(transcribe(self.store, asset.id, FakeEngine(WORDS)), 0)
        self.assertEqual(_job(self.store, asset)["status"], "done")
        self.assertIsNone(_job(self.store, asset)["error"])
        self.assertEqual(asr_outcome(self.store, asset.id),
                         {"outcome": "no_speech", "reason": "no_audio_stream", "wordCount": 0})

    def test_silent_audio_is_reported_as_silent_not_as_an_empty_success(self):
        # Found with the dress rehearsal: 7 of 8 CoreVideo ISO files carry an all-zero stereo AAC track (-91 dB,
        # the codec's floor); the engine heard nothing and the run looked like a successful empty transcript.
        asset = _asset(self.store, self.root, audio="silent")
        engine = FakeEngine([])
        self.assertEqual(transcribe(self.store, asset.id, engine), 0)
        out = asr_outcome(self.store, asset.id)
        self.assertEqual((out["outcome"], out["reason"], out["wordCount"]), ("no_speech", "silent_audio", 0))
        self.assertLessEqual(out["peakDbfs"], -70)
        self.assertEqual(_job(self.store, asset)["status"], "done")

    def test_sound_with_no_recognised_words_is_a_different_reason(self):
        asset = _asset(self.store, self.root)  # a loud sine tone: audio is present, no speech is found in it
        self.assertEqual(transcribe(self.store, asset.id, FakeEngine([])), 0)
        out = asr_outcome(self.store, asset.id)
        self.assertEqual((out["outcome"], out["reason"]), ("no_speech", "no_words_recognized"))
        self.assertGreater(out["peakDbfs"], -20)

    def test_a_transcript_with_words_reports_words_and_the_endpoint_carries_the_outcome(self):
        from volt import media
        asset = _asset(self.store, self.root)
        self.assertIsNone(media.transcript(self.store, asset.id, {})["asr"])  # no run yet
        transcribe(self.store, asset.id, FakeEngine(WORDS))
        self.assertEqual(media.transcript(self.store, asset.id, {})["asr"]["outcome"], "words")
        (self.root / "s").mkdir()
        silent = _asset(self.store, self.root / "s", audio="silent")
        transcribe(self.store, silent.id, FakeEngine([]))
        body = media.transcript(self.store, silent.id, {})
        self.assertEqual((body["words"], body["asr"]["outcome"], body["asr"]["reason"]), ([], "no_speech", "silent_audio"))

    def test_one_extracted_stream_is_what_the_engine_hears(self):
        # The ISO files have a single stereo AAC stream; extraction maps audio stream 0 and mixes to mono. A tone on
        # the RIGHT channel only must survive that mix (not be reported silent).
        clip = self.root / "right.mp4"
        subprocess.check_call(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=2", "-f", "lavfi", "-i",
             "sine=f=440:d=2", "-filter_complex", "[1:a]pan=stereo|c0=0*c0|c1=c0[a]", "-map", "0:v", "-map", "[a]",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        asset = self.store.mint_asset(owner="desk")
        self.store.set_cleared(asset.id, True)
        self.store.add_essence(asset.id, role="hi-res", location=str(clip), frame_rate="25")
        transcribe(self.store, asset.id, FakeEngine([]))
        self.assertEqual(asr_outcome(self.store, asset.id)["reason"], "no_words_recognized")

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
        self.assertEqual(out[bad.id], 0)  # no audio: a no_speech outcome, not a failure
        self.assertEqual(asr_outcome(self.store, bad.id)["reason"], "no_audio_stream")
        self.assertNotIn(still_open.id, out)


class Provenance(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store()
        self.asset = _asset(self.store, self.root)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _runs(self):
        return self.store.conn.execute("SELECT * FROM asr_run ORDER BY rowid").fetchall()

    def test_run_records_engine_model_vocabulary_and_corrections(self):
        engine = FakeEngine([Word("they", 0.2, 0.4), Word("steal", 0.4, 0.9)], model="tiny.en")
        transcribe(self.store, self.asset.id, engine, ["Okonkwo"], parse_corrections("steal => Steele"))
        run = self._runs()[0]
        self.assertEqual((run["engine"], run["model"], run["word_count"]), ("fake", "tiny.en", 2))
        self.assertEqual(json.loads(run["vocabulary_used"]), ["Okonkwo"])
        self.assertEqual(json.loads(run["corrections_applied"]), [{"heard": "steal", "replacement": "Steele", "count": 1}])
        self.assertEqual(len(search(self.store, "steele")), 1)
        self.assertEqual(engine.last_vocabulary, ["Okonkwo"])

    def test_different_model_or_vocabulary_is_a_new_run_that_replaces_the_transcript(self):
        a, b = FakeEngine(WORDS, model="tiny.en"), FakeEngine([Word("steal", 0.1, 0.5)], model="large-v3")
        transcribe(self.store, self.asset.id, a)
        self.assertEqual(transcribe(self.store, self.asset.id, b), 1)
        self.assertEqual(len(search(self.store, "they")), 0)
        self.assertEqual(len(self._runs()), 2)
        transcribe(self.store, self.asset.id, b, ["Okonkwo"])
        self.assertEqual(b.calls, 2)
        # going back to the first configuration is a real re-run, not a stale "done"
        self.assertEqual(transcribe(self.store, self.asset.id, a), 3)
        self.assertEqual(a.calls, 2)

    def test_uncleared_asset_is_refused_before_any_work(self):
        (self.root / "u").mkdir()
        asset = _asset(self.store, self.root / "u", cleared=False)
        engine = FakeEngine(WORDS)
        with self.assertRaisesRegex(SpeechError, "not cleared"):
            transcribe(self.store, asset.id, engine)
        self.assertEqual((engine.calls, _job(self.store, asset)), (0, None))
        self.assertEqual(transcribe_pending(self.store, engine)[asset.id], "skipped: not cleared")
        self.assertEqual(transcribe(self.store, asset.id, engine, require_cleared=False), 3)

    def test_pending_never_replaces_an_existing_transcript(self):
        transcribe(self.store, self.asset.id, FakeEngine(WORDS))
        self.assertEqual(transcribe_pending(self.store, FakeEngine([], model="other")), {})


class VocabularyAndCorrections(unittest.TestCase):
    def test_vocabulary_keeps_priority_order_dedupes_and_stops_at_budget(self):
        self.assertEqual(select_vocabulary(["Okonkwo", " okonkwo ", "", "Zed"]), ["Okonkwo", "Zed"])
        self.assertEqual(select_vocabulary(["aaaa", "bbbb", "cc"], budget=10), ["aaaa", "bbbb"])
        self.assertEqual(select_vocabulary(["aaaa", "bbbbbbbbbbbb", "c"], budget=10), ["aaaa"])

    def test_show_vocabulary_is_title_then_rundown_slugs_in_order(self):
        store = Store()
        sid = "s1"
        store.conn.execute("INSERT INTO show (id, title, created_at) VALUES (?, ?, 'now')", (sid, "Tuesday Show"))
        for pos, slug in ((2, "okonkwo-interview"), (1, "cold-open")):
            store.conn.execute("INSERT INTO item (id, show_id, position, slug, status, created_at) VALUES (?,?,?,?, 'planned','now')",
                               (f"i{pos}", sid, pos, slug))
        self.assertEqual(show_vocabulary(store, "Tuesday Show"), ["Tuesday Show", "cold-open", "okonkwo-interview"])
        self.assertEqual(show_vocabulary(store, "Unknown"), ["Unknown"])
        store.close()

    def test_corrections_are_whole_word_case_insensitive_and_keep_punctuation(self):
        words = [Word("Okonquo,", 0, 1), Word("the", 1, 2), Word("o", 2, 3), Word("conquo.", 3, 4), Word("cat", 4, 5)]
        out, applied = apply_corrections(words, parse_corrections("# names\nokonquo => Okonkwo\no conquo => Okonkwo\nca => X"))
        self.assertEqual([(w.text, w.start, w.end) for w in out],
                         [("Okonkwo,", 0, 1), ("the", 1, 2), ("Okonkwo.", 2, 4), ("cat", 4, 5)])
        self.assertEqual([(a["heard"], a["count"]) for a in applied], [("okonquo", 1), ("o conquo", 1)])

    def test_bad_corrections_line_is_loud(self):
        with self.assertRaisesRegex(SpeechError, "line 2"):
            parse_corrections("a => b\nnonsense")


class RealEngine(unittest.TestCase):
    """Real faster-whisper tiny.en on a Windows SAPI clip. Opt in with VOLT_ASR_REAL=1 (needs faster-whisper
    importable, the tiny.en model in the HF cache or network, and powershell System.Speech)."""

    @unittest.skipUnless(os.environ.get("VOLT_ASR_REAL") == "1" and sys.platform == "win32", "set VOLT_ASR_REAL=1 on Windows")
    def test_spoken_words_are_found_on_plausible_timecode(self):
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            self.skipTest("faster-whisper not installed")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "say.wav"
            subprocess.check_call(["powershell", "-NoProfile", "-Command",
                                   "Add-Type -AssemblyName System.Speech;$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                                   f"$s.SetOutputToWaveFile('{wav}');$s.Speak('They steal home.');$s.Dispose()"])
            clip = root / "iso.mp4"
            subprocess.check_call(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=4", "-i", str(wav),
                                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            store = Store()
            asset = store.mint_asset(owner="desk")
            store.set_cleared(asset.id, True)
            store.add_essence(asset.id, role="hi-res", location=str(clip), open_file=False,
                              frame_rate="25", timecode_start="01:00:00:00")
            self.assertGreaterEqual(transcribe(store, asset.id, FasterWhisperEngine(device="cpu", model="tiny.en")), 3)
            hit = search(store, "steal")[0]
            self.assertEqual(hit.asset_id, asset.id)
            self.assertTrue(hit.tc_in.startswith("01:00:0"), hit.tc_in)  # within the first 10 s of the essence
            self.assertEqual(store.conn.execute("SELECT model FROM asr_run").fetchone()[0], "tiny.en")
            store.close()


class EngineSetup(unittest.TestCase):
    def test_cuda_without_a_gpu_fails_loud(self):
        from volt import speech
        orig = speech._cuda_available
        speech._cuda_available = lambda: False
        try:
            with self.assertRaisesRegex(SpeechError, "no CUDA"):
                FasterWhisperEngine(device="cuda")
            self.assertEqual(FasterWhisperEngine(device="auto").describe()["model"], "distil-large-v3")
            self.assertEqual(FasterWhisperEngine(device="auto", model="tiny.en").describe()["model"], "tiny.en")
        finally:
            speech._cuda_available = orig
        speech._cuda_available = lambda: True
        try:
            d = FasterWhisperEngine(device="auto").describe()
            self.assertEqual((d["device"], d["model"], d["compute_type"]), ("cuda", "large-v3", "float16"))
        finally:
            speech._cuda_available = orig


if __name__ == "__main__":
    unittest.main()