"""Speech worker. Turns the audio of a closed hi-res essence into transcript spans on source timecode."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from volt.deliverables import _probe_fps, tc_to_seconds
from volt.library import index_words
from volt.store import Store


class SpeechError(Exception):
    pass


@dataclass
class Word:
    text: str
    start: float  # seconds from the start of the essence
    end: float


class Engine(Protocol):
    def transcribe(self, wav: Path) -> list[Word]: ...


class FakeEngine:
    def __init__(self, words: list[Word] | None = None, error: str | None = None):
        self.words = words or []
        self.error = error
        self.calls = 0

    def transcribe(self, wav: Path) -> list[Word]:
        self.calls += 1
        if self.error:
            raise SpeechError(self.error)
        return list(self.words)


class WhisperCliEngine:
    """Runs the openai-whisper CLI with word timestamps and reads its JSON.

    Any program that writes <name>.json with segments[].words[] {word, start, end} fits."""

    def __init__(self, exe: str | list[str] = "whisper", model: str = "base", language: str | None = None):
        self.exe, self.model, self.language = exe, model, language

    def transcribe(self, wav: Path) -> list[Word]:
        with tempfile.TemporaryDirectory() as out:
            cmd = [*([self.exe] if isinstance(self.exe, str) else self.exe), str(wav), "--model", self.model, "--word_timestamps", "True",
                   "--output_format", "json", "--output_dir", out]
            if self.language:
                cmd += ["--language", self.language]
            try:
                run = subprocess.run(cmd, capture_output=True, text=True)
            except OSError as exc:
                raise SpeechError(f"cannot run {self.exe}: {exc}") from exc
            result = Path(out) / f"{wav.stem}.json"
            if run.returncode != 0 or not result.exists():
                raise SpeechError((run.stderr or "engine wrote no transcript")[-300:].strip())
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
                return [Word(w["word"].strip(), float(w["start"]), float(w["end"]))
                        for seg in data["segments"] for w in seg.get("words", []) if w["word"].strip()]
            except (ValueError, KeyError, TypeError) as exc:
                raise SpeechError("engine output was not readable") from exc


def seconds_to_tc(seconds: float, fps: float) -> str:
    """Inverse of tc_to_seconds. Non-drop."""
    base = max(1, round(fps))
    total = round(seconds * fps)
    f = total % base
    whole = total // base
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}:{f:02d}"


def _extract_audio(src: str, dest: Path) -> None:
    run = subprocess.run(
        ["ffmpeg", "-y", "-i", src, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", str(dest)],
        capture_output=True, text=True)
    if run.returncode != 0 or not dest.exists():
        tail = (run.stderr or "")[-300:].strip()
        if "matches no streams" in tail or "Stream map" in tail:
            raise SpeechError("essence has no audio")
        raise SpeechError(tail or "ffmpeg produced no audio")


def transcribe(store: Store, asset_id: str, engine: Engine) -> int:
    """Index the words of one asset. Returns the word count. Already indexed assets are left alone."""
    store.get_asset(asset_id)
    essence = store.conn.execute(
        "SELECT location, open, frame_rate, timecode_start FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,)).fetchone()
    if essence is None:
        raise SpeechError("no hi-res essence")
    if essence["open"]:
        raise SpeechError("asset is still recording; wrap it first")
    job = store.enqueue(asset_id, "asr", "asr-v1")
    done = store.conn.execute(
        "SELECT COUNT(*) FROM span WHERE asset_id = ? AND kind = 'transcript'", (asset_id,)).fetchone()[0]
    if done:
        store.complete_job(job.id)
        return done
    try:
        fps = float(Fraction(essence["frame_rate"])) if essence["frame_rate"] else _probe_fps(essence["location"])
        origin = tc_to_seconds(essence["timecode_start"] or "00:00:00:00", fps)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            _extract_audio(essence["location"], wav)
            words = engine.transcribe(wav)
        rows = []
        for w in words:
            tc_in = seconds_to_tc(origin + w.start, fps)
            tc_out = seconds_to_tc(origin + max(w.end, w.start + 1 / fps), fps)
            rows.append((w.text, tc_in, tc_out))
    except Exception as exc:
        store.fail_job(job.id, str(exc) or exc.__class__.__name__)
        if isinstance(exc, SpeechError):
            raise
        raise SpeechError(str(exc)) from exc
    return index_words(store, asset_id, rows)


def transcribe_pending(store: Store, engine: Engine) -> dict[str, int | str]:
    """Run every closed asset that has no transcript. One failure does not stop the rest."""
    rows = store.conn.execute(
        """SELECT DISTINCT e.asset_id FROM essence e
           WHERE e.role = 'hi-res' AND e.open = 0
             AND NOT EXISTS (SELECT 1 FROM span s WHERE s.asset_id = e.asset_id AND s.kind = 'transcript')
           ORDER BY e.asset_id""").fetchall()
    out: dict[str, int | str] = {}
    for row in rows:
        try:
            out[row["asset_id"]] = transcribe(store, row["asset_id"], engine)
        except SpeechError as exc:
            out[row["asset_id"]] = f"failed: {exc}"
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m volt.speech <db> [model]")
        return 2
    store = Store(argv[0])
    engine = WhisperCliEngine(model=argv[1] if len(argv) > 2 else "base")
    for asset_id, result in transcribe_pending(store, engine).items():
        print(asset_id, result)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
