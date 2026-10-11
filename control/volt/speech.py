"""Speech worker. Turns the audio of a closed hi-res essence into transcript spans on source timecode.

Design decisions carried over from SwarmScribe (read, not imported): ordered vocabulary with a hard token
budget and a record of the terms actually used; whole-word "heard => should be" corrections applied after
recognition; a device-aware default model with an explicit, recorded choice; provenance stored per run;
a consent gate; no silent fallbacks. Stdlib only: faster-whisper is imported lazily, only when selected."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Protocol, Sequence

from volt.deliverables import _probe_fps, tc_to_seconds
from volt.store import Store, _now


class SpeechError(Exception):
    pass


@dataclass
class Word:
    text: str
    start: float  # seconds from the start of the essence
    end: float


@dataclass
class Transcription:
    words: list[Word]
    vocabulary_used: list[str] = field(default_factory=list)


class Engine(Protocol):
    def describe(self) -> dict: ...  # engine, model, device, compute_type: the resolved choice, before running
    def transcribe(self, wav: Path, vocabulary: Sequence[str] = ()) -> Transcription: ...


class FakeEngine:
    def __init__(self, words: list[Word] | None = None, error: str | None = None, model: str = "fake"):
        self.words = words or []
        self.error = error
        self.model = model
        self.calls = 0
        self.last_vocabulary: list[str] = []

    def describe(self) -> dict:
        return {"engine": "fake", "model": self.model, "device": None, "compute_type": None}

    def transcribe(self, wav: Path, vocabulary: Sequence[str] = ()) -> Transcription:
        self.calls += 1
        self.last_vocabulary = list(vocabulary)
        if self.error:
            raise SpeechError(self.error)
        return Transcription(list(self.words), list(vocabulary))


# ---- vocabulary -------------------------------------------------------------------------------------

HOTWORDS_TOKEN_BUDGET = 220  # faster-whisper truncates hotwords at 223 tokens
HOTWORDS_CHAR_BUDGET = 300  # fallback when the model exposes no tokenizer


def select_vocabulary(terms: Sequence[str], budget: int = HOTWORDS_CHAR_BUDGET, measure=len) -> list[str]:
    """Leading terms, in priority order, whose comma-joined text fits the budget. Stops at the first misfit."""
    used: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = term.strip()
        if not cleaned or cleaned.casefold() in seen:
            continue
        if measure(", ".join([*used, cleaned])) > budget:
            break
        seen.add(cleaned.casefold())
        used.append(cleaned)
    return used


def show_vocabulary(store: Store, show_name: str) -> list[str]:
    """Priority-ordered terms for a show: its title first, then its rundown slugs in running order.
    Volt does not model guests or sponsors yet; pass them in front of this list when it does."""
    rows = store.conn.execute(
        """SELECT s.title, i.slug FROM show s LEFT JOIN item i ON i.show_id = s.id
           WHERE s.title = ? ORDER BY i.position""", (show_name,)).fetchall()
    if not rows:
        return [show_name]
    return [rows[0]["title"], *[r["slug"] for r in rows if r["slug"]]]


# ---- corrections ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Correction:
    heard: str
    replacement: str


def parse_corrections(text: str) -> list[Correction]:
    """One `heard as => should be` per line. Blank lines and # comments are skipped; a bad line is an error."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        heard, sep, replacement = line.partition("=>")
        if not sep or not heard.strip() or not replacement.strip():
            raise SpeechError(f"corrections line {n} is not 'heard => replacement': {line!r}")
        out.append(Correction(heard.strip(), replacement.strip()))
    return out


_EDGES = re.compile(r"^(\W*)(.*?)(\W*)$", re.DOTALL)


def _core(word: str) -> tuple[str, str, str]:
    pre, core, post = _EDGES.match(word).groups()
    return pre, core.casefold(), post


