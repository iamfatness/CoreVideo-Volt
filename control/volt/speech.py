"""Speech worker. Turns the audio of a closed hi-res essence into transcript spans on source timecode.

Design decisions carried over from SwarmScribe (read, not imported): ordered vocabulary with a hard token
budget and a record of the terms actually used; whole-word "heard => should be" corrections applied after
recognition; a device-aware default model with an explicit, recorded choice; provenance stored per run;
a consent gate; no silent fallbacks. Stdlib only: faster-whisper is imported lazily, only when selected."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Protocol, Sequence

from volt import chunking, timecode
from volt.deliverables import _probe_rate
from volt.store import Store, _id, _now


log = logging.getLogger("volt.speech")


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
    stats: dict = field(default_factory=dict)  # per-window counters (e.g. words dropped by a guard); summed per run


class Engine(Protocol):
    def describe(self) -> dict: ...  # engine, model, device, compute_type: the resolved choice, before running
    # `window` = (start, end) of this audio on the essence timeline, for engines that want it (the fakes do).
    # Word times in the result are relative to the start of `wav`.
    def transcribe(self, wav: Path, vocabulary: Sequence[str] = (),
                   window: tuple[float, float] | None = None) -> Transcription: ...


class FakeEngine:
    def __init__(self, words: list[Word] | None = None, error: str | None = None, model: str = "fake"):
        self.words = words or []
        self.error = error
        self.model = model
        self.calls = 0
        self.last_vocabulary: list[str] = []

    def describe(self) -> dict:
        return {"engine": "fake", "model": self.model, "device": None, "compute_type": None}

    def transcribe(self, wav: Path, vocabulary: Sequence[str] = (),
                   window: tuple[float, float] | None = None) -> Transcription:
        self.calls += 1
        self.last_vocabulary = list(vocabulary)
        if self.error:
            raise SpeechError(self.error)
        return Transcription(list(self.words), list(vocabulary))


class TimelineEngine(FakeEngine):
    """A fake that knows the whole essence. `words` are on the essence timeline; each call returns the ones that
    fall inside the window it is given, with times relative to the window, like a real engine would. Words that
    straddle a window edge are returned whole (or clipped by `clip_edges`), as a real engine tends to do.
    `fail_on_call=n` raises on the n-th call (1-based) to stand in for a crash."""

    def __init__(self, words: list[Word], model: str = "fake-timeline", fail_on_call: int | None = None,
                 clip_edges: bool = False):
        super().__init__(words, model=model)
        self.fail_on_call = fail_on_call
        self.clip_edges = clip_edges
        self.windows: list[tuple[float, float]] = []

    def transcribe(self, wav: Path, vocabulary: Sequence[str] = (),
                   window: tuple[float, float] | None = None) -> Transcription:
        self.calls += 1
        self.last_vocabulary = list(vocabulary)
        if self.fail_on_call == self.calls:
            raise SpeechError("simulated crash")
        a, b = window if window else (0.0, float("inf"))
        self.windows.append((a, b))
        out = []
        for w in self.words:
            if w.end > a and w.start < b and not (self.clip_edges and (w.start < a or w.end > b)):
                out.append(Word(w.text, w.start - a, w.end - a))
        return Transcription(out, list(vocabulary))


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

VOCAB_MODES = ("hotwords", "initial_prompt", "none")


_CUDA_LIBS_ERROR: str | None | bool = False  # False = not checked yet


def _cuda_libs_error() -> str | None:
    """None if the CUDA 12 runtime libraries ctranslate2 needs can be loaded, else the reason. On Windows the pip
    wheels (nvidia-cublas-cu12) put them under site-packages/nvidia/*/bin, which is not on the DLL search path
    until it is added here. Checked once per process."""
    global _CUDA_LIBS_ERROR
    if _CUDA_LIBS_ERROR is not False:
        return _CUDA_LIBS_ERROR
    err = None
    if sys.platform == "win32":
        import ctypes
        import os
        for entry in sys.path:
            nv = Path(entry) / "nvidia"
            if nv.is_dir():
                for bin_dir in nv.glob("*/bin"):
                    try:
                        os.add_dll_directory(str(bin_dir))
                    except OSError:
                        pass
        try:
            ctypes.WinDLL("cublas64_12.dll")
        except OSError:
            err = ("a CUDA GPU is present but cublas64_12.dll cannot be loaded "
                   "(pip install nvidia-cublas-cu12 into the same environment as faster-whisper)")
    _CUDA_LIBS_ERROR = err
    return err


