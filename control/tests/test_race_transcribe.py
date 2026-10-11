"""Race 3: speech.transcribe replacing a transcript while another connection reads or transcribes."""

import json
import subprocess
import unittest
from pathlib import Path

from volt import speech
from volt.speech import FakeEngine, Word, transcribe

from tests.racekit import Bg, PgBase, SqliteBase, _cleared_span


# ---- race 3: speech.transcribe replacing spans --------------------------------------------------

def _clip(path: Path) -> None:
    subprocess.check_call(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=160x90:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
         "-c:a", "aac", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


OLD = [Word("one", 0.2, 0.4), Word("two", 0.5, 0.9)]
NEW = [Word("red", 0.2, 0.4), Word("green", 0.5, 0.9), Word("blue", 1.0, 1.4)]


class TranscribeRaceTests:
    def _asset(self):
        self.a, self.b = self.new_store(), self.new_store()
        clip = self.root / "iso.mp4"
        _clip(clip)
        asset = self.a.mint_asset(owner="desk")
        self.a.set_cleared(asset.id, True)
        self.a.add_essence(asset.id, role="hi-res", location=str(clip), open_file=False, frame_rate="25",
                           timecode_start="01:00:00:00")
        self.asset = asset.id

    def _transcript(self, store):
        rows = store.conn.execute("SELECT text FROM span WHERE asset_id = ? AND kind = 'transcript'",
                                  (self.asset,)).fetchall()
        return tuple(sorted(r["text"] for r in rows))

    def _latest_model(self, store):
        row = store.conn.execute(
            "SELECT settings FROM asr_run WHERE asset_id = ? ORDER BY created_at DESC"
            + ("" if store.postgres else ", rowid DESC") + " LIMIT 1", (self.asset,)).fetchone()
        import json
        return json.loads(row["settings"])["engine"]["model"] if row else None

    def _watch(self, store, then, before=False):
        """Around every statement that touches spans or the run record, call then(sql): after it ran, or just
        before it runs when before=True (the racing point where this connection holds no write lock yet)."""
        real = store.conn.execute

        def execute(sql, params=()):
            hit = sql.lstrip().upper().startswith(("INSERT INTO SPAN", "DELETE FROM SPAN", "INSERT INTO ASR_RUN",
                                                   "DELETE FROM ASR_RUN"))
            if hit and before:
                then(sql)
            cur = real(sql, params)
            if hit and not before:
                then(sql)
            return cur

        store.conn.execute = execute

    def test_a_reader_never_sees_a_half_replaced_transcript(self):
        self._asset()
        transcribe(self.a, self.asset, FakeEngine(OLD, model="old"))
        old = tuple(sorted(w.text for w in OLD))
        new = tuple(sorted(w.text for w in NEW))
        seen = []
        self._watch(self.a, lambda sql: seen.append((self._transcript(self.b), self._latest_model(self.b))))
        transcribe(self.a, self.asset, FakeEngine(NEW, model="new"))
        self.assertTrue(seen)
        for spans, model in seen:
            self.assertIn((spans, model), [(old, "old"), (new, "new")],
                          f"a second connection saw a half-replaced transcript: {spans} / run {model}")
        self.assertEqual((self._transcript(self.b), self._latest_model(self.b)), (new, "new"))

    def test_two_transcribes_of_one_asset_end_with_one_runs_spans_not_a_mix(self):
        self._asset()
        transcribe(self.a, self.asset, FakeEngine(OLD, model="old"))
        second = {"bg": self.in_thread(lambda st: transcribe(st, self.asset, FakeEngine(NEW, model="other")))}
        fired = []

        def other_run(sql):
            if fired or not sql.lstrip().upper().startswith("INSERT INTO SPAN"):
                return
            fired.append(1)                         # first would-be new span of run A; run B arrives now
            second["bg"].wait(1.0)

        self._watch(self.a, other_run, before=True)
        transcribe(self.a, self.asset, FakeEngine([Word("alpha", 0.2, 0.4), Word("beta", 0.5, 0.9)], model="mine"))
        second["bg"].finish()
        self.assertIsNone(second["bg"].error, repr(second["bg"].error))
        spans, model = self._transcript(self.b), self._latest_model(self.b)
        self.assertIn((spans, model), [(("alpha", "beta"), "mine"), (tuple(sorted(w.text for w in NEW)), "other")],
                      f"mixed or contradictory transcript: {spans} / latest run {model}")

    def test_legal_hold_does_not_block_transcription_and_consent_still_does(self):
        self._asset()
        self.a.set_hold(self.asset, True)
        self.assertEqual(transcribe(self.a, self.asset, FakeEngine(OLD)), 2)
        self.assertEqual(self._transcript(self.b), tuple(sorted(w.text for w in OLD)))
        self.a.set_cleared(self.asset, False)
        with self.assertRaises(speech.SpeechError):
            transcribe(self.a, self.asset, FakeEngine(NEW, model="x"))
        self.assertEqual(self._transcript(self.b), tuple(sorted(w.text for w in OLD)))

    def test_a_failed_replacement_leaves_the_old_transcript_whole(self):
        self._asset()
        transcribe(self.a, self.asset, FakeEngine(OLD, model="old"))
        real = self.a.conn.execute
        n = {"spans": 0}

        def execute(sql, params=()):
            if sql.lstrip().upper().startswith("INSERT INTO SPAN"):
                n["spans"] += 1
                if n["spans"] == 2:
                    raise RuntimeError("disk full")
            return real(sql, params)

        self.a.conn.execute = execute
        with self.assertRaises(Exception):
            transcribe(self.a, self.asset, FakeEngine(NEW, model="new"))
        self.a.conn.execute = real
        self.assertEqual((self._transcript(self.b), self._latest_model(self.b)),
                         (tuple(sorted(w.text for w in OLD)), "old"))


class TranscribeRaceSqlite(TranscribeRaceTests, SqliteBase):
    pass


class TranscribeRacePg(TranscribeRaceTests, PgBase):
    pass



if __name__ == "__main__":
    unittest.main()