def apply_corrections(words: list[Word], corrections: Sequence[Correction]) -> tuple[list[Word], list[dict]]:
    """Whole-word, case-insensitive; punctuation around a match is kept. Multi-word matches merge into one
    Word spanning the first start to the last end. Longest rule first. Returns the words and, per rule that
    fired, how many times."""
    rules = sorted(((tuple(c.heard.casefold().split()), c) for c in corrections if c.heard.split()),
                   key=lambda r: -len(r[0]))
    counts: dict[Correction, int] = {}
    out: list[Word] = []
    i = 0
    while i < len(words):
        for heard, c in rules:
            span = words[i:i + len(heard)]
            if len(span) == len(heard) and all(_core(w.text)[1] == h for w, h in zip(span, heard)):
                out.append(Word(_core(span[0].text)[0] + c.replacement + _core(span[-1].text)[2],
                                span[0].start, span[-1].end))
                counts[c] = counts.get(c, 0) + 1
                i += len(heard)
                break
        else:
            out.append(words[i])
            i += 1
    return out, [{"heard": c.heard, "replacement": c.replacement, "count": n} for c, n in counts.items()]


# ---- engine: faster-whisper -------------------------------------------------------------------------

_CUDA = ("cuda", "large-v3", "float16")
_CPU = ("cpu", "distil-large-v3", "int8")


def _cuda_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


class FasterWhisperEngine:
    """faster-whisper with word timestamps. CUDA gets large-v3 float16, otherwise CPU distil-large-v3 int8;
    `model` and `compute_type` override. Asking for cuda without a GPU fails; there is no quiet CPU fallback."""

    def __init__(self, device: str = "auto", model: str | None = None, compute_type: str | None = None,
                 language: str | None = None, model_dir: str | None = None):
        if device not in ("auto", "cuda", "cpu"):
            raise SpeechError(f"device must be auto, cuda or cpu, not {device!r}")
        if device == "cuda" and not _cuda_available():
            raise SpeechError("cuda was requested but no CUDA GPU is available")
        dev, default_model, default_compute = _CUDA if device == "cuda" or (device == "auto" and _cuda_available()) else _CPU
        self.device = dev
        self.model_name = model or default_model
        self.compute_type = compute_type or default_compute
        self.language = language
        self.model_dir = model_dir
        self._model = None

    def describe(self) -> dict:
        return {"engine": "faster-whisper", "model": self.model_name, "device": self.device,
                "compute_type": self.compute_type}

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise SpeechError("faster-whisper is not installed (pip install faster-whisper)") from exc
            try:
                self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type,
                                           download_root=self.model_dir)
            except Exception as exc:
                raise SpeechError(f"cannot load model {self.model_name}: {exc}") from exc
        return self._model

    def _select(self, vocabulary: Sequence[str]) -> list[str]:
        tok = getattr(self._load(), "hf_tokenizer", None)
        if tok is None:
            return select_vocabulary(vocabulary)
        return select_vocabulary(vocabulary, HOTWORDS_TOKEN_BUDGET,
                                 lambda t: len(tok.encode(" " + t, add_special_tokens=False).ids))

    def transcribe(self, wav: Path, vocabulary: Sequence[str] = ()) -> Transcription:
        model = self._load()
        used = self._select(vocabulary) if vocabulary else []
        try:
            segments, _ = model.transcribe(str(wav), word_timestamps=True, vad_filter=False,
                                           language=self.language, hotwords=", ".join(used) or None)
            words = [Word(w.word.strip(), float(w.start), float(w.end))
                     for seg in segments for w in (seg.words or []) if w.word.strip()]
        except Exception as exc:
            raise SpeechError(f"transcription failed: {exc}") from exc
        return Transcription(words, used)

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