def _cuda_gpu() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _cuda_available() -> bool:
    """A GPU that can actually run: present and its runtime libraries load."""
    return _cuda_gpu() and _cuda_libs_error() is None


def _cuda_why_not() -> str:
    return "cuda was requested but no CUDA GPU is available" if not _cuda_gpu() else f"cuda was requested but {_cuda_libs_error()}"


RECOVER_MIN_GAP = 5.0   # seconds without a word before a stretch is checked for missed speech
RECOVER_MIN_SPEECH = 0.5  # ...and it is re-read only if at least this fraction of it is voice per the VAD


def uncovered_speech(words: Sequence[Word], speech: Sequence[tuple[float, float]], total: float,
                     min_gap: float = RECOVER_MIN_GAP, min_speech: float = RECOVER_MIN_SPEECH) -> list[tuple[float, float]]:
    """Stretches of the audio (start, end) that hold no word for at least `min_gap` seconds although the voice
    detector found speech in at least `min_speech` of them. Long-form whisper decoding silently skips whole
    30 s stretches of real speech (measured on a podcast: 25 s of an ad read, differently placed with every
    setting); this finds them so they can be decoded again on their own."""
    ordered = sorted(words, key=lambda w: w.start)
    pairs, last = [], 0.0
    for w in ordered:
        pairs.append((last, w.start))
        last = max(last, w.end)
    pairs.append((last, total))
    out = []
    for a, b in pairs:
        if b - a < min_gap:
            continue
        voiced = sum(max(0.0, min(e, b) - max(s, a)) for s, e in speech)
        if voiced / (b - a) >= min_speech:
            out.append((a, b))
    return out


