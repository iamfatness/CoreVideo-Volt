"""Shot and clip spans: the rules that decide what may be marked, moved or removed.

A shot is one cut-to-cut run of a single essence; a clip is an operator selection. Both are rows in `span`
(docs/specs/shot-spans.md), written with the same label style as their essence plus `frame_in` and `frame_out`,
whole frames from midnight on the essence's exact rate, so ordering and overlap never compare timecode strings.

Every rule lives here and not in a page or a route:

1. `tc_out > tc_in`; both labels exist on the essence's timebase (a drop-frame label that is skipped is refused);
   `tc_in` is not before the essence start; when the length is known `tc_out` is not past its end.
2. Shots of one asset never overlap (adjacent is fine: the range is half open). A clip may overlap anything.
3. A span an item or a version uses cannot be deleted or have its in/out changed. The label may change.

A check and its write are one step: an update or delete carries its refusal conditions in its own WHERE, and a
shot's insert or move runs under a per-asset lock (`Store.transaction`), so two operators marking one asset
cannot both pass the overlap check.

Rights: spans carry none and marking is allowed on any asset (cleared or not, held or not): an operator has to
be able to log footage to decide what to clear. Nothing here consults `Store.assert_publishable`; export and
publish keep that gate. Detection (`request_detect`, `detect`) is marking, so it follows the same rule.

Also here: split and merge, the `shots` detection job's logic, and the one thumbnail path and frame rule.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Optional

from volt import timecode
from volt.store import Store

log = logging.getLogger("volt.shots")

SHOT, CLIP, TRANSCRIPT = "shot", "clip", "transcript"
MARK_KINDS = (SHOT, CLIP)               # the kinds this module creates, trims, relabels (label: any) and deletes
ORIGINS = ("detect", "manual", "transcript")
LABEL_LIMIT = 200
SPAN_FRAMES_KEY = "span-frames-v1"
SPAN_FRAMES_TYPE = "span-frames"


class ShotError(Exception):
    """A refusal. `code` is stable (the API will carry it); `reason` is for people. Nothing changed."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ---- the essence's timebase ----------------------------------------------------------------------

@dataclass(frozen=True)
class Facts:
    essence_id: str
    rate: Fraction
    drop: bool
    start: int                    # frames from midnight of the essence's first frame
    duration: Optional[int]       # frames, None = unknown (an open essence, or one never probed)
    open: bool

    def frames(self, label) -> int:
        try:
            return timecode.tc_to_frames(str(label), self.rate, self.drop)
        except timecode.TimecodeError as exc:
            raise ShotError("bad_timecode", str(exc)) from exc

    def label(self, frames: int) -> str:
        return timecode.frames_to_tc(frames, self.rate, self.drop)

    def check_range(self, frame_in: int, frame_out: int) -> None:
        if frame_out <= frame_in:
            raise ShotError("bad_input", "tc_out must be after tc_in")
        if frame_in < self.start:
            raise ShotError("outside_source", f"tc_in is before the start of the source ({self.label(self.start)})")
        if self.duration is not None and frame_out > self.start + self.duration:
            raise ShotError("outside_source",
                            f"tc_out is past the end of the source ({self.label(self.start + self.duration)})")


def facts(store: Store, asset_id: str, probe_rate: bool = False) -> Facts:
    """The hi-res essence's timebase. Refuses an asset with no hi-res or no frame rate: with nothing to count
    frames on, a label cannot be checked and a mark would be a guess. `probe_rate` reads the rate from the file
    when the row has none (the backfill only; marking never guesses)."""
    try:
        store.get_asset(asset_id)
    except KeyError:
        raise ShotError("not_found", f"no such asset: {asset_id}") from None
    row = store.conn.execute(
        "SELECT id, location, frame_rate, timecode_start, duration_frames, open "
        "FROM essence WHERE asset_id = ? AND role = 'hi-res'", (asset_id,)).fetchone()
    if row is None:
        raise ShotError("bad_input", "the asset has no hi-res essence to mark")
    try:
        if row["frame_rate"]:
            rate = timecode.parse_rate(row["frame_rate"])
        elif probe_rate:
            from volt.deliverables import DeliverableError, _probe_rate
            try:
                rate = _probe_rate(row["location"])
            except DeliverableError as exc:
                raise ShotError("bad_input", f"the essence has no frame rate and none can be read: {exc}") from exc
        else:
            raise ShotError("bad_input", "the essence has no frame rate recorded; spans cannot be placed on it")
        start_label = row["timecode_start"] or "00:00:00:00"
        drop = timecode.is_drop(start_label)
        start = timecode.tc_to_frames(start_label, rate, drop)
    except timecode.TimecodeError as exc:
        raise ShotError("bad_input", f"the essence timebase is not usable: {exc}") from exc
    duration = row["duration_frames"]
    return Facts(row["id"], rate, drop, start, None if duration is None else int(duration), bool(row["open"]))


