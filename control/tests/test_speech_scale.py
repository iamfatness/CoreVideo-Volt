"""Long files: windows, cut points, overlap dedupe, resume, timecode across windows. All on fake engines, so these
run in CI; the real-engine checks at the bottom are opt-in (VOLT_ASR_REAL=1)."""

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from volt import chunking, speech
from volt.library import search
from volt.runner import Config, Runner, default_handlers
from volt.speech import FakeEngine, FasterWhisperEngine, SpeechError, TimelineEngine, Word, transcribe
from volt.store import Store

SMALL = chunking.ChunkSettings(window=8.0, overlap=1.0, search=2.0)


def _long_clip(path: Path, seconds=20, quiet=((8.4, 9.6),), audio=True) -> None:
    """Video plus a 440 Hz tone with silences cut into it."""
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s=160x90:r=25:d={seconds}"]
    if audio:
        gate = "".join(f",volume=enable='between(t,{a},{b})':volume=0" for a, b in quiet)
        cmd += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}{gate}", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _asset(store, root, tc_start="01:00:00:00", rate="25", seconds=20, quiet=((8.4, 9.6),)):
    clip = root / "long.mp4"
    _long_clip(clip, seconds, quiet)
    asset = store.mint_asset(owner="desk")
    store.set_cleared(asset.id, True)
    store.add_essence(asset.id, role="hi-res", location=str(clip), open_file=False, frame_rate=rate,
                      timecode_start=tc_start)
    return asset


def _words(n=38, step=0.5, length=0.3):
    """One word every half second: w0 at 0.0, w1 at 0.5 ..."""
    return [Word(f"w{i}", i * step, i * step + length) for i in range(n)]


def _transcript(store, asset):
    rows = store.conn.execute(
        "SELECT text FROM span WHERE asset_id = ? AND kind = 'transcript' ORDER BY rowid", (asset.id,)).fetchall()
    return [r["text"] for r in rows]