class FasterWhisperEngine:
    """faster-whisper with word timestamps. CUDA gets large-v3 float16, otherwise CPU distil-large-v3 int8;
    `model` and `compute_type` override. Asking for cuda without a GPU fails; there is no quiet CPU fallback.

    Hallucination guards (all recorded in describe(), so a change is a new run):
      vad_filter        skip non-speech audio before recognition (faster-whisper's bundled Silero VAD).
      vocab_mode        how a vocabulary reaches the model: `hotwords` (decoder bias), `initial_prompt` (a
                        glossary sentence as prior text) or `none` (the vocabulary is ignored).
      min_word_prob     drop words the model itself was less sure of than this (None = keep all).
      silence_guard     seconds; faster-whisper skips a silent stretch this long when it sees a hallucination.
      recover           with the VAD on, a stretch of voiced audio left without words (see uncovered_speech) is
                        decoded again on its own; the words found there are added, counted in the run's stats.
    """

    def __init__(self, device: str = "auto", model: str | None = None, compute_type: str | None = None,
                 language: str | None = None, model_dir: str | None = None, vad_filter: bool = True,
                 vocab_mode: str = "initial_prompt", min_word_prob: float | None = None,
                 silence_guard: float | None = None, cpu_threads: int = 0, beam_size: int = 5,
                 condition_on_previous_text: bool = True, recover: bool = True):
        if device not in ("auto", "cuda", "cpu"):
            raise SpeechError(f"device must be auto, cuda or cpu, not {device!r}")
        if vocab_mode not in VOCAB_MODES:
            raise SpeechError(f"vocab_mode must be one of {', '.join(VOCAB_MODES)}, not {vocab_mode!r}")
        if device == "cuda" and not _cuda_available():
            raise SpeechError(_cuda_why_not())
        dev, default_model, default_compute = _CUDA if device == "cuda" or (device == "auto" and _cuda_available()) else _CPU
        self.device = dev
        self.model_name = model or default_model
        self.compute_type = compute_type or default_compute
        self.language = language
        self.model_dir = model_dir
        self.vad_filter = vad_filter
        self.vocab_mode = vocab_mode
        self.min_word_prob = min_word_prob
        self.silence_guard = silence_guard
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        self.condition_on_previous_text = condition_on_previous_text
        self.recover = recover and vad_filter
        self._model = None

    def describe(self) -> dict:
        return {"engine": "faster-whisper", "model": self.model_name, "device": self.device,
                "compute_type": self.compute_type, "vad_filter": self.vad_filter, "vocab_mode": self.vocab_mode,
                "min_word_prob": self.min_word_prob, "silence_guard": self.silence_guard,
                "beam_size": self.beam_size, "condition_on_previous_text": self.condition_on_previous_text,
                "recover": self.recover}

    def _load(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise SpeechError("faster-whisper is not installed (pip install faster-whisper)") from exc
            try:
                self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type,
                                           download_root=self.model_dir, cpu_threads=self.cpu_threads)
            except Exception as exc:
                raise SpeechError(f"cannot load model {self.model_name}: {exc}") from exc
        return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def release(self) -> None:
        """Drop the model so its RAM or VRAM can be returned. The next transcribe reloads it."""
        self._model = None
        import gc
        gc.collect()

    def _select(self, vocabulary: Sequence[str]) -> list[str]:
        tok = getattr(self._load(), "hf_tokenizer", None)
        if tok is None:
            return select_vocabulary(vocabulary)
        return select_vocabulary(vocabulary, HOTWORDS_TOKEN_BUDGET,
                                 lambda t: len(tok.encode(" " + t, add_special_tokens=False).ids))

    def transcribe(self, wav: Path, vocabulary: Sequence[str] = (),
                   window: tuple[float, float] | None = None) -> Transcription:
        model = self._load()
        used = self._select(vocabulary) if vocabulary and self.vocab_mode != "none" else []
        extra = {}
        if used and self.vocab_mode == "hotwords":
            extra["hotwords"] = ", ".join(used)
        elif used:
            extra["initial_prompt"] = "Glossary: " + ", ".join(used) + "."
        if self.silence_guard:
            extra["hallucination_silence_threshold"] = self.silence_guard
        dropped = 0
        try:
            segments, _ = model.transcribe(str(wav), word_timestamps=True, vad_filter=self.vad_filter,
                                           language=self.language, beam_size=self.beam_size,
                                           condition_on_previous_text=self.condition_on_previous_text, **extra)
            words = []
            for seg in segments:
                for w in (seg.words or []):
                    if not w.word.strip():
                        continue
                    if self.min_word_prob is not None and w.probability < self.min_word_prob:
                        dropped += 1
                        continue
                    words.append(Word(w.word.strip(), float(w.start), float(w.end)))
            stats = {"dropped_low_prob": dropped} if self.min_word_prob is not None else {}
            if self.recover:
                words = self._recover(model, wav, words, stats)
        except Exception as exc:
            raise SpeechError(f"transcription failed: {exc}") from exc
        return Transcription(words, used, stats)

    def _load_audio(self, wav: Path):
        from faster_whisper.audio import decode_audio
        return decode_audio(str(wav), sampling_rate=16000)

    def _speech_spans(self, audio) -> list[tuple[float, float]]:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        ts = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300, speech_pad_ms=100))
        return [(t["start"] / 16000, t["end"] / 16000) for t in ts]

    def _recover(self, model, wav: Path, words: list[Word], stats: dict) -> list[Word]:
        """Decode again, alone, each voiced stretch the main pass left empty. Only words that fall inside the
        stretch are taken, so nothing already found is repeated; a stretch that still yields nothing (music the
        VAD took for voice) stays empty."""
        audio = self._load_audio(wav)
        total = len(audio) / 16000
        gaps = uncovered_speech(words, self._speech_spans(audio), total)
        added, found = [], 0
        for a, b in gaps:
            lo, hi = max(0.0, a - 1.0), min(total, b + 1.0)
            segments, _ = model.transcribe(audio[int(lo * 16000):int(hi * 16000)], word_timestamps=True,
                                           vad_filter=True, language=self.language, beam_size=self.beam_size,
                                           condition_on_previous_text=False)
            got = [Word(w.word.strip(), lo + float(w.start), lo + float(w.end))
                   for seg in segments for w in (seg.words or []) if w.word.strip()]
            got = [w for w in got if a <= (w.start + w.end) / 2 < b]
            if got:
                found += 1
                added.extend(got)
        if gaps:
            stats["recover_gaps"] = len(gaps)
            stats["recovered_gaps"] = found
            stats["recovered_words"] = len(added)
        return sorted([*words, *added], key=lambda w: w.start) if added else words