def try_facts(store: Store, asset_id: str) -> Optional[Facts]:
    """facts() for callers that fill the frame columns when they can and leave them NULL when they cannot."""
    try:
        return facts(store, asset_id)
    except ShotError:
        return None


def probe_duration_frames(path, rate=None) -> Optional[int]:
    """Whole frames in a media file, or None when it cannot be read. Never raises: a missing length only
    means the range check is lower-bound until the file is probed again.

    Uses the container's frame count when it has one; otherwise duration times `rate` (the essence's own
    rate, else the stream's average rate)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=nb_frames,avg_frame_rate,duration:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
        stream = (data.get("streams") or [{}])[0]
        nb = stream.get("nb_frames")
        if isinstance(nb, str) and nb.isdigit() and int(nb) > 0:
            return int(nb)
        seconds = stream.get("duration")
        if seconds in (None, "N/A"):
            seconds = (data.get("format") or {}).get("duration")
        fps = timecode.parse_rate(rate) if rate else timecode.parse_rate(stream.get("avg_frame_rate"))
        frames = timecode.seconds_to_frames(float(seconds), fps)
        return frames if frames > 0 else None
    except Exception:  # noqa: BLE001 - ffprobe missing, bad json, N/A: all mean "unknown"
        return None


# ---- reading --------------------------------------------------------------------------------------

_COLUMNS = "id, asset_id, kind, tc_in, tc_out, text, label, origin, run_id, frame_in, frame_out"


def _row(store: Store, span_id: str):
    row = store.conn.execute(f"SELECT {_COLUMNS} FROM span WHERE id = ?", (span_id,)).fetchone()
    if row is None:
        raise ShotError("not_found", f"no such span: {span_id}")
    return row


def in_use(store: Store, span_id: str) -> dict:
    """How many rundown items and versions reference the span. Either being non-zero pins its in and out."""
    items = store.conn.execute("SELECT COUNT(*) FROM item WHERE span_id = ?", (span_id,)).fetchone()[0]
    versions = store.conn.execute("SELECT COUNT(*) FROM version WHERE span_id = ?", (span_id,)).fetchone()[0]
    return {"items": items, "versions": versions}


def span_json(store: Store, row) -> dict:
    return {"id": row["id"], "assetId": row["asset_id"], "kind": row["kind"], "origin": row["origin"],
            "label": row["label"], "tcIn": row["tc_in"], "tcOut": row["tc_out"],
            "frameIn": row["frame_in"], "frameOut": row["frame_out"], "text": row["text"],
            "runId": row["run_id"], "usedBy": in_use(store, row["id"])}


def get(store: Store, span_id: str) -> dict:
    return span_json(store, _row(store, span_id))


def overlaps_shot(store: Store, asset_id: str, frame_in: int, frame_out: int,
                  exclude: Optional[str] = None) -> Optional[str]:
    """The id of a shot of this asset that overlaps [frame_in, frame_out), or None. Half open: a shot that ends
    where another begins does not overlap it."""
    row = store.conn.execute(
        "SELECT id FROM span WHERE asset_id = ? AND kind = 'shot' AND frame_in < ? AND frame_out > ? "
        "AND id <> ? ORDER BY frame_in, id LIMIT 1",
        (asset_id, frame_out, frame_in, exclude or "")).fetchone()
    return row["id"] if row else None


# ---- writing --------------------------------------------------------------------------------------

def _clean_label(label) -> Optional[str]:
    if label is None:
        return None
    if not isinstance(label, str):
        raise ShotError("bad_input", "label must be a string or null")
    label = label.strip()
    if len(label) > LABEL_LIMIT:
        raise ShotError("bad_input", f"label is longer than {LABEL_LIMIT} characters")
    return label or None


def create(store: Store, asset_id: str, kind: str, tc_in: str, tc_out: str, *, label: Optional[str] = None,
           origin: str = "manual", run_id: Optional[str] = None, text: Optional[str] = None) -> dict:
    """Mark a shot or a clip. Labels are stored in the essence's own style (drop-frame `;` or not), whatever
    separator the caller used. Refused (nothing written): bad kind or origin, an unusable timebase, a label that
    does not exist, a range outside the source, a shot that overlaps another shot."""
    if kind not in MARK_KINDS:
        raise ShotError("bad_input", f"kind must be one of {', '.join(MARK_KINDS)}")
    if origin not in ORIGINS:
        raise ShotError("bad_input", f"origin must be one of {', '.join(ORIGINS)}")
    if run_id is not None and origin != "detect":
        raise ShotError("bad_input", "run_id belongs only to origin 'detect'")
    label = _clean_label(label)
    f = facts(store, asset_id)
    frame_in, frame_out = f.frames(tc_in), f.frames(tc_out)
    f.check_range(frame_in, frame_out)
    span_id = None
    # A shot's overlap check and its insert are one step under the asset's lock. A clip overlaps anything.
    with store.transaction(f"shots:{asset_id}" if kind == SHOT else None):
        if kind == SHOT:
            other = overlaps_shot(store, asset_id, frame_in, frame_out)
            if other:
                raise ShotError("overlaps_shot", f"overlaps shot {other}")
        span_id = store.add_span(asset_id, kind, f.label(frame_in), f.label(frame_out), text=text, label=label,
                                 origin=origin, run_id=run_id, frame_in=frame_in, frame_out=frame_out)
    return get(store, span_id)


def trim(store: Store, span_id: str, tc_in: Optional[str] = None, tc_out: Optional[str] = None) -> dict:
    """Move a shot's or clip's in and/or out. Refused when an item or version uses the span, when the new range is
    outside the source, or (shots) when it would overlap another shot. Atomic: the refusals are in the UPDATE."""
    if tc_in is None and tc_out is None:
        raise ShotError("bad_input", "give tc_in, tc_out or both")
    row = _row(store, span_id)
    if row["kind"] not in MARK_KINDS:
        raise ShotError("not_editable", f"a {row['kind']} span cannot be trimmed")
    f = facts(store, row["asset_id"])
    old_in = row["frame_in"] if row["frame_in"] is not None else f.frames(row["tc_in"])
    old_out = row["frame_out"] if row["frame_out"] is not None else f.frames(row["tc_out"])
    new_in = old_in if tc_in is None else f.frames(tc_in)
    new_out = old_out if tc_out is None else f.frames(tc_out)
    f.check_range(new_in, new_out)
    with store.transaction(f"shots:{row['asset_id']}"):
        cur = store.conn.execute(
            """UPDATE span SET tc_in = ?, tc_out = ?, frame_in = ?, frame_out = ?
               WHERE id = ?
                 AND NOT EXISTS (SELECT 1 FROM item WHERE item.span_id = span.id)
                 AND NOT EXISTS (SELECT 1 FROM version WHERE version.span_id = span.id)
                 AND (span.kind <> 'shot' OR NOT EXISTS (
                        SELECT 1 FROM span o WHERE o.asset_id = span.asset_id AND o.kind = 'shot'
                          AND o.id <> span.id AND o.frame_in < ? AND o.frame_out > ?))""",
            (f.label(new_in), f.label(new_out), new_in, new_out, span_id, new_out, new_in))
        if cur.rowcount != 1:
            _explain_refusal(store, span_id, new_in, new_out)
    return get(store, span_id)


def _explain_refusal(store: Store, span_id: str, frame_in: int, frame_out: int) -> None:
    """A guarded write changed nothing: say why. Always raises."""
    row = store.conn.execute("SELECT asset_id, kind FROM span WHERE id = ?", (span_id,)).fetchone()
    if row is None:
        raise ShotError("not_found", f"no such span: {span_id}")
    used = in_use(store, span_id)
    if used["items"] or used["versions"]:
        raise ShotError("span_in_use", f"used by {used['items']} rundown item(s) and {used['versions']} version(s)")
    if row["kind"] == SHOT:
        other = overlaps_shot(store, row["asset_id"], frame_in, frame_out, exclude=span_id)
        if other:
            raise ShotError("overlaps_shot", f"overlaps shot {other}")
    raise ShotError("conflict", "the span changed while it was being written; try again")


def label(store: Store, span_id: str, new_label: Optional[str]) -> dict:
    """Rename a span of any kind. Allowed while it is in use: the label is not part of what a version was cut from."""
    value = _clean_label(new_label)
    with store.transaction():
        cur = store.conn.execute("UPDATE span SET label = ? WHERE id = ?", (value, span_id))
        if cur.rowcount != 1:
            raise ShotError("not_found", f"no such span: {span_id}")
    return get(store, span_id)


def delete(store: Store, span_id: str) -> None:
    """Remove a shot or clip that nothing uses. Transcript spans are never deleted here."""
    try:
        with store.transaction():
            cur = store.conn.execute(
                """DELETE FROM span WHERE id = ? AND kind IN ('shot', 'clip')
                   AND NOT EXISTS (SELECT 1 FROM item WHERE item.span_id = span.id)
                   AND NOT EXISTS (SELECT 1 FROM version WHERE version.span_id = span.id)""", (span_id,))
            deleted = cur.rowcount == 1
    except store.IntegrityError:
        # Postgres' foreign keys are the backstop if an item or version attached after the guard was evaluated.
        raise ShotError("span_in_use", "an item or version started using the span") from None
    if deleted:
        return
    row = store.conn.execute("SELECT kind FROM span WHERE id = ?", (span_id,)).fetchone()
    if row is None:
        raise ShotError("not_found", f"no such span: {span_id}")
    if row["kind"] not in MARK_KINDS:
        raise ShotError("not_deletable", f"a {row['kind']} span cannot be deleted")
    used = in_use(store, span_id)
    raise ShotError("span_in_use", f"used by {used['items']} rundown item(s) and {used['versions']} version(s)")


# ---- frame columns for spans that predate them -----------------------------------------------------

def pending_frames(store: Store, asset_id: str) -> int:
    """Spans of the asset whose frame columns are still NULL."""
    return store.conn.execute(
        "SELECT COUNT(*) FROM span WHERE asset_id = ? AND (frame_in IS NULL OR frame_out IS NULL)",
        (asset_id,)).fetchone()[0]


def request_span_frames(store: Store, asset_id: str):
    """Queue the backfill for an asset that needs it (the panel calls this when it first opens one). Returns
    the job, or None when every span already has frames. One job per asset, ever: `span-frames-v1`."""
    if not pending_frames(store, asset_id):
        return None
    return store.enqueue(asset_id, SPAN_FRAMES_TYPE, SPAN_FRAMES_KEY)


def backfill_frames(store: Store, asset_id: str) -> dict:
    """Fill frame_in/frame_out from each span's labels on the asset's timebase. Idempotent: it touches only
    rows still NULL, and each write re-checks that. A label that cannot be placed (does not exist on this
    timebase) is skipped and counted, never guessed, and does not stop the rest."""
    f = facts(store, asset_id, probe_rate=True)
    rows = store.conn.execute(
        "SELECT id, tc_in, tc_out FROM span WHERE asset_id = ? AND (frame_in IS NULL OR frame_out IS NULL) "
        "ORDER BY id", (asset_id,)).fetchall()
    filled = skipped = 0
    with store.transaction(f"span-frames:{asset_id}"):
        for r in rows:
            try:
                fi, fo = f.frames(r["tc_in"]), f.frames(r["tc_out"])
            except ShotError:
                skipped += 1
                continue
            cur = store.conn.execute(
                "UPDATE span SET frame_in = ?, frame_out = ? WHERE id = ? AND (frame_in IS NULL OR frame_out IS NULL)",
                (fi, fo, r["id"]))
            filled += cur.rowcount
    return {"filled": filled, "skipped": skipped}


# ---- split and merge --------------------------------------------------------------------------------
# Both are operator edits, so every piece they leave behind is origin 'manual' with no run_id: a later
# detection run replaces only its own untouched output and must not take an edited shot back.

def _shot_for_edit(store: Store, span_id: str):
    row = _row(store, span_id)
    if row["kind"] != SHOT:
        raise ShotError("not_editable", f"a {row['kind']} span cannot be split or merged; only shots can")
    return row


def _frames_of(f: Facts, row) -> tuple[int, int]:
    fi = row["frame_in"] if row["frame_in"] is not None else f.frames(row["tc_in"])
    fo = row["frame_out"] if row["frame_out"] is not None else f.frames(row["tc_out"])
    return fi, fo


def split(store: Store, span_id: str, at: str) -> tuple[dict, dict]:
    """Cut one shot into two at the frame `at` names: the first keeps the id, the label and the in; the second is
    new, unnamed, and runs from `at` to the old out. `at` must be strictly inside the shot. Refused when an item
    or version uses the shot. The shot's own row is rewritten only if it still has the in and out this call read
    (compare-and-set), so two splits racing on one shot cannot both win."""
    with store.transaction(f"shots:{_row(store, span_id)['asset_id']}"):
        row = _shot_for_edit(store, span_id)  # read again under the lock
        f = facts(store, row["asset_id"])
        old_in, old_out = _frames_of(f, row)
        cut = f.frames(at)
        if not old_in < cut < old_out:
            raise ShotError("bad_input", f"the split point must be inside the shot ({f.label(old_in)} to "
                                         f"{f.label(old_out)}) and not at either end")
        cur = store.conn.execute(
            """UPDATE span SET tc_out = ?, frame_out = ?, origin = 'manual', run_id = NULL
               WHERE id = ? AND kind = 'shot' AND frame_in = ? AND frame_out = ?
                 AND NOT EXISTS (SELECT 1 FROM item WHERE item.span_id = span.id)
                 AND NOT EXISTS (SELECT 1 FROM version WHERE version.span_id = span.id)""",
            (f.label(cut), cut, span_id, old_in, old_out))
        if cur.rowcount != 1:
            _explain_refusal(store, span_id, old_in, old_out)
        right = store.add_span(row["asset_id"], SHOT, f.label(cut), f.label(old_out), origin="manual",
                               frame_in=cut, frame_out=old_out)
    return get(store, span_id), get(store, right)


def merge(store: Store, span_id: str, with_id: str) -> dict:
    """Join a shot with the shot that starts exactly where it ends (`with_id`). The first span keeps its id and
    label and takes the other's out; the other is deleted. Refused when they are not the same asset's two
    neighbours (a gap is not adjacent), when either is not a shot, or when either is used by an item or version.
    Each write carries those conditions, and both happen in one transaction: all or nothing."""
    if span_id == with_id:
        raise ShotError("not_adjacent", "a shot cannot be merged with itself")
    with store.transaction(f"shots:{_row(store, span_id)['asset_id']}"):
        first, second = _shot_for_edit(store, span_id), _shot_for_edit(store, with_id)
        if first["asset_id"] != second["asset_id"]:
            raise ShotError("not_adjacent", "the shots belong to different assets")
        f = facts(store, first["asset_id"])
        first_in, first_out = _frames_of(f, first)
        second_in, second_out = _frames_of(f, second)
        if first_out != second_in:
            raise ShotError("not_adjacent", "the second shot must start exactly where the first ends")
        gone = store.conn.execute(
            """DELETE FROM span WHERE id = ? AND kind = 'shot' AND frame_in = ?
                 AND NOT EXISTS (SELECT 1 FROM item WHERE item.span_id = span.id)
                 AND NOT EXISTS (SELECT 1 FROM version WHERE version.span_id = span.id)""",
            (with_id, second_in))
        kept = store.conn.execute(
            """UPDATE span SET tc_out = ?, frame_out = ?, origin = 'manual', run_id = NULL
               WHERE id = ? AND kind = 'shot' AND frame_in = ? AND frame_out = ?
                 AND NOT EXISTS (SELECT 1 FROM item WHERE item.span_id = span.id)
                 AND NOT EXISTS (SELECT 1 FROM version WHERE version.span_id = span.id)""",
            (f.label(second_out), second_out, span_id, first_in, first_out))
        if gone.rowcount != 1 or kept.rowcount != 1:
            # Raising rolls the transaction back, so a half-done merge is never left behind.
            for sid in (span_id, with_id):
                used = in_use(store, sid)
                if used["items"] or used["versions"]:
                    raise ShotError("span_in_use", f"{sid} is used by {used['items']} rundown item(s) and "
                                                   f"{used['versions']} version(s)")
            raise ShotError("conflict", "a shot changed while the merge was being written; try again")
    return get(store, span_id)


# ---- thumbnails: the one cache path and frame rule --------------------------------------------------
# The detection job writes these at the end of a run and the thumbnail route makes a missing one on demand.
# They must agree on the file name, so both use these functions.

THUMB_WIDTH = 320
THUMB_TIMEOUT = 15  # seconds per thumbnail


def default_out_dir() -> Path:
    """VOLT_OUT, else control/out: the same default the session server uses for its output directory."""
    return Path(os.environ.get("VOLT_OUT") or Path(__file__).resolve().parents[1] / "out")


def thumb_frame(frame_in: int, frame_out: int) -> int:
    """The frame a span is shown by: frame_in + max(2, 10% of its length), kept inside the span, so the image
    is past the cut and not a transition blend. A one-frame span gets its only frame."""
    length = frame_out - frame_in
    return frame_in + min(max(2, length // 10), length - 1)


def thumb_path(out_dir, asset_id: str, span_id: str, frame: int) -> Path:
    """`<out_dir>/thumbs/<asset>/<span>-<frame>.jpg`. The frame is in the name, so a trim makes a new file."""
    return Path(out_dir) / "thumbs" / asset_id / f"{span_id}-{frame}.jpg"


def write_thumb(source, seconds: float, dest: Path, run=subprocess.run) -> None:
    """One ffmpeg seek on `source` into `dest`. ffmpeg writes a private part file that is renamed into place
    only when it is a whole, non-empty image, so a failure or a timeout leaves nothing behind that a later
    reader could take for a thumbnail. Raises ShotError('thumb_failed')."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex[:12]}.part.jpg")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{seconds:.4f}", "-i", str(source), "-frames:v", "1",
           "-an", "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", str(part)]
    try:
        try:
            out = run(cmd, capture_output=True, text=True, timeout=THUMB_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ShotError("thumb_failed", f"ffmpeg took longer than {THUMB_TIMEOUT} s") from None
        except OSError as exc:
            raise ShotError("thumb_failed", f"ffmpeg could not be run: {exc}") from None
        if out.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            detail = (out.stderr or "").strip()[-200:] or "no frame at that position"
            raise ShotError("thumb_failed", f"ffmpeg produced no thumbnail: {detail}")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


# ---- detection --------------------------------------------------------------------------------------

DETECT_TYPE = "shots"
DETECT_PREFIX = "shots-v1:"
ENGINE = "ffmpeg-scene"
# Set from measurement, not from practice: see "Scene threshold" in docs/specs/shot-spans.md. Real cuts in the
# measured program feeds scored 0.165 and up; dissolve and mix frames scored up to 0.097.
DEFAULT_THRESHOLD = 0.13
DEFAULT_MIN_FRAMES = 12
MAX_MIN_FRAMES = 10000
SCENE_WIDTH = 320  # the picture is scaled down to this before scoring: faster, and what the threshold was measured on


def check_settings(threshold, min_frames) -> tuple[float, int]:
    """Validate and normalise detection settings (None = the default). Refuses anything else, never clamps."""
    threshold = DEFAULT_THRESHOLD if threshold is None else threshold
    min_frames = DEFAULT_MIN_FRAMES if min_frames is None else min_frames
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold < 1:
        raise ShotError("bad_input", "threshold must be a number above 0 and below 1")
    if isinstance(min_frames, bool) or not isinstance(min_frames, int) or not 1 <= min_frames <= MAX_MIN_FRAMES:
        raise ShotError("bad_input", f"min_frames must be a whole number from 1 to {MAX_MIN_FRAMES}")
    return round(float(threshold), 4), min_frames


@lru_cache(maxsize=1)
def engine_version() -> str:
    """The ffmpeg version string (`8.1.1-full_build...`). Raises ShotError('engine_missing') when ffmpeg will not run;
    a failure is not cached."""
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=30)
        m = re.match(r"ffmpeg version (\S+)", out.stdout or "")
    except (OSError, subprocess.SubprocessError):
        m = None
    if m is None:
        raise ShotError("engine_missing", "ffmpeg is not available, so shots cannot be detected")
    return m.group(1)


