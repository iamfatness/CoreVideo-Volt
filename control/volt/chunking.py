"""Long-file plumbing for the speech worker: where to cut, how to pull one window of audio, who owns a word.

Constant memory: audio is never held as a whole. Each window is cut out of the essence by ffmpeg into a small
16 kHz mono WAV, handed to the engine, and deleted. A window is `own_start..own_end` plus `overlap` seconds of
context on each side; the context lets the engine finish a word that straddles the cut, and the word is then
kept by the window that owns its midpoint, so the overlap never produces a duplicate.

Cut points are chosen lazily, one window at a time, near `window` seconds on: ffmpeg `silencedetect` (an energy
gate, not a speech model) scans +/- `search` seconds around the nominal cut and the longest quiet stretch wins,
cut at its middle. Where nothing is quiet (a music bed under the talk) the cut falls on the nominal point and
ownership still keeps the words single. The cut is recorded with the window, so a resumed run continues from
exactly where the last one stopped."""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


class AudioError(Exception):
    pass


@dataclass(frozen=True)
class ChunkSettings:
    window: float = 900.0      # seconds each window owns, before the cut moves to a quiet spot
    overlap: float = 15.0      # seconds of context audio each side
    search: float = 30.0       # look this far either side of the nominal cut for a quiet spot
    silence_db: float = -35.0  # silencedetect noise floor
    min_silence: float = 0.35  # shortest quiet stretch that can host a cut

    def as_dict(self) -> dict:
        return asdict(self)

    def validate(self) -> "ChunkSettings":
        if self.window <= 0 or self.overlap < 0 or self.search < 0 or self.search >= self.window / 2:
            raise AudioError("chunk window must be > 0, overlap and search >= 0, and search < window / 2")
        return self


def probe_duration(src: str) -> float:
    """Seconds of the first audio stream (container duration if the stream carries none)."""
    out = {}
    for what, entry in (("stream", "stream=duration"), ("format", "format=duration")):
        sel = ["-select_streams", "a:0"] if what == "stream" else []
        run = subprocess.run(["ffprobe", "-v", "error", *sel, "-show_entries", entry, "-of", "csv=p=0", src],
                             capture_output=True, text=True)
        text = run.stdout.strip().splitlines()
        out[what] = text[0] if text else ""
        if what == "stream" and not text:
            raise AudioError("essence has no audio")
    for what in ("stream", "format"):
        try:
            return float(out[what])
        except ValueError:
            continue
    raise AudioError("cannot read the duration of the essence audio")


def extract(src: str, start: float, duration: Optional[float], dest: Path) -> None:
    """16 kHz mono PCM of [start, start+duration) from the first audio stream. duration None = to the end."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest)]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0 or not dest.exists():
        tail = (run.stderr or "")[-300:].strip()
        if "matches no streams" in tail or "Stream map" in tail:
            raise AudioError("essence has no audio")
        raise AudioError(tail or "ffmpeg produced no audio")


_SIL_START = re.compile(r"silence_start: (-?[\d.]+)")
_SIL_END = re.compile(r"silence_end: ([\d.]+)")


def quiet_stretches(src: str, start: float, duration: float, settings: ChunkSettings) -> list[tuple[float, float]]:
    """(begin, end) of each quiet stretch in [start, start+duration), absolute seconds."""
    cmd = ["ffmpeg", "-v", "info", "-nostats", "-ss", f"{max(start, 0):.3f}", "-t", f"{duration:.3f}", "-i", src,
           "-map", "0:a:0", "-vn", "-af", f"silencedetect=n={settings.silence_db}dB:d={settings.min_silence}",
           "-f", "null", "-"]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        raise AudioError((run.stderr or "")[-300:].strip() or "ffmpeg silencedetect failed")
    base = max(start, 0)
    out: list[tuple[float, float]] = []
    open_at: Optional[float] = None
    for line in run.stderr.splitlines():
        m = _SIL_START.search(line)
        if m:
            open_at = max(float(m.group(1)), 0.0)
            continue
        m = _SIL_END.search(line)
        if m and open_at is not None:
            out.append((base + open_at, base + float(m.group(1))))
            open_at = None
    if open_at is not None:
        out.append((base + open_at, base + duration))
    return out


def choose_cut(nominal: float, stretches: Sequence[tuple[float, float]]) -> tuple[float, bool]:
    """Middle of the longest quiet stretch (the one nearest `nominal` on a tie). Returns (cut, found_quiet)."""
    best = None
    for a, b in stretches:
        key = (b - a, -abs((a + b) / 2 - nominal))
        if best is None or key > best[0]:
            best = (key, (a + b) / 2)
    if best is None:
        return nominal, False
    return best[1], True


def next_cut(src: str, own_start: float, total: float, settings: ChunkSettings) -> tuple[float, bool]:
    """Where the window that starts at own_start ends. total if the rest fits in one more window (a tail of up to
    1.25 windows stays whole rather than leaving a sliver). Returns (own_end, found_quiet)."""
    nominal = own_start + settings.window
    if total - own_start <= settings.window * 1.25:
        return total, True
    lo = max(nominal - settings.search, own_start + settings.window / 2)
    hi = min(nominal + settings.search, total)
    return choose_cut(nominal, quiet_stretches(src, lo, hi - lo, settings))


def owned(words, audio_start: float, own_start: float, own_end: float, last: bool):
    """Words of a window, shifted to essence seconds, that this window owns (midpoint in [own_start, own_end);
    the last window owns to the end). `words` carry window-relative start/end."""
    out = []
    for w in words:
        s, e = w.start + audio_start, w.end + audio_start
        mid = (s + e) / 2
        if mid >= own_start and (last or mid < own_end):
            out.append((w.text, s, e))
    return out


ProgressFn = Callable[[int, float, float], None]  # window index, seconds done, seconds total