# ---- one engine per process -------------------------------------------------------------------------
# A model is loaded once and reused by every job in the process. Keyed by the full construction choice.

_ENGINES: dict[tuple, "FasterWhisperEngine"] = {}
_ENGINES_LOCK = threading.Lock()


def shared_engine(**options) -> "FasterWhisperEngine":
    key = tuple(sorted(options.items()))
    with _ENGINES_LOCK:
        if key not in _ENGINES:
            _ENGINES[key] = FasterWhisperEngine(**options)
        return _ENGINES[key]


def release_engines() -> int:
    """Unload every shared model. Returns how many were loaded."""
    with _ENGINES_LOCK:
        n = 0
        for eng in _ENGINES.values():
            if eng.loaded:
                eng.release()
                n += 1
        return n


def seconds_to_tc(seconds, rate, drop: bool = False) -> str:
    """Inverse of deliverables.tc_to_seconds. Exact rate (Fraction, "30000/1001", 25); drop=True gives `;` labels."""
    try:
        return timecode.seconds_to_tc(seconds, rate, drop)
    except timecode.TimecodeError as exc:
        raise SpeechError(str(exc)) from exc


def _run_key(info: dict, vocabulary: Sequence[str], corrections: Sequence[Correction],
             chunk: chunking.ChunkSettings) -> str:
    """Different model, device, vocabulary, corrections or windowing = a different run. Same everything = the same run."""
    blob = json.dumps([info, list(vocabulary), [[c.heard, c.replacement] for c in corrections], chunk.as_dict()],
                      sort_keys=True)
    return "asr-v3:" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def _run_time(store: Store) -> str:
    """SQLite breaks created_at ties by rowid. Postgres has no rowid, so its run time keeps microseconds."""
    if not store.postgres:
        return _now()
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fingerprint(info: dict, vocabulary: Sequence[str], chunk: chunking.ChunkSettings, location: str) -> str:
    """What saved windows are only good for: the same file (path, size, mtime), engine, vocabulary and windowing."""
    try:
        st = os.stat(location)
        ident = [location, st.st_size, int(st.st_mtime)]
    except OSError:
        ident = [location, None, None]
    blob = json.dumps([ident, info, list(vocabulary), chunk.as_dict()], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _saved_windows(store: Store, job_id: str, fingerprint: str) -> list[dict]:
    """Finished windows of this job that still apply and join up from zero. Anything else is deleted: windows
    of another configuration or file, or past a gap."""
    rows = store.conn.execute(
        "SELECT idx, fingerprint, own_start, own_end, words FROM asr_chunk WHERE job_id = ? ORDER BY idx",
        (job_id,)).fetchall()
    keep: list[dict] = []
    end = 0.0
    for r in rows:
        if r["fingerprint"] != fingerprint or r["idx"] != len(keep) or abs(r["own_start"] - end) > 1e-6:
            break
        body = json.loads(r["words"])
        keep.append({"idx": r["idx"], "own_start": r["own_start"], "own_end": r["own_end"],
                     "words": [Word(t, s, e) for t, s, e in body["words"]], "quiet": body.get("quiet", True),
                     "stats": body.get("stats", {}), "vocab_used": body.get("vocab_used", [])})
        end = r["own_end"]
    if len(keep) != len(rows):
        store.conn.execute("DELETE FROM asr_chunk WHERE job_id = ? AND idx >= ?", (job_id, len(keep)))
        store.conn.commit()
    return keep


def _window_run(store: Store, job_id: str, src: str, engine: Engine, vocabulary: Sequence[str],
                chunk: chunking.ChunkSettings, fingerprint: str, progress=None) -> tuple[list[Word], dict, list[str]]:
    """Transcribe an essence window by window. Returns (words on essence seconds, run facts, vocabulary used).

    Each finished window is saved (its owned words and its cut) before the next starts, so a crash resumes at the
    first unfinished window with the same cut points. A file that fits in one window is a single call and saves
    nothing."""
    try:
        total = chunking.probe_duration(src)
    except chunking.AudioError as exc:
        raise SpeechError(str(exc)) from exc
    windows = _saved_windows(store, job_id, fingerprint)
    resumed = len(windows)
    words: list[Word] = [w for win in windows for w in win["words"]]
    quiet = sum(1 for win in windows if win["quiet"])
    stats: dict = {}
    for win in windows:
        for k, v in win["stats"].items():
            stats[k] = stats.get(k, 0) + v
    used: list[str] = windows[0]["vocab_used"] if windows else []
    own_start = windows[-1]["own_end"] if windows else 0.0
    idx = resumed
    if resumed:
        log.info("asr %s: resuming at window %d (%.0f of %.0f s already done)", job_id, idx, own_start, total)
    while own_start < total - 1e-6:
        try:
            own_end, found = chunking.next_cut(src, own_start, total, chunk)
            last = own_end >= total - 1e-6
            a0 = max(0.0, own_start - chunk.overlap)
            a1 = min(total, own_end + chunk.overlap)
            with tempfile.TemporaryDirectory() as tmp:
                wav = Path(tmp) / "audio.wav"
                chunking.extract(src, a0, a1 - a0, wav)
                result = engine.transcribe(wav, vocabulary, window=(a0, a1))
        except chunking.AudioError as exc:
            raise SpeechError(str(exc)) from exc
        kept = [Word(t, s, e) for t, s, e in chunking.owned(result.words, a0, own_start, own_end, last)]
        used = used or result.vocabulary_used
        if not (idx == 0 and last):
            store.conn.execute(
                "INSERT INTO asr_chunk VALUES (?,?,?,?,?,?,?)",
                (job_id, idx, fingerprint, own_start, own_end,
                 json.dumps({"words": [[w.text, w.start, w.end] for w in kept], "quiet": found,
                             "stats": result.stats, "vocab_used": result.vocabulary_used}), _run_time(store)))
            store.conn.commit()
        words.extend(kept)
        quiet += 1 if found else 0
        for k, v in result.stats.items():
            stats[k] = stats.get(k, 0) + v
        idx += 1
        own_start = own_end
        if progress:
            progress(idx, own_end, total)
    facts = {"chunk": chunk.as_dict(), "audio_seconds": round(total, 3), "windows": idx, "resumed_windows": resumed,
             "quiet_cuts": quiet, "hard_cuts": idx - quiet,
             "engine_stats": stats}
    return words, facts, used


def transcribe(store: Store, asset_id: str, engine: Engine, vocabulary: Sequence[str] = (),
               corrections: Sequence[Correction] = (), require_cleared: bool = True,
               job_id: str | None = None, chunk: chunking.ChunkSettings | None = None, progress=None) -> int:
    """Index the words of one asset and record how. Returns the word count.

    Consent: an asset that is not cleared is refused (no job, no audio read) unless require_cleared=False.
    The same engine/model/vocabulary/corrections/windowing on the same asset is a no-op. A different configuration
    is a new run and replaces the asset's transcript spans, so a better model or a fuller vocabulary can be re-run
    on purpose; transcribe_pending never replaces an existing transcript.

    Long files are read window by window (see volt.chunking) and each finished window is saved, so a crash or a
    lost lease resumes where it stopped instead of starting over. `progress(window_count, seconds_done, seconds_total)`
    is called after each window.

    job_id: the job row a runner has already claimed for this work. It is used in place of a run-keyed job and its
    status is left to the runner (no inline done/failed), so no second queued row appears for another runner to claim."""
    try:
        chunk = (chunk or chunking.ChunkSettings()).validate()
    except chunking.AudioError as exc:
        raise SpeechError(str(exc)) from exc
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
    owned = job_id is not None
    job = store.get_job(job_id) if owned else store.enqueue(asset_id, "asr", _run_key(info, vocabulary, corrections, chunk))
    slot = _claim_slot(store, asset_id)
    if slot is None:
        latest = store.conn.execute(
            "SELECT word_count FROM asr_run WHERE asset_id = ? ORDER BY created_at DESC"
            + ("" if store.postgres else ", rowid DESC") + " LIMIT 1",
            (asset_id,)).fetchone()
        if latest is not None:
            return latest["word_count"]
        raise SpeechError("another transcribe holds this asset")
    latest = store.conn.execute(
        "SELECT job_id, word_count FROM asr_run WHERE asset_id = ? ORDER BY created_at DESC"
        + ("" if store.postgres else ", rowid DESC") + " LIMIT 1",
        (asset_id,)).fetchone()
    if job.status == "done" and latest is not None and latest["job_id"] == job.id:
        _release_slot(store, slot)
        return latest["word_count"]
    try:
        rate = timecode.parse_rate(essence["frame_rate"]) if essence["frame_rate"] else _probe_rate(essence["location"])
        drop = timecode.is_drop(essence["timecode_start"])  # `;` before the frames = drop-frame
        origin = timecode.tc_to_frames(essence["timecode_start"] or "00:00:00:00", rate, drop)
        def window_done(n, done, total):
            _renew_slot(store, slot)
            if progress:
                progress(n, done, total)

        found, facts, used = _window_run(store, job.id, essence["location"], engine, vocabulary, chunk,
                                         _fingerprint(info, vocabulary, chunk, essence["location"]), window_done)
        words, applied = apply_corrections(found, corrections)
        one = 1 / rate
        rows = []
        for w in words:
            f_in = origin + timecode.seconds_to_frames(w.start, rate)
            f_out = origin + timecode.seconds_to_frames(max(Fraction(repr(float(w.end))), Fraction(repr(float(w.start))) + one), rate)
            rows.append((w.text, timecode.frames_to_tc(f_in, rate, drop), timecode.frames_to_tc(f_out, rate, drop), f_in, f_out))
    except Exception as exc:
        store.conn.rollback()
        _release_slot(store, slot)
        if not owned:
            store.fail_job(job.id, str(exc) or exc.__class__.__name__)
        if isinstance(exc, SpeechError):
            raise
        raise SpeechError(str(exc)) from exc
    conn = store.conn
    # The replacement is one transaction: the old transcript spans, the new ones, this run's record and its
    # saved windows change together or not at all, so a reader on another connection sees the old transcript
    # or the new one and never a half. The per-asset lock serializes two transcribes of one asset (they
    # queue here, after the slow engine work above, and the later one replaces the earlier one whole).
    with store.transaction(f"transcript:{asset_id}"):
        # A word span an item or a version uses cannot be deleted (Postgres refuses; SQLite would orphan the
        # reference). It is pinned instead: re-kinded to a clip, same in and out and text, so the item keeps
        # the frames it was cut from, search stops seeing it, and the new transcript is written beside it.
        in_use = ("(EXISTS (SELECT 1 FROM item WHERE item.span_id = span.id) "
                  "OR EXISTS (SELECT 1 FROM version WHERE version.span_id = span.id))")
        conn.execute(f"UPDATE span SET kind = 'clip', origin = 'transcript' "
                     f"WHERE asset_id = ? AND kind = 'transcript' AND {in_use}", (asset_id,))
        conn.execute("DELETE FROM span WHERE asset_id = ? AND kind = 'transcript'", (asset_id,))
        stamp = _now()
        for text, tc_in, tc_out, f_in, f_out in rows:
            conn.execute(
                """INSERT INTO span (id, asset_id, kind, tc_in, tc_out, text, created_at, origin, frame_in, frame_out)
                   VALUES (?, ?, 'transcript', ?, ?, ?, ?, 'transcript', ?, ?)""",
                (_id(), asset_id, tc_in, tc_out, text, stamp, f_in, f_out))
        conn.execute("DELETE FROM asr_run WHERE job_id = ?", (job.id,))
        conn.execute(
            "INSERT INTO asr_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.id, asset_id, info["engine"], info.get("model"), info.get("device"), info.get("compute_type"),
             json.dumps(list(vocabulary)), json.dumps(used), json.dumps(applied), len(rows),
             _run_time(store), json.dumps({"engine": info, **facts})))
        conn.execute("DELETE FROM asr_chunk WHERE job_id = ?", (job.id,))
    _release_slot(store, slot)
    if not owned:
        store.complete_job(job.id)
    return len(rows)