def detect_key(threshold: float, min_frames: int, version: Optional[str] = None) -> str:
    """`shots-v1:<hash of engine, version, threshold, min_frames>:<threshold>:<min_frames>`. The same settings on the
    same asset are the same key (a no-op); any change is a new run. The readable tail is how the handler gets its
    settings back: the job row carries nothing else."""
    version = version or engine_version()
    digest = hashlib.sha256(f"{ENGINE}\0{version}\0{threshold!r}\0{min_frames}".encode()).hexdigest()[:16]
    return f"{DETECT_PREFIX}{digest}:{threshold!r}:{min_frames}"


def parse_detect_key(key: str) -> tuple[float, int]:
    parts = key.split(":")
    if len(parts) != 4 or parts[0] + ":" != DETECT_PREFIX:
        raise ShotError("bad_input", f"not a detection job key: {key!r}")
    try:
        return check_settings(float(parts[2]), int(parts[3]))
    except ValueError:
        raise ShotError("bad_input", f"not a detection job key: {key!r}") from None


def request_detect(store: Store, asset_id: str, threshold=None, min_frames=None):
    """Queue a detection run. Returns `(job, already_done)`: a repeat with the same settings (same engine version)
    is the same job, and `already_done` says its run has finished, so nothing is queued twice. Refused while the
    essence is still open (wrap first). Rights are not consulted: marking is allowed on any asset, held or not."""
    threshold, min_frames = check_settings(threshold, min_frames)
    f = facts(store, asset_id)
    if f.open:
        raise ShotError("essence_open", "the essence is still being recorded; detect after wrap")
    job = store.enqueue(asset_id, DETECT_TYPE, detect_key(threshold, min_frames))
    return job, job.status == "done"


