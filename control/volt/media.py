"""Read side of shot spans (slice 2 of docs/specs/shot-spans.md): listing, proxy ranges, thumbnails.

Nothing here writes control-plane state. The thumbnail cache is files under `VOLT_OUT/thumbs/`, safe to lose.

The client never names a file. A proxy is found by asset id (or, for the old shell's `/media?path=`, by
matching the given string against `essence.location` of registered proxies), so there is no path to traverse.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from volt import shots, timecode
from volt.operator_state import _blocked_by
from volt.shots import ShotError
from volt.store import Store

DEFAULT_KINDS = ("shot", "clip")
ALL_KINDS = ("shot", "clip", "transcript")
SPANS_DEFAULT, SPANS_MAX = 200, 500
WORDS_DEFAULT, WORDS_MAX = 500, 1000

THUMB_WIDTH = 320
THUMB_TIMEOUT = 15            # seconds; tests shrink it
# How ffmpeg is run for thumbnails. Tests swap this to inject a failure or a hang.
RUN = subprocess.run


class MediaError(Exception):
    """A refusal with an HTTP status. `code` is stable, `reason` is for people."""

    def __init__(self, status: int, code: str, reason: str, headers: Optional[dict] = None):
        super().__init__(reason)
        self.status, self.code, self.reason, self.headers = status, code, reason, headers or {}


def shot_error(exc: ShotError) -> MediaError:
    status = 404 if exc.code == "not_found" else 422
    return MediaError(status, exc.code, exc.reason)


# ---- listing --------------------------------------------------------------------------------------

def _single(query: dict, allowed: set) -> dict:
    extra = sorted(set(query) - allowed)
    if extra:
        raise MediaError(422, "bad_input", f"unknown parameter(s): {', '.join(extra)}")
    out = {}
    for key, values in query.items():
        if len(values) != 1:
            raise MediaError(422, "bad_input", f"{key} may be given once")
        out[key] = values[0]
    return out


def _limit(raw: Optional[str], default: int, maximum: int) -> int:
    if raw is None:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        raise MediaError(422, "bad_input", f"limit must be a whole number from 1 to {maximum}")
    return int(raw)


def _encode_cursor(frame_in: int, span_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([frame_in, span_id]).encode()).decode().rstrip("=")


def _decode_cursor(raw: str):
    try:
        frame_in, span_id = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if type(frame_in) is not int or not isinstance(span_id, str):
            raise ValueError
        return frame_in, span_id
    except (ValueError, TypeError, binascii.Error):
        raise MediaError(422, "bad_input", "after is not a cursor this server issued") from None


def _span_row(row, blocked) -> dict:
    return {"id": row["id"], "assetId": row["asset_id"], "kind": row["kind"], "origin": row["origin"],
            "label": row["label"], "tcIn": row["tc_in"], "tcOut": row["tc_out"],
            "frameIn": row["frame_in"], "frameOut": row["frame_out"], "text": row["text"],
            "runId": row["run_id"], "blockedBy": blocked,
            "usedBy": {"items": row["n_items"], "versions": row["n_versions"]}}


def _page(store: Store, asset_id: str, kinds, frame_from, frame_to, after, limit, blocked) -> tuple:
    where, args = ["asset_id = ?", "frame_in IS NOT NULL", "frame_out IS NOT NULL"], [asset_id]
    where.append("kind IN (" + ", ".join("?" for _ in kinds) + ")")
    args += list(kinds)
    if frame_from is not None:
        where.append("frame_out > ?")
        args.append(frame_from)
    if frame_to is not None:
        where.append("frame_in < ?")
        args.append(frame_to)
    if after is not None:
        where.append("(frame_in > ? OR (frame_in = ? AND id > ?))")
        args += [after[0], after[0], after[1]]
    rows = store.conn.execute(
        f"""SELECT id, asset_id, kind, tc_in, tc_out, text, label, origin, run_id, frame_in, frame_out,
                   (SELECT COUNT(*) FROM item WHERE item.span_id = span.id) AS n_items,
                   (SELECT COUNT(*) FROM version WHERE version.span_id = span.id) AS n_versions
            FROM span WHERE {' AND '.join(where)} ORDER BY frame_in, id LIMIT ?""",
        args + [limit + 1]).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = _encode_cursor(rows[-1]["frame_in"], rows[-1]["id"]) if more else None
    return [_span_row(r, blocked) for r in rows], nxt


def _window(store: Store, asset_id: str, q: dict) -> tuple:
    """from/to labels to frames. Needs the essence timebase, so an asset without one refuses them."""
    if "from" not in q and "to" not in q:
        return None, None
    try:
        f = shots.facts(store, asset_id)
        lo = f.frames(q["from"]) if "from" in q else None
        hi = f.frames(q["to"]) if "to" in q else None
    except ShotError as exc:
        raise shot_error(exc) from None
    if lo is not None and hi is not None and hi <= lo:
        raise MediaError(422, "bad_input", "to must be after from")
    return lo, hi


def _asset_row(store: Store, asset_id: str):
    row = store.conn.execute("SELECT cleared, legal_hold FROM asset WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise MediaError(404, "not_found", f"no such asset: {asset_id}")
    return row


def header(store: Store, asset_id: str, asset) -> dict:
    """The asset facts the panel header needs. Fields the essence cannot supply are null."""
    ess = store.conn.execute(
        "SELECT frame_rate, timecode_start, duration_frames, open FROM essence "
        "WHERE asset_id = ? AND role = 'hi-res'", (asset_id,)).fetchone()
    proxy = store.conn.execute(
        "SELECT 1 FROM essence WHERE asset_id = ? AND role = 'proxy'", (asset_id,)).fetchone()
    run = store.conn.execute(
        "SELECT job_id, essence_id, engine, engine_version, threshold, min_frames, shot_count, created_at "
        "FROM shot_run WHERE asset_id = ? ORDER BY created_at DESC, job_id DESC LIMIT 1", (asset_id,)).fetchone()
    return {
        "assetId": asset_id,
        "rate": ess["frame_rate"] if ess else None,
        "start": (ess["timecode_start"] or "00:00:00:00") if ess else None,
        "durationFrames": ess["duration_frames"] if ess else None,
        "open": bool(ess["open"]) if ess else False,
        "cleared": bool(asset["cleared"]), "legalHold": bool(asset["legal_hold"]),
        "hasProxy": proxy is not None,
        "pendingFrames": shots.pending_frames(store, asset_id),
        "lastRun": None if run is None else {
            "job": run["job_id"], "essence": run["essence_id"], "engine": run["engine"],
            "engineVersion": run["engine_version"], "threshold": run["threshold"],
            "minFrames": run["min_frames"], "shotCount": run["shot_count"], "createdAt": str(run["created_at"])},
    }


def list_spans(store: Store, asset_id: str, query: dict) -> dict:
    """GET /api/assets/{asset}/spans. Ordered by frame_in, id. Spans whose frame columns are not yet
    backfilled cannot be ordered or windowed and are left out; `pendingFrames` says how many."""
    asset = _asset_row(store, asset_id)
    q = _single(query, {"kind", "from", "to", "limit", "after"})
    kinds = tuple(dict.fromkeys(k for k in q.get("kind", ",".join(DEFAULT_KINDS)).split(",") if k))
    if not kinds or any(k not in ALL_KINDS for k in kinds):
        raise MediaError(422, "bad_input", f"kind must be a comma list of {', '.join(ALL_KINDS)}")
    limit = _limit(q.get("limit"), SPANS_DEFAULT, SPANS_MAX)
    lo, hi = _window(store, asset_id, q)
    after = _decode_cursor(q["after"]) if "after" in q else None
    blocked = _blocked_by(bool(asset["cleared"]), bool(asset["legal_hold"]))
    spans, nxt = _page(store, asset_id, kinds, lo, hi, after, limit, blocked)
    return {"ok": True, **header(store, asset_id, asset), "spans": spans, "next": nxt}


def transcript(store: Store, asset_id: str, query: dict) -> dict:
    """GET /api/assets/{asset}/transcript: word spans in a window, paged."""
    asset = _asset_row(store, asset_id)
    q = _single(query, {"from", "to", "limit", "after"})
    limit = _limit(q.get("limit"), WORDS_DEFAULT, WORDS_MAX)
    lo, hi = _window(store, asset_id, q)
    after = _decode_cursor(q["after"]) if "after" in q else None
    blocked = _blocked_by(bool(asset["cleared"]), bool(asset["legal_hold"]))
    words, nxt = _page(store, asset_id, ("transcript",), lo, hi, after, limit, blocked)
    return {"ok": True, "assetId": asset_id, "words": words, "next": nxt,
            "pendingFrames": shots.pending_frames(store, asset_id)}


# ---- proxy and HTTP Range -------------------------------------------------------------------------

@dataclass(frozen=True)
class Proxy:
    asset_id: str
    path: Path


def proxy_for_asset(store: Store, asset_id: str) -> Proxy:
    _asset_row(store, asset_id)
    row = store.conn.execute(
        "SELECT location FROM essence WHERE asset_id = ? AND role = 'proxy'", (asset_id,)).fetchone()
    if row is None:
        raise MediaError(404, "no_proxy", "this asset has no proxy yet")
    return Proxy(asset_id, Path(row["location"]))


def proxy_by_location(store: Store, location: str) -> Proxy:
    """The old shell's /media?path=. The string must equal a registered proxy's location exactly; any other
    path, existing or not, is the same 404. The string is only ever compared, never opened."""
    row = store.conn.execute(
        "SELECT asset_id, location FROM essence WHERE role = 'proxy' AND location = ?", (location,)).fetchone()
    if row is None:
        raise MediaError(404, "not_found", "no such media")
    return Proxy(row["asset_id"], Path(row["location"]))


def open_proxy(proxy: Proxy):
    """Open the registered file. A row whose file is gone is a 404, not a 500."""
    try:
        return open(proxy.path, "rb")
    except OSError:
        raise MediaError(404, "no_proxy", "the proxy file is not on disk") from None


def parse_range(header_value: Optional[str], size: int) -> Optional[tuple]:
    """(first, last) inclusive for a satisfiable single byte range; None when the header should be ignored
    (absent, another unit, or several ranges: a full 200 is always a legal answer to those).
    Malformed or unsatisfiable raises 416 with `Content-Range: bytes */size`."""
    if not header_value:
        return None
    unit, _, spec = header_value.partition("=")
    if unit.strip().lower() != "bytes":
        return None
    spec = spec.strip()
    if "," in spec:
        return None
    bad = MediaError(416, "bad_range", "the requested range cannot be served", {"Content-Range": f"bytes */{size}"})
    first, dash, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    if not dash or not (first or last) or (first and not first.isdigit()) or (last and not last.isdigit()):
        raise bad
    if first:
        a = int(first)
        b = int(last) if last else size - 1
        if last and b < a:
            raise bad
        if a >= size:
            raise bad
        return a, min(b, size - 1)
    n = int(last)                      # suffix: the final n bytes
    if n == 0 or size == 0:
        raise bad
    return max(0, size - n), size - 1


CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
                 ".mkv": "video/x-matroska"}


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


# ---- thumbnails -----------------------------------------------------------------------------------

def thumb_frame(frame_in: int, frame_out: int) -> int:
    """Frame rule from the spec: frame_in + max(2, 10% of length), kept inside the span, so the image is past
    the cut and not a transition blend. A one-frame span gets its only frame."""
    length = frame_out - frame_in
    return frame_in + min(max(2, length // 10), length - 1)


def thumb_path(out_dir: Path, asset_id: str, span_id: str, frame: int) -> Path:
    return Path(out_dir) / "thumbs" / asset_id / f"{span_id}-{frame}.jpg"


def ensure_thumb(store: Store, span_id: str, out_dir: Path) -> Path:
    """The cached thumbnail of a span, made first if missing: one ffmpeg seek on the proxy, THUMB_TIMEOUT
    seconds at most, THUMB_WIDTH wide. ffmpeg writes a private part file that is renamed into place only when
    it is a whole non-empty image, so a failure or timeout leaves nothing a later request could mistake for a
    thumbnail."""
    row = store.conn.execute(
        "SELECT id, asset_id, tc_in, tc_out, frame_in, frame_out FROM span WHERE id = ?", (span_id,)).fetchone()
    if row is None:
        raise MediaError(404, "not_found", f"no such span: {span_id}")
    proxy = proxy_for_asset(store, row["asset_id"])
    try:
        f = shots.facts(store, row["asset_id"])
        fi = row["frame_in"] if row["frame_in"] is not None else f.frames(row["tc_in"])
        fo = row["frame_out"] if row["frame_out"] is not None else f.frames(row["tc_out"])
    except ShotError as exc:
        raise shot_error(exc) from None
    frame = thumb_frame(fi, fo)
    dest = thumb_path(out_dir, row["asset_id"], span_id, frame)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    if not proxy.path.is_file():
        raise MediaError(404, "no_proxy", "the proxy file is not on disk")
    # The proxy is assumed to share the hi-res's start timecode and rate (docs/specs/shot-spans.md).
    seconds = float(timecode.frames_to_seconds(frame - f.start, f.rate))
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex[:12]}.part.jpg")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{seconds:.4f}", "-i", str(proxy.path),
           "-frames:v", "1", "-an", "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "4", str(part)]
    try:
        try:
            run = RUN(cmd, capture_output=True, text=True, timeout=THUMB_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise MediaError(504, "thumb_timeout", f"ffmpeg took longer than {THUMB_TIMEOUT} s") from None
        except OSError as exc:
            raise MediaError(502, "thumb_failed", f"ffmpeg could not be run: {exc}") from None
        if run.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            detail = (run.stderr or "").strip()[-200:] or "no frame at that position"
            raise MediaError(502, "thumb_failed", f"ffmpeg produced no thumbnail: {detail}")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return dest