SLOT_LEASE = 1800  # seconds; renewed after every finished window, so it only lapses when the holder is gone


def _slot_owner() -> str:
    return f"asr-{socket.gethostname()}-{os.getpid()}"


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _slot_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=SLOT_LEASE)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim_slot(store: Store, asset_id: str):
    """One engine run per asset. A second transcribe does not start the model.

    A holder that was killed leaves its slot behind with a lease that has not run out; a retry would then wait
    out the rest of SLOT_LEASE (up to 30 minutes) before it could resume. When the holder is a process on this
    host that no longer exists, its slot is taken over at once."""
    job = store.enqueue(asset_id, "asr-slot", f"asr-slot:{asset_id}")
    owner = _slot_owner()
    held = store.conn.execute("SELECT claimed_by FROM job WHERE id = ? AND status = 'running'", (job.id,)).fetchone()
    prefix = f"asr-{socket.gethostname()}-"
    if held and held["claimed_by"] and held["claimed_by"].startswith(prefix):
        tail = held["claimed_by"][len(prefix):]
        if tail.isdigit() and not _pid_alive(int(tail)):
            store.conn.execute("UPDATE job SET lease_expires_at = NULL WHERE id = ? AND claimed_by = ?",
                               (job.id, held["claimed_by"]))
            store.conn.commit()
    now = _now()
    cur = store.conn.execute(
        """UPDATE job SET status = 'running', claimed_by = ?, lease_expires_at = ?, updated_at = ?
           WHERE id = ? AND (status != 'running' OR lease_expires_at IS NULL OR lease_expires_at < ?)""",
        (owner, _slot_expiry(), now, job.id, now))
    store.conn.commit()
    if cur.rowcount != 1:
        return None
    return job.id, owner


