"""Deliverables. One span cut from the closed hi-res essence into a house, OTT or social file."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

from volt import timecode
from volt.store import Store


class DeliverableError(Exception):
    pass


def tc_to_seconds(tc: str, rate, drop: bool | None = None) -> float:
    """Timecode to seconds on an exact rate; `;` before the frames (or drop=True) means drop-frame."""
    try:
        return timecode.tc_to_seconds(tc, rate, drop)
    except timecode.TimecodeError as exc:
        raise DeliverableError(str(exc)) from exc


PROFILES = {
    "house": (["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"], None, "256k"),
    "ott": (["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"],
            "scale=-2:'min(720,ih)'", "128k"),
    "social": (["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p"],
               "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920", "128k"),
}


def _probe_rate(path: str) -> Fraction:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
         "-of", "json", path], capture_output=True, text=True)
    try:
        rate = json.loads(out.stdout)["streams"][0]["r_frame_rate"]
        return timecode.parse_rate(rate)
    except (ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        raise DeliverableError("cannot read frame rate") from exc


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True)
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (ValueError, KeyError):
        return 0.0


def render(store: Store, span, kind: str, out_dir: Path) -> str:
    """Write one deliverable and return its file URL. Raises DeliverableError."""
    if kind not in PROFILES:
        raise DeliverableError(f"unknown kind {kind}")
    src = store.conn.execute(
        "SELECT location, open, frame_rate, timecode_start FROM essence WHERE asset_id = ? AND role = 'hi-res'",
        (span["asset_id"],),
    ).fetchone()
    if src is None:
        raise DeliverableError("no hi-res essence")
    if src["open"]:
        raise DeliverableError("asset is still recording; wrap it first")
    try:
        rate = timecode.parse_rate(src["frame_rate"]) if src["frame_rate"] else _probe_rate(src["location"])
        # Drop-frame is read from the stored start (`;` before the frames); a clip with no start falls back
        # to the span's own label.
        drop = timecode.is_drop(src["timecode_start"] or span["tc_in"])
        origin = timecode.tc_to_frames(src["timecode_start"] or "00:00:00:00", rate, drop)
        first = timecode.tc_to_frames(span["tc_in"], rate, drop) - origin
        count = timecode.tc_to_frames(span["tc_out"], rate, drop) - origin - first
    except timecode.TimecodeError as exc:
        raise DeliverableError(str(exc)) from exc
    if first < 0 or count <= 0:
        raise DeliverableError("span is outside the source or empty")
    # Seek a quarter frame before the first frame (a half would sit on a rounding tie and duplicate it at
    # whole-number rates) so rounding to a few decimals cannot drop it, and stop on a
    # frame count (-frames:v) rather than a time, so the cut holds exactly `count` frames.
    seek = max(Fraction(0), first - Fraction(1, 4))
    start = float(timecode.frames_to_seconds(1, rate) * seek)
    length = float(timecode.frames_to_seconds(1, rate) * (first + count - seek))
    dest_dir = Path(out_dir) / span["asset_id"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{span['id']}-{kind}.mp4"
    part = dest.with_suffix(".part.mp4")
    video, vf, audio_rate = PROFILES[kind]
    cmd = ["ffmpeg", "-y", "-ss", f"{start:.4f}", "-i", src["location"], "-t", f"{length:.4f}",
           "-frames:v", str(count), "-map", "0:v:0", "-map", "0:a:0?"]
    if vf:
        cmd += ["-vf", vf]
    cmd += video + ["-c:a", "aac", "-b:a", audio_rate, "-movflags", "+faststart", str(part)]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0 or not part.exists() or _duration(part) <= 0:
        part.unlink(missing_ok=True)
        raise DeliverableError((run.stderr or "ffmpeg produced no output")[-300:].strip())
    part.replace(dest)
    return dest.resolve().as_uri()