def merge_short(cuts, total: int, min_frames: int) -> list[int]:
    """The cuts that survive the minimum-length rule. `cuts` are frame offsets from the file start (0 < c < total).
    A shot shorter than `min_frames` is merged into the previous one (its cut at the end is what stays); a short
    first shot has no previous and is merged into the next. A flash frame therefore costs at most the one cut
    after it, not a shot of its own."""
    bounds = [0] + sorted({c for c in cuts if 0 < c < total}) + [total]
    segs = [[bounds[i], bounds[i + 1]] for i in range(len(bounds) - 1)]
    out: list[list[int]] = []
    for seg in segs:
        if out and seg[1] - seg[0] < min_frames:
            out[-1][1] = seg[1]
        else:
            out.append(seg)
    if len(out) > 1 and out[0][1] - out[0][0] < min_frames:
        out[1][0] = out[0][0]
        out.pop(0)
    return [seg[0] for seg in out[1:]]


def _subtract(seg: tuple[int, int], kept: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """`seg` minus the (sorted, disjoint) kept ranges: the pieces of it nobody else already holds."""
    start, end = seg
    pieces = []
    for k_in, k_out in kept:
        if k_out <= start or k_in >= end:
            continue
        if k_in > start:
            pieces.append((start, k_in))
        start = max(start, k_out)
        if start >= end:
            break
    if start < end:
        pieces.append((start, end))
    return pieces


_TB = re.compile(r"config in time_base: (\d+)/(\d+)")
_SHOWINFO = re.compile(r"n:\s*\d+\s+pts:\s*(-?\d+)\s")


def scene_cuts(source, rate: Fraction, threshold: float, timeout: float, run=subprocess.run) -> list[int]:
    """Frame offsets from the start of `source` where ffmpeg's scene score exceeds `threshold`. The frame is the
    selected frame's pts on the stream's own time base, exact, then counted on the essence's rate: nothing goes
    through a printed decimal (showinfo's pts_time keeps six digits, which is a third of a second off at an hour).
    Raises RuntimeError when ffmpeg fails (the runner retries it) and ShotError('no_media') when it cannot start."""
    vf = f"scale=w='min({SCENE_WIDTH},iw)':h=-2:flags=bilinear,select='gt(scene,{threshold!r})',showinfo"
    cmd = ["ffmpeg", "-nostdin", "-nostats", "-v", "info", "-i", str(source), "-an", "-sn", "-dn", "-vf", vf,
           "-f", "null", "-"]
    try:
        out = run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg scene detection took longer than {timeout:.0f} s") from None
    except OSError as exc:
        raise ShotError("engine_missing", f"ffmpeg could not be run: {exc}") from None
    if out.returncode != 0:
        raise RuntimeError("ffmpeg scene detection failed: " + (out.stderr or "").strip()[-300:])
    tb = None
    frames: set[int] = set()
    for line in (out.stderr or "").splitlines():
        if "Parsed_showinfo" not in line:
            continue
        m = _TB.search(line)
        if m and tb is None and int(m.group(2)):
            tb = Fraction(int(m.group(1)), int(m.group(2)))
            continue
        m = _SHOWINFO.search(line)
        if m:
            if tb is None:
                raise RuntimeError("ffmpeg showinfo gave a frame before its time base; cannot place the cut")
            frames.add(round(int(m.group(1)) * tb * rate))
    return sorted(frames)


def _probe_rate(path) -> Optional[Fraction]:
    try:
        from volt.deliverables import DeliverableError, _probe_rate as probe
        return probe(str(path))
    except Exception:  # noqa: BLE001 - unreadable means "not the same"
        return None


def _choose_source(store: Store, f: Facts, asset_id: str, hires: str) -> tuple[str, str, Optional[str]]:
    """(path, 'proxy'|'hi-res', note). The proxy is read when it is closed, on disk, and has the hi-res's frame rate
    and frame count, so a frame number in one is the same frame in the other. Anything else reads the hi-res and
    says why. The proxy row carries no rate or start timecode (the builder does not write them), so the check is
    on the files themselves; both are counted from their first frame."""
    row = store.conn.execute(
        "SELECT location, open FROM essence WHERE asset_id = ? AND role = 'proxy'", (asset_id,)).fetchone()
    if row is None:
        return hires, "hi-res", "no proxy"
    if row["open"]:
        return hires, "hi-res", "the proxy is still growing"
    if not Path(row["location"]).is_file():
        return hires, "hi-res", "the proxy file is not on disk"
    rate = _probe_rate(row["location"])
    frames = probe_duration_frames(row["location"], f.rate)
    if rate != f.rate:
        return hires, "hi-res", f"the proxy's frame rate ({rate}) differs from the hi-res's ({f.rate})"
    if frames != f.duration:
        return hires, "hi-res", f"the proxy has {frames} frames and the hi-res {f.duration}"
    return row["location"], "proxy", None


def detect(store: Store, asset_id: str, *, threshold=None, min_frames=None, job_id: str,
           claimed_by: Optional[str] = None, out_dir=None, run=subprocess.run) -> dict:
    """Run one detection and write its shots, its `shot_run` row and its thumbnails. Returns a summary dict, or
    `{"lost": True}` when the job is no longer held by `claimed_by` (nothing is written then).

    Order: read the timebase and the source, score the whole file (nothing is written yet), then in ONE
    transaction under the asset's lock and with the job row held: confirm the lease, replace the run's own
    previous output, tile the rest, record the run. A failure anywhere before the commit changes nothing, so a
    crashed or lost run is retried whole. Thumbnails come after the commit; they are a cache the thumbnail route
    can rebuild, so a failed one is counted in the run's settings and logged, not fatal.

    Refusals (ShotError): essence_open (wait), not_found/bad_input/no_media/engine_missing (retrying will not help).
    Never consults rights: a held or uncleared asset is detected like any other."""
    threshold, min_frames = check_settings(threshold, min_frames)
    version = engine_version()
    f = facts(store, asset_id)
    if f.open:
        raise ShotError("essence_open", "the essence is still being recorded; detect after wrap")
    hires = store.conn.execute(
        "SELECT location FROM essence WHERE id = ?", (f.essence_id,)).fetchone()["location"]
    if not Path(hires).is_file():
        raise ShotError("no_media", f"the hi-res file is not on disk: {hires}")
    if f.duration is None:  # an essence from before duration was recorded: read it now and keep it
        length = probe_duration_frames(hires, f.rate)
        if length is None:
            raise ShotError("no_media", "the length of the hi-res file cannot be read")
        store.conn.execute("UPDATE essence SET duration_frames = ? WHERE id = ? AND duration_frames IS NULL",
                           (length, f.essence_id))
        store.conn.commit()
        f = facts(store, asset_id)
    total = f.duration
    source, source_kind, note = _choose_source(store, f, asset_id, hires)
    seconds = float(timecode.frames_to_seconds(total, f.rate))
    raw = [c for c in scene_cuts(source, f.rate, threshold, max(600.0, seconds * 6), run=run) if 0 < c < total]
    cuts = merge_short(raw, total, min_frames)
    bounds = [0] + cuts + [total]
    segments = [(f.start + bounds[i], f.start + bounds[i + 1]) for i in range(len(bounds) - 1)]

    created: list[tuple[str, int, int]] = []
    with store.transaction(f"shots:{asset_id}"):
        if claimed_by is not None:
            lock = " FOR UPDATE" if store.postgres else ""
            job = store.conn.execute(
                f"SELECT status, claimed_by FROM job WHERE id = ?{lock}", (job_id,)).fetchone()
            if job is None or job["status"] != "running" or job["claimed_by"] != claimed_by:
                log.warning("shots %s: the job is no longer ours; nothing written", job_id)
                return {"lost": True}
        now_f = facts(store, asset_id)
        if now_f.essence_id != f.essence_id or now_f.open or now_f.duration != total:
            raise ShotError("essence_open", "the essence changed while it was being read; try again")
        replaced = store.conn.execute(
            """DELETE FROM span WHERE asset_id = ? AND kind = 'shot' AND origin = 'detect'
                 AND NOT EXISTS (SELECT 1 FROM item WHERE item.span_id = span.id)
                 AND NOT EXISTS (SELECT 1 FROM version WHERE version.span_id = span.id)""",
            (asset_id,)).rowcount
        kept_rows = store.conn.execute(
            "SELECT frame_in, frame_out, origin FROM span WHERE asset_id = ? AND kind = 'shot' "
            "ORDER BY frame_in, id", (asset_id,)).fetchall()
        kept = [(r["frame_in"], r["frame_out"]) for r in kept_rows]
        for segment in segments:
            for fi, fo in _subtract(segment, kept):
                span_id = store.add_span(asset_id, SHOT, f.label(fi), f.label(fo), origin="detect",
                                         run_id=job_id, frame_in=fi, frame_out=fo)
                created.append((span_id, fi, fo))
        settings = {"engine": ENGINE, "engineVersion": version, "threshold": threshold, "minFrames": min_frames,
                    "source": source_kind, "sourceNote": note, "frames": total, "rate": str(f.rate),
                    "cutsFound": len(raw), "cutsKept": len(cuts), "replaced": replaced,
                    "kept": {"manual": sum(1 for r in kept_rows if r["origin"] != "detect"),
                             "detect": sum(1 for r in kept_rows if r["origin"] == "detect")}}
        store.conn.execute("DELETE FROM shot_run WHERE job_id = ?", (job_id,))
        store.conn.execute(
            """INSERT INTO shot_run (job_id, asset_id, essence_id, engine, engine_version, threshold, min_frames,
                                     shot_count, created_at, settings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, asset_id, f.essence_id, ENGINE, version, threshold, min_frames, len(created),
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), json.dumps(settings)))
    made = failed = 0
    out = Path(out_dir) if out_dir else default_out_dir()
    for span_id, fi, fo in created:
        frame = thumb_frame(fi, fo)
        dest = thumb_path(out, asset_id, span_id, frame)
        try:
            write_thumb(source, float(timecode.frames_to_seconds(frame - f.start, f.rate)), dest, run=run)
            made += 1
        except ShotError as exc:
            failed += 1
            log.warning("shots %s: thumbnail for %s not written: %s", job_id, span_id, exc.reason)
    settings["thumbnails"] = {"made": made, "failed": failed}
    store.conn.execute("UPDATE shot_run SET settings = ? WHERE job_id = ?", (json.dumps(settings), job_id))
    store.conn.commit()
    return {"shots": len(created), "replaced": replaced, "kept": settings["kept"], "cutsFound": len(raw),
            "cutsKept": len(cuts), "source": source_kind, "thumbnails": settings["thumbnails"]}