def _run_key(info: dict, vocabulary: Sequence[str], corrections: Sequence[Correction]) -> str:
    """Different model, device, vocabulary or corrections = a different run. Same everything = the same run."""
    blob = json.dumps([info, list(vocabulary), [[c.heard, c.replacement] for c in corrections]], sort_keys=True)
    return "asr-v2:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def transcribe(store: Store, asset_id: str, engine: Engine, vocabulary: Sequence[str] = (),
               corrections: Sequence[Correction] = (), require_cleared: bool = True) -> int:
    """Index the words of one asset and record how. Returns the word count.

    Consent: an asset that is not cleared is refused (no job, no audio read) unless require_cleared=False.
    The same engine/model/vocabulary/corrections on the same asset is a no-op. A different configuration is a
    new run and replaces the asset's transcript spans, so a better model or a fuller vocabulary can be re-run
    on purpose; transcribe_pending never replaces an existing transcript."""
    asset = store.get_asset(asset_id)
    if require_cleared and not asset.cleared:
        raise SpeechError("asset is not cleared; clear it before transcribing")
    essence = store.conn.execute(
        "SELECT location, open, frame_rate, timecode_start FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (asset_id,)).fetchone()
    if essence is None:
        raise SpeechError("no hi-res essence")
    if essence["open"]:
        raise SpeechError("asset is still recording; wrap it first")
    info = engine.describe()
    job = store.enqueue(asset_id, "asr", _run_key(info, vocabulary, corrections))
    latest = store.conn.execute(
        "SELECT job_id, word_count FROM asr_run WHERE asset_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (asset_id,)).fetchone()
    if job.status == "done" and latest is not None and latest["job_id"] == job.id:
        return latest["word_count"]
    try:
        fps = float(Fraction(essence["frame_rate"])) if essence["frame_rate"] else _probe_fps(essence["location"])
        origin = tc_to_seconds(essence["timecode_start"] or "00:00:00:00", fps)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "audio.wav"
            _extract_audio(essence["location"], wav)
            result = engine.transcribe(wav, vocabulary)
        words, applied = apply_corrections(result.words, corrections)
        rows = [(w.text, seconds_to_tc(origin + w.start, fps),
                 seconds_to_tc(origin + max(w.end, w.start + 1 / fps), fps)) for w in words]
    except Exception as exc:
        store.fail_job(job.id, str(exc) or exc.__class__.__name__)
        if isinstance(exc, SpeechError):
            raise
        raise SpeechError(str(exc)) from exc
    conn = store.conn
    conn.execute("DELETE FROM span WHERE asset_id = ? AND kind = 'transcript'", (asset_id,))
    conn.commit()
    for text, tc_in, tc_out in rows:
        store.add_span(asset_id, "transcript", tc_in, tc_out, text=text)
    conn.execute(
        "INSERT OR REPLACE INTO asr_run VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job.id, asset_id, info["engine"], info.get("model"), info.get("device"), info.get("compute_type"),
         json.dumps(list(vocabulary)), json.dumps(result.vocabulary_used), json.dumps(applied), len(rows), _now()))
    conn.commit()
    store.complete_job(job.id)
    return len(rows)


def transcribe_pending(store: Store, engine: Engine, vocabulary: Sequence[str] = (),
                       corrections: Sequence[Correction] = ()) -> dict[str, int | str]:
    """Run every closed asset that has no transcript. Assets that are not cleared are reported as skipped.
    One failure does not stop the rest."""
    rows = store.conn.execute(
        """SELECT DISTINCT e.asset_id, a.cleared FROM essence e JOIN asset a ON a.id = e.asset_id
           WHERE e.role = 'hi-res' AND e.open = 0
             AND NOT EXISTS (SELECT 1 FROM span s WHERE s.asset_id = e.asset_id AND s.kind = 'transcript')
           ORDER BY e.asset_id""").fetchall()
    out: dict[str, int | str] = {}
    for row in rows:
        if not row["cleared"]:
            out[row["asset_id"]] = "skipped: not cleared"
            continue
        try:
            out[row["asset_id"]] = transcribe(store, row["asset_id"], engine, vocabulary, corrections)
        except SpeechError as exc:
            out[row["asset_id"]] = f"failed: {exc}"
    return out


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m volt.speech")
    ap.add_argument("db")
    ap.add_argument("--model")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--vocabulary", help="file, one term per line, most important first")
    ap.add_argument("--corrections", help="file of 'heard => should be' lines")
    args = ap.parse_args(argv)
    vocab = [l for l in Path(args.vocabulary).read_text(encoding="utf-8").splitlines()] if args.vocabulary else []
    corr = parse_corrections(Path(args.corrections).read_text(encoding="utf-8")) if args.corrections else []
    store = Store(args.db)
    try:
        engine = FasterWhisperEngine(device=args.device, model=args.model)
        for asset_id, result in transcribe_pending(store, engine, vocab, corr).items():
            print(asset_id, result)
    except SpeechError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
