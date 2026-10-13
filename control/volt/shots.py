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
publish keep that gate.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from volt import timecode
from volt.store import Store

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