class Windows(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_every_word_appears_exactly_once_in_order_across_windows(self):
        asset = _asset(self.store, self.root)
        words = _words()
        engine = TimelineEngine(words)  # returns words that straddle a window edge whole, to both windows
        self.assertEqual(transcribe(self.store, asset.id, engine, chunk=SMALL), len(words))
        self.assertEqual(_transcript(self.store, asset), [w.text for w in words])
        self.assertGreater(len(engine.windows), 2)  # 20 s at 8 s windows

    def test_a_quiet_stretch_near_the_nominal_cut_is_where_the_window_ends(self):
        asset = _asset(self.store, self.root, quiet=((8.4, 9.6),))
        engine = TimelineEngine(_words())
        transcribe(self.store, asset.id, engine, chunk=SMALL)
        first_end = engine.windows[0][1] - SMALL.overlap
        self.assertAlmostEqual(first_end, 9.0, delta=0.3)  # middle of the quiet stretch, not the nominal 8.0

    def test_without_a_quiet_spot_the_cut_falls_on_the_nominal_point_and_words_stay_single(self):
        asset = _asset(self.store, self.root, quiet=())
        words = _words()
        engine = TimelineEngine(words)
        transcribe(self.store, asset.id, engine, chunk=SMALL)
        self.assertAlmostEqual(engine.windows[0][1] - SMALL.overlap, 8.0, delta=0.01)
        self.assertEqual(_transcript(self.store, asset), [w.text for w in words])
        settings = json.loads(self.store.conn.execute("SELECT settings FROM asr_run").fetchone()[0])
        self.assertGreaterEqual(settings["hard_cuts"], 1)

    def test_a_word_clipped_by_the_window_edge_is_not_lost_when_the_next_window_has_it_whole(self):
        asset = _asset(self.store, self.root, quiet=())
        words = [Word("before", 6.8, 7.2), Word("straddle", 7.8, 8.4), Word("after", 8.6, 9.0)]
        engine = TimelineEngine(words, clip_edges=True)  # an engine that drops a word cut by the window edge
        transcribe(self.store, asset.id, engine, chunk=SMALL)
        self.assertEqual(_transcript(self.store, asset), ["before", "straddle", "after"])

    def test_audio_is_cut_per_window_not_loaded_whole(self):
        asset = _asset(self.store, self.root)
        sizes = []

        class Spy(TimelineEngine):
            def transcribe(self, wav, vocabulary=(), window=None):
                sizes.append((os.path.getsize(wav), window))
                return super().transcribe(wav, vocabulary, window)

        transcribe(self.store, asset.id, Spy(_words()), chunk=SMALL)
        whole = 20 * 32000
        for size, (a, b) in sizes:
            self.assertAlmostEqual(size, (b - a) * 32000, delta=4000)  # 16 kHz mono 16-bit
            self.assertLess(size, whole * 0.75)

    def test_short_file_is_one_call_with_no_saved_windows(self):
        asset = _asset(self.store, self.root, seconds=6, quiet=())
        engine = TimelineEngine(_words(10))
        transcribe(self.store, asset.id, engine, chunk=SMALL)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM asr_chunk").fetchone()[0], 0)

    def test_windowing_is_part_of_the_run_identity_and_provenance(self):
        asset = _asset(self.store, self.root)
        engine = TimelineEngine(_words())
        transcribe(self.store, asset.id, engine, chunk=SMALL)
        calls = engine.calls
        transcribe(self.store, asset.id, engine, chunk=SMALL)
        self.assertEqual(engine.calls, calls)  # same windowing: no-op
        transcribe(self.store, asset.id, engine, chunk=chunking.ChunkSettings(window=10.0, overlap=1.0, search=2.0))
        self.assertGreater(engine.calls, calls)  # new windowing: a recorded re-run
        runs = self.store.conn.execute("SELECT settings FROM asr_run ORDER BY rowid").fetchall()
        self.assertEqual(len(runs), 2)
        first = json.loads(runs[0]["settings"])
        self.assertEqual((first["chunk"]["window"], first["resumed_windows"], first["audio_seconds"] > 19), (8.0, 0, True))
        self.assertEqual(first["engine"]["engine"], "fake")

    def test_a_bad_window_setting_is_refused(self):
        asset = _asset(self.store, self.root, seconds=3)
        with self.assertRaisesRegex(SpeechError, "window"):
            transcribe(self.store, asset.id, FakeEngine(), chunk=chunking.ChunkSettings(window=4, search=3))

    def test_no_audio_still_fails_with_the_reason(self):
        clip = self.root / "silent.mp4"
        _long_clip(clip, 3, audio=False)
        asset = self.store.mint_asset(owner="desk")
        self.store.set_cleared(asset.id, True)
        self.store.add_essence(asset.id, role="hi-res", location=str(clip), open_file=False, frame_rate="25",
                               timecode_start="01:00:00:00")
        with self.assertRaisesRegex(SpeechError, "no audio"):
            transcribe(self.store, asset.id, FakeEngine(), chunk=SMALL)


class Resume(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store()
        self.asset = _asset(self.store, self.root)
        self.words = _words()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _saved(self):
        return self.store.conn.execute("SELECT idx FROM asr_chunk ORDER BY idx").fetchall()

    def test_a_crash_resumes_at_the_first_unfinished_window_with_the_same_cuts(self):
        reference = TimelineEngine(self.words)
        ref_store = Store()
        (self.root / "ref").mkdir()
        ref_asset = _asset(ref_store, self.root / "ref")
        transcribe(ref_store, ref_asset.id, reference, chunk=SMALL)
        ref_text = _transcript(ref_store, ref_asset)
        ref_store.close()

        crashing = TimelineEngine(self.words, fail_on_call=3)
        with self.assertRaisesRegex(SpeechError, "simulated crash"):
            transcribe(self.store, self.asset.id, crashing, chunk=SMALL)
        self.assertEqual([r["idx"] for r in self._saved()], [0, 1])  # two windows survived
        self.assertEqual(_transcript(self.store, self.asset), [])  # nothing half-written

        again = TimelineEngine(self.words)
        transcribe(self.store, self.asset.id, again, chunk=SMALL)
        self.assertEqual(again.windows[0][0], crashing.windows[1][1] - 2 * SMALL.overlap)  # window 3 starts one overlap before the saved cut
        self.assertEqual(again.calls + 2, len(reference.windows))  # only the unfinished windows were run
        self.assertEqual(_transcript(self.store, self.asset), ref_text)
        self.assertEqual(self._saved(), [])  # progress rows are cleared once the run is complete
        settings = json.loads(self.store.conn.execute("SELECT settings FROM asr_run").fetchone()[0])
        self.assertEqual(settings["resumed_windows"], 2)

    def test_saved_windows_from_another_configuration_are_discarded(self):
        job = self.store.enqueue(self.asset.id, "asr", f"asr:{self.asset.id}")  # a runner-owned job keeps its id
        with self.assertRaises(SpeechError):
            transcribe(self.store, self.asset.id, TimelineEngine(self.words, model="a", fail_on_call=3),
                       chunk=SMALL, job_id=job.id)
        self.assertEqual(len(self._saved()), 2)
        other = TimelineEngine(self.words, model="b")
        transcribe(self.store, self.asset.id, other, chunk=SMALL, job_id=job.id)
        self.assertEqual(other.windows[0][0], 0.0)  # started over: the saved windows were another model's
        self.assertEqual(_transcript(self.store, self.asset), [w.text for w in self.words])

    def test_saved_windows_that_do_not_join_up_are_not_trusted(self):
        with self.assertRaises(SpeechError):
            transcribe(self.store, self.asset.id, TimelineEngine(self.words, fail_on_call=3), chunk=SMALL)
        self.store.conn.execute("DELETE FROM asr_chunk WHERE idx = 0")  # a hole at the front
        self.store.conn.commit()
        again = TimelineEngine(self.words)
        transcribe(self.store, self.asset.id, again, chunk=SMALL)
        self.assertEqual(again.windows[0][0], 0.0)
        self.assertEqual(_transcript(self.store, self.asset), [w.text for w in self.words])

    def test_the_runner_retries_a_crashed_job_from_its_saved_windows(self):
        engine = TimelineEngine(self.words, fail_on_call=3)
        job = self.store.enqueue(self.asset.id, "asr", f"asr:{self.asset.id}")
        config = Config(asr_window=8.0, asr_overlap=1.0)
        runner = Runner(self.store, default_handlers(config, engine), backoff=0.0, max_attempts=3)
        runner.drain()
        row = self.store.conn.execute("SELECT status, attempt FROM job WHERE id = ?", (job.id,)).fetchone()
        self.assertEqual((row["status"], row["attempt"]), ("done", 2))
        self.assertEqual(_transcript(self.store, self.asset), [w.text for w in self.words])
        starts = [w[0] for w in engine.windows]
        self.assertEqual(starts.count(0.0), 1)  # window 1 ran once, not again on the retry


class TimecodeAcrossWindows(unittest.TestCase):
    def test_drop_frame_labels_are_right_across_a_minute_and_a_window_boundary(self):
        with TemporaryDirectory() as tmp:
            store = Store()
            asset = _asset(store, Path(tmp), tc_start="00:58:58;00", rate="30000/1001", seconds=12, quiet=())
            words = [Word("a", 2.0, 2.4), Word("b", 3.1, 3.5), Word("c", 6.3, 6.5), Word("d", 11.0, 11.5)]
            engine = TimelineEngine(words)
            transcribe(store, asset.id, engine, chunk=chunking.ChunkSettings(window=3.0, overlap=0.5, search=1.0))
            self.assertGreaterEqual(len(engine.windows), 3)
            got = {r["text"]: (r["tc_in"], r["tc_out"]) for r in store.conn.execute(
                "SELECT text, tc_in, tc_out FROM span WHERE asset_id = ? AND kind = 'transcript'", (asset.id,))}
            # 2.0 s is 60 frames after 00:58:58;00: crossing the minute skips labels ;00 and ;01
            self.assertEqual(got["a"][0], "00:59:00;02")
            self.assertEqual(got["b"][0], "00:59:01;05")  # 3.1 s -> frame 93 -> window 2, same timeline
            self.assertTrue(all(";" in tc for pair in got.values() for tc in pair))
            store.close()


class Hooks(unittest.TestCase):
    def test_idle_release_fires_once_after_the_queue_goes_quiet(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "v.db")
            store = Store(db)
            asset = store.mint_asset(owner="desk")
            store.enqueue(asset.id, "proxy", "k")
            store.close()
            released, ran = [], []

            def work():  # the runner owns its store on its own thread
                s = Store(db)
                runner.store = s
                runner.run_forever()
                s.close()

            runner = Runner(None, {"proxy": lambda s, j: ran.append(1)}, poll=0.01,
                            release=lambda: released.append(1), idle_release=0.1)
            thread = threading.Thread(target=work)
            thread.start()
            deadline = time.monotonic() + 5
            while not released and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.3)
            runner.stop()
            thread.join(5)
            self.assertEqual((ran, released), ([1], [1]))

    def test_one_model_per_process_and_configuration(self):
        speech._ENGINES.clear()
        a = speech.shared_engine(device="cpu", model="tiny.en")
        self.assertIs(a, speech.shared_engine(device="cpu", model="tiny.en"))
        self.assertIsNot(a, speech.shared_engine(device="cpu", model="tiny.en", vad_filter=False))
        self.assertEqual(speech.release_engines(), 0)  # nothing was loaded
        speech._ENGINES.clear()

    def test_engine_options_are_validated_and_recorded(self):
        with self.assertRaisesRegex(SpeechError, "vocab_mode"):
            FasterWhisperEngine(device="cpu", vocab_mode="loud")
        d = FasterWhisperEngine(device="cpu", model="tiny.en", vad_filter=False, vocab_mode="initial_prompt").describe()
        self.assertEqual((d["vad_filter"], d["vocab_mode"]), (False, "initial_prompt"))

    def test_config_reads_the_new_settings_from_the_environment(self):
        c = Config.from_env({"VOLT_ASR_WINDOW": "600", "VOLT_ASR_VAD": "0", "VOLT_ASR_VOCAB_MODE": "none",
                             "VOLT_ASR_MIN_WORD_PROB": "0.3", "VOLT_ASR_IDLE_RELEASE": "60"})
        self.assertEqual((c.asr_window, c.asr_vad, c.asr_vocab_mode, c.asr_min_word_prob, c.asr_idle_release),
                         (600.0, False, "none", 0.3, 60.0))
        self.assertEqual(Config().chunk_settings().window, 900.0)


class RecoverSkippedSpeech(unittest.TestCase):
    """Long-form whisper decoding skips whole stretches of real speech (seen on a podcast: 25 s of an ad read,
    dropped with the default settings and again, elsewhere, with others). The engine finds voiced stretches with
    no words and decodes them again alone."""

    def test_uncovered_speech_finds_long_voiced_gaps_only(self):
        words = [Word("a", 1.0, 1.3), Word("b", 30.0, 30.4)]
        voiced = [(0.9, 1.4), (5.0, 28.0), (30.0, 30.5), (40.0, 41.0)]
        # 1.3..30 holds 23 s of voice -> found; 30.4..60 is mostly silence -> not; a 2 s gap is never checked
        self.assertEqual(speech.uncovered_speech(words, voiced, 60.0), [(1.3, 30.0)])
        self.assertEqual(speech.uncovered_speech(words, [], 60.0), [])  # music the VAD does not call voice
        self.assertEqual(speech.uncovered_speech([Word("a", 0.0, 1.0), Word("b", 4.0, 5.0)], [(1.0, 4.0)], 5.0), [])

    def _engine(self, main_words, rescued):
        calls = []

        class Seg:
            def __init__(self, words):
                self.words = words

        class W:
            def __init__(self, text, start, end):
                self.word, self.start, self.end, self.probability = text, start, end, 1.0

        class Model:
            def transcribe(self, audio, **kw):
                calls.append((audio if isinstance(audio, str) else len(audio), kw.get("condition_on_previous_text")))
                if isinstance(audio, str):
                    return [Seg([W(*w) for w in main_words])], None
                return [Seg([W(t, s, e) for t, s, e in rescued])], None

        class Engine(FasterWhisperEngine):
            def _load_audio(self, wav):
                return [0.0] * (60 * 16000)

            def _speech_spans(self, audio):
                return [(0.0, 12.0), (20.0, 45.0)]

        eng = Engine(device="cpu", model="x")
        eng._model = Model()
        return eng, calls

    def test_a_skipped_stretch_is_decoded_again_and_added_once(self):
        main = [("one", 0.5, 0.9), ("two", 4.0, 4.4), ("three", 8.0, 8.4), ("four", 11.5, 11.9), ("tail", 50.0, 50.4)]
        # the rescue decodes 10.9..51 (the 11.9..50 gap plus a second each side); times are clip-relative
        eng, calls = self._engine(main, [("edge", 0.2, 0.5), ("skipped", 6.0, 6.4), ("also", 20.0, 20.4)])
        result = eng.transcribe(Path("x.wav"))
        self.assertEqual([w.text for w in result.words], ["one", "two", "three", "four", "skipped", "also", "tail"])
        self.assertAlmostEqual(result.words[4].start, 16.9)  # 10.9 + 6: back on the window's timeline
        self.assertEqual((result.stats["recover_gaps"], result.stats["recovered_words"]), (1, 2))
        # "edge" is at 11.1, outside the stretch it was asked to fill (it is a word the main pass already has)
        self.assertEqual(calls[1][1], False)  # the rescue does not carry the previous text forward

    def test_nothing_missing_means_no_second_pass(self):
        main = [("w%d" % i, float(i), i + 0.4) for i in range(0, 45)]
        eng, calls = self._engine(main, [("never", 1.0, 1.2)])
        result = eng.transcribe(Path("x.wav"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(result.words), 45)
        self.assertNotIn("recovered_words", result.stats)

    def test_recovery_is_part_of_the_recorded_settings(self):
        self.assertTrue(FasterWhisperEngine(device="cpu", model="x").describe()["recover"])
        self.assertFalse(FasterWhisperEngine(device="cpu", model="x", recover=False).describe()["recover"])
        self.assertFalse(FasterWhisperEngine(device="cpu", model="x", vad_filter=False).describe()["recover"])


class ChunkHelpers(unittest.TestCase):
    def test_choose_cut_takes_the_longest_quiet_stretch_and_its_middle(self):
        self.assertEqual(chunking.choose_cut(10, [(8.0, 8.5), (11.0, 13.0)]), (12.0, True))
        self.assertEqual(chunking.choose_cut(10, []), (10, False))

    def test_ownership_is_by_word_midpoint(self):
        ws = [Word("a", 0.0, 0.4), Word("b", 4.8, 5.4), Word("c", 5.1, 5.3)]
        self.assertEqual([m[0] for m in chunking.owned(ws, 0.0, 0.0, 5.0, last=False)], ["a"])
        self.assertEqual([m[0] for m in chunking.owned(ws, 0.0, 5.0, 9.0, last=True)], ["b", "c"])
        shifted = chunking.owned([Word("x", 1.0, 2.0)], 10.0, 10.0, 20.0, last=False)
        self.assertEqual(shifted, [("x", 11.0, 12.0)])


class RealGuard(unittest.TestCase):
    """The default VAD guard keeps a vocabulary from turning silence or a noise bed into words (measured on tiny.en,
    distil-large-v3 and large-v3, see docs/specs/library.md). Opt in with VOLT_ASR_REAL=1."""

    @unittest.skipUnless(os.environ.get("VOLT_ASR_REAL") == "1", "set VOLT_ASR_REAL=1")
    def test_vad_default_emits_nothing_over_silence_and_noise_with_a_vocabulary(self):
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            self.skipTest("faster-whisper not installed")
        with TemporaryDirectory() as tmp:
            for name, src in (("silence", "anullsrc=r=16000:cl=mono"), ("noise", "anoisesrc=c=pink:r=16000:a=0.05")):
                wav = Path(tmp) / f"{name}.wav"
                subprocess.check_call(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-t", "40", "-i", src, str(wav)])
                for mode in ("hotwords", "initial_prompt"):
                    engine = FasterWhisperEngine(device="cpu", model="tiny.en", vocab_mode=mode)
                    self.assertEqual(engine.transcribe(wav, ["Okonkwo", "Tuesday Show"]).words, [], (name, mode))


class RealLong(unittest.TestCase):
    """Real tiny.en across several windows of SAPI speech. Opt in with VOLT_ASR_REAL=1 on Windows."""

    @unittest.skipUnless(os.environ.get("VOLT_ASR_REAL") == "1" and sys.platform == "win32", "set VOLT_ASR_REAL=1 on Windows")
    def test_words_are_not_duplicated_or_lost_across_real_windows(self):
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            self.skipTest("faster-whisper not installed")
        text = " ".join(f"Segment {w} says the quick brown fox jumps over the lazy dog." for w in
                        ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot"))
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "say.wav"
            subprocess.check_call(["powershell", "-NoProfile", "-Command",
                                   "Add-Type -AssemblyName System.Speech;$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                                   f"$s.SetOutputToWaveFile('{wav}');$s.Speak('{text}');$s.Dispose()"])
            clip = root / "iso.mp4"
            subprocess.check_call(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=40", "-i", str(wav),
                                   "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            store = Store()
            asset = store.mint_asset(owner="desk")
            store.set_cleared(asset.id, True)
            store.add_essence(asset.id, role="hi-res", location=str(clip), open_file=False, frame_rate="25",
                              timecode_start="01:00:00:00")
            engine = FasterWhisperEngine(device="cpu", model="tiny.en")
            transcribe(store, asset.id, engine, chunk=chunking.ChunkSettings(window=12, overlap=2, search=3))
            said = " ".join(_transcript(store, asset)).lower()
            self.assertEqual(said.count("segment"), 6, said)  # each sentence once: none lost, none doubled at a cut
            self.assertEqual(said.count("lazy dog"), 6, said)
            self.assertGreaterEqual(json.loads(store.conn.execute("SELECT settings FROM asr_run").fetchone()[0])["windows"], 2)
            store.close()


if __name__ == "__main__":
    unittest.main()