def _renew_slot(store: Store, slot) -> None:
    """Push the slot's lease out; called after each window so a long file never lets it lapse under the holder."""
    if slot is None:
        return
    job_id, owner = slot
    store.conn.execute("UPDATE job SET lease_expires_at = ? WHERE id = ? AND claimed_by = ?",
                       (_slot_expiry(), job_id, owner))
    store.conn.commit()


def _release_slot(store: Store, slot) -> None:
    if slot is None:
        return
    job_id, owner = slot
    store.conn.execute(
        """UPDATE job SET status = 'done', claimed_by = NULL, lease_expires_at = NULL, updated_at = ?
           WHERE id = ? AND claimed_by = ?""",
        (_now(), job_id, owner))
    store.conn.commit()


def transcribe_pending(store: Store, engine: Engine, vocabulary: Sequence[str] = (),
                       corrections: Sequence[Correction] = (),
                       chunk: chunking.ChunkSettings | None = None) -> dict[str, int | str]:
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
            out[row["asset_id"]] = transcribe(store, row["asset_id"], engine, vocabulary, corrections, chunk=chunk)
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
    ap.add_argument("--window", type=float, default=900.0, help="seconds per window for long files")
    ap.add_argument("--overlap", type=float, default=15.0)
    ap.add_argument("--vad", type=int, choices=(0, 1), default=1, help="skip non-speech audio (hallucination guard)")
    ap.add_argument("--vocab-mode", choices=VOCAB_MODES, default="initial_prompt")
    ap.add_argument("--min-word-prob", type=float)
    args = ap.parse_args(argv)
    vocab = [l for l in Path(args.vocabulary).read_text(encoding="utf-8").splitlines()] if args.vocabulary else []
    corr = parse_corrections(Path(args.corrections).read_text(encoding="utf-8")) if args.corrections else []
    store = Store(args.db)
    try:
        engine = FasterWhisperEngine(device=args.device, model=args.model, vad_filter=bool(args.vad),
                                     vocab_mode=args.vocab_mode, min_word_prob=args.min_word_prob)
        chunk = chunking.ChunkSettings(window=args.window, overlap=args.overlap, search=min(30.0, args.window / 4))
        for asset_id, result in transcribe_pending(store, engine, vocab, corr, chunk).items():
            print(asset_id, result)
    except SpeechError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
