"""NLE export (slice 6 of docs/specs/shot-spans.md): a selection of spans as an xmeml or CMX3600 file.

Stateless. Nothing is stored and no media is read, moved or copied: the file names where the media already is.

Two halves:

* Builders (`build_xmeml`, `build_edl`, `build`) are pure. They take `Item`s (a span already resolved onto its
  essence: absolute frame numbers from midnight on the essence's exact rate) and write text. All frame
  arithmetic is whole frames through `volt/timecode.py`, the same library the publish path uses, so drop-frame
  is not re-derived here.
* `export` is the store-facing half behind `POST /api/exports/nle`: resolves span IDs, applies the rights
  gate, picks the essence, maps paths, refuses what cannot be exported, then calls a builder.

Frame convention. A span is half open: `frame_in` is its first frame and `frame_out` the first frame after
it, so its length is `frame_out - frame_in`. xmeml `in`/`out` are frames from the first frame of the file
(`frame - essence start`) with `out` exclusive, which is how xmeml counts (a clip of N frames has out = in + N).
On the sequence, clip `start`/`end` are frames from the sequence's first frame, butted: each clip starts where
the last one ended. The sequence's own start timecode (`start_tc`) is carried in `sequence/timecode`.

FORMAT STATUS: the xmeml layout is written from knowledge of the Final Cut Pro 7 XML interchange format and
has NOT been imported into Premiere or Resolve. See docs/specs/shot-spans.md ("Manual import check").
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional
from urllib.parse import quote

from volt import shots, timecode
from volt.store import Store

MAX_SPANS = 500
DEFAULT_START_TC = "01:00:00:00"
DEFAULT_NAME = "Volt selects"
FORMATS = ("xmeml", "edl")
MEDIA = ("hi-res", "proxy")
MAX_CHANNELS = 32
PREVIEW_LINES = 20
SAMPLE_RATE = 48000       # essence carries no sample rate; assumed and written as such
SAMPLE_DEPTH = 16

# Where export lines go (span IDs and the file's sha256). Tests replace it.
LOG = lambda line: print(line, file=sys.stderr)  # noqa: E731


class NleError(Exception):
    """A refusal with an HTTP status. `code` is stable, `reason` is for people. `extra` is merged into the body."""

    def __init__(self, status: int, code: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra


def bad(reason: str) -> NleError:
    return NleError(422, "bad_input", reason)


# ---- small pure helpers ------------------------------------------------------------------------------

_BAD_XML = re.compile("[^\u0009\u000a\u000d\u0020-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def clean(text, limit: int = 500) -> str:
    """Text safe for an XML element or one EDL line: control characters out, whitespace runs folded."""
    text = _BAD_XML.sub("", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def rate_parts(rate) -> tuple:
    """(timebase, ntsc) as xmeml writes them: 25 -> (25, False); 30000/1001 -> (30, True). A rate that is
    neither a whole number nor a /1001 broadcast rate cannot be written and is refused."""
    r = timecode.parse_rate(rate)
    if r.denominator == 1:
        return int(r), False
    if r.denominator == 1001 and r.numerator % 1000 == 0:
        return r.numerator // 1000, True
    raise NleError(422, "unsupported_rate", f"frame rate {r} cannot be written to xmeml or EDL")


def can_drop(rate) -> bool:
    try:
        timecode.drop_per_minute(rate)
        return True
    except timecode.TimecodeError:
        return False


def parse_layout(layout) -> Optional[int]:
    """Channel count from `essence.audio_layout`, free text; ingest and wrap write the first audio stream's channel count. Understood:
    a bare count ("2", "8ch", "4 channels"), mono, stereo, quad, and x.y as in 5.1 or 7.1 (x + y channels).
    Anything else is None: no audio tracks are written and the caller warns."""
    text = str(layout or "").strip().lower()
    if not text:
        return None
    named = {"mono": 1, "stereo": 2, "quad": 4, "5.1": 6, "7.1": 8, "2.0": 2, "1.0": 1}
    if text in named:
        n = named[text]
    else:
        m = re.fullmatch(r"(\d+)\s*(?:ch|channels?)?", text)
        if not m:
            return None
        n = int(m.group(1))
    return n if 1 <= n <= MAX_CHANNELS else None


def parse_path_map(text: Optional[str]) -> list:
    """`D:\\iso=\\\\nas\\iso;E:\\cam=/Volumes/cam` -> [(src, dst), ...], longest source first. Entries split on the
    first `=`; blanks are skipped. A bad entry is a configuration fault (500), not the caller's."""
    pairs = []
    for entry in (text or "").split(";"):
        if not entry.strip():
            continue
        src, eq, dst = entry.partition("=")
        if not eq or not src.strip() or not dst.strip():
            raise NleError(500, "bad_path_map", "VOLT_EXPORT_PATH_MAP is malformed: expected `from=to` entries "
                                                 "separated by `;`")
        pairs.append((src.strip(), dst.strip()))
    return sorted(pairs, key=lambda p: -len(p[0]))


def _is_windows_path(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", p)) or p.startswith("\\\\")


def map_path(path: str, pairs: list) -> str:
    """Rewrite a leading prefix. Matches on a path-segment boundary, so `D:\\iso` does not match `D:\\iso2`.
    Windows-style paths match case-insensitively and either slash; others match exactly. The rest of the
    path takes the separator style of the replacement."""
    for src, dst in pairs:
        win = _is_windows_path(src)
        norm = (lambda s: s.replace("\\", "/")) if win else (lambda s: s)
        a, b = norm(path), norm(src).rstrip("/")
        head = a[:len(b)]
        if (head.lower() == b.lower() if win else head == b) and (len(a) == len(b) or a[len(b)] == "/"):
            rest = a[len(b):]
            if "\\" in dst:
                rest = rest.replace("/", "\\")
            return dst.rstrip("\\/") + rest
    return path


def file_url(path: str) -> str:
    """xmeml `pathurl`. `file://localhost/` + the path with `/` separators, percent-encoded; a drive colon is
    written `%3a` as FCP7 and Premiere do (`file://localhost/C%3a/media/a.mov`); a UNC share keeps its two
    slashes (`file://localhost//nas/iso/a.mov`). Relative paths and URLs are not openable by an NLE: refused."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", path):
        raise NleError(409, "not_local", "the media location is not a file path an NLE can open")
    p = path.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.*)?$", p)
    if m:
        body = quote(m.group(2) or "/", safe="/")
        return f"file://localhost/{m.group(1)}%3a{body}"
    if p.startswith("/"):
        return "file://localhost" + quote(p, safe="/")
    raise NleError(409, "not_local", "the media location is not an absolute file path")


def file_name(path: str) -> str:
    return ntpath.basename(path.replace("/", "\\")) or path


# ---- resolved inputs ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    """One essence as exported: where it is, and the timebase its frame numbers count on."""
    key: str                      # unique per (asset, role)
    asset_id: str
    path: str                     # as written to the file: already mapped
    rate: Fraction
    drop: bool                    # the essence's own labels are drop-frame
    start: int                    # frames from midnight of the essence's first frame
    duration: Optional[int]       # frames, None unknown
    channels: Optional[int]       # audio channels, None unknown or none
    reel: str = ""                # EDL reel, set by build_edl when empty
    location: str = ""            # the essence's location as stored, before the path map


@dataclass(frozen=True)
class Item:
    span_id: str
    source: Source
    frame_in: int                 # absolute, from midnight, first frame of the span
    frame_out: int                # absolute, first frame after the span
    name: str
    comment: str = ""
    text: str = ""

    @property
    def length(self) -> int:
        return self.frame_out - self.frame_in


@dataclass
class Built:
    content: str
    mime: str
    extension: str
    duration_frames: int
    rate: Fraction
    start_tc: str
    drop: bool
    warnings: list = field(default_factory=list)


# ---- timeline arithmetic ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    rate: Fraction
    drop: bool                    # the sequence's timecode is drop-frame
    start_tc: str
    start_frames: int             # sequence start, frames from midnight
    placed: tuple                 # ((item, rec_start, rec_end), ...) relative to the sequence start
    duration: int


def plan(items: list, start_tc: Optional[str] = None) -> Plan:
    """Butt the items end to end. The sequence rate is the items' common rate (callers refuse a mix first).
    Default start is 01:00:00:00, written with `;` when the first source is drop-frame, so a drop-frame
    selection gets a drop-frame sequence. An explicit `start_tc` decides drop-frame by its own separator."""
    if not items:
        raise bad("nothing to export")
    rate = items[0].source.rate
    if any(i.source.rate != rate for i in items):
        raise NleError(422, "mixed_rates", "the selection mixes frame rates")
    for i in items:
        if i.length < 1:
            raise bad(f"span {i.span_id} has no frames")
    rate_parts(rate)
    if start_tc is None:
        drop = items[0].source.drop and can_drop(rate)
        start_tc = DEFAULT_START_TC.replace(":", ";") if False else \
            timecode.frames_to_tc(timecode.tc_to_frames(DEFAULT_START_TC, rate, drop), rate, drop)
    else:
        drop = timecode.is_drop(start_tc)
        if drop and not can_drop(rate):
            raise bad(f"start_tc is drop-frame but {rate} has no drop-frame form")
    try:
        start_frames = timecode.tc_to_frames(start_tc, rate, drop)
    except timecode.TimecodeError as exc:
        raise bad(f"start_tc: {exc}") from None
    pos, placed = 0, []
    for i in items:
        placed.append((i, pos, pos + i.length))
        pos += i.length
    return Plan(rate, drop, start_tc, start_frames, tuple(placed), pos)


# ---- xmeml ------------------------------------------------------------------------------------------

def _el(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        e.text = str(text)
    return e


def _rate(parent, rate):
    base, ntsc = rate_parts(rate)
    r = _el(parent, "rate")
    _el(r, "timebase", base)
    _el(r, "ntsc", "TRUE" if ntsc else "FALSE")
    return r


def _timecode(parent, rate, label, frames, drop):
    tc = _el(parent, "timecode")
    _rate(tc, rate)
    _el(tc, "string", label)
    _el(tc, "frame", frames)
    _el(tc, "displayformat", "DF" if drop else "NDF")
    return tc


def _file(parent, src: Source, file_id: str, first_use: bool):
    f = _el(parent, "file", id=file_id)
    if not first_use:
        return f                                   # the xmeml convention: later uses are a bare reference
    _el(f, "name", clean(file_name(src.path)))
    _el(f, "pathurl", file_url(src.path))
    _rate(f, src.rate)
    if src.duration is not None:
        _el(f, "duration", src.duration)
    _timecode(f, src.rate, timecode.frames_to_tc(src.start, src.rate, src.drop), src.start, src.drop)
    media = _el(f, "media")
    video = _el(media, "video")
    sc = _el(video, "samplecharacteristics")
    _rate(sc, src.rate)
    if src.channels:
        audio = _el(media, "audio")
        a = _el(audio, "samplecharacteristics")
        _el(a, "depth", SAMPLE_DEPTH)
        _el(a, "samplerate", SAMPLE_RATE)
        _el(audio, "channelcount", src.channels)
    return f


def build_xmeml(items: list, name: str = DEFAULT_NAME, start_tc: Optional[str] = None) -> Built:
    p = plan(items, start_tc)
    warnings = []
    n_audio = max((i.source.channels or 0) for i in items)
    if any(not i.source.channels for i in items) and n_audio:
        warnings.append("some sources have no known audio layout; their audio tracks are empty")
    if not n_audio:
        warnings.append("no source has a known audio layout: no audio tracks were written")
    n = len(items)

    root = ET.Element("xmeml", version="4")
    seq = _el(root, "sequence", id="sequence-1")
    _el(seq, "name", clean(name, 200))
    _el(seq, "duration", p.duration)
    _rate(seq, p.rate)
    _timecode(seq, p.rate, p.start_tc, p.start_frames, p.drop)
    media = _el(seq, "media")
    video = _el(media, "video")
    vformat = _el(_el(video, "format"), "samplecharacteristics")
    _rate(vformat, p.rate)
    _el(vformat, "pixelaspectratio", "square")
    vtrack = _el(video, "track")

    file_ids: dict = {}
    for src in (i.source for i in items):
        file_ids.setdefault(src.key, f"file-{len(file_ids) + 1}")
    seen_files: set = set()
    clip_ids = {}                                    # (kind, track, index) -> id
    for idx in range(1, n + 1):
        clip_ids[("video", 1, idx)] = f"clipitem-{idx}"
    for ch in range(1, n_audio + 1):
        for idx in range(1, n + 1):
            clip_ids[("audio", ch, idx)] = f"clipitem-{n + (ch - 1) * n + idx}"

    def clipitem(parent, kind, track, idx, item, rec_start, rec_end):
        src = item.source
        c = _el(parent, "clipitem", id=clip_ids[(kind, track, idx)])
        _el(c, "masterclipid", f"masterclip-{file_ids[src.key].split('-')[1]}")
        _el(c, "name", clean(item.name, 200))
        _el(c, "enabled", "TRUE")
        _el(c, "duration", item.length)
        _rate(c, src.rate)
        _el(c, "start", rec_start)
        _el(c, "end", rec_end)
        _el(c, "in", item.frame_in - src.start)
        _el(c, "out", item.frame_out - src.start)
        if kind == "video":
            _el(c, "pixelaspectratio", "square")
        fid = file_ids[src.key]
        _file(c, src, fid, fid not in seen_files)
        seen_files.add(fid)
        if kind == "audio":
            st = _el(c, "sourcetrack")
            _el(st, "mediatype", "audio")
            _el(st, "trackindex", track)
        for (k, t, i2), cid in clip_ids.items():
            if i2 != idx:
                continue
            if k == "audio" and (item.source.channels or 0) < t:
                continue
            link = _el(c, "link")
            _el(link, "linkclipref", cid)
            _el(link, "mediatype", k)
            _el(link, "trackindex", t)
            _el(link, "clipindex", i2)
            if k == "audio":
                _el(link, "groupindex", 1)
        _el(_el(c, "logginginfo"), "description", clean(item.comment, 200))
        cm = _el(c, "comments")
        _el(cm, "mastercomment1", clean(item.comment, 200))
        if item.text:
            _el(cm, "mastercomment2", clean(item.text, 500))
        return c

    for idx, (item, a, b) in enumerate(p.placed, 1):
        clipitem(vtrack, "video", 1, idx, item, a, b)
    _el(vtrack, "enabled", "TRUE")
    _el(vtrack, "locked", "FALSE")
    if n_audio:
        audio = _el(media, "audio")
        fmt = _el(_el(audio, "format"), "samplecharacteristics")
        _el(fmt, "depth", SAMPLE_DEPTH)
        _el(fmt, "samplerate", SAMPLE_RATE)
        for ch in range(1, n_audio + 1):
            track = _el(audio, "track")
            for idx, (item, a, b) in enumerate(p.placed, 1):
                if (item.source.channels or 0) >= ch:
                    clipitem(track, "audio", ch, idx, item, a, b)
            _el(track, "enabled", "TRUE")
            _el(track, "locked", "FALSE")

    ET.indent(root)
    body = ET.tostring(root, encoding="unicode")
    content = '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n"
    return Built(content, "application/xml", "xml", p.duration, p.rate, p.start_tc, p.drop, warnings)


# ---- CMX3600 EDL ------------------------------------------------------------------------------------

def reel_names(items: list) -> dict:
    """asset id -> 8-character reel. Upper-cased asset ID with everything but letters and digits dropped, cut to
    8. Two different assets that cut to the same 8 characters would be one reel to an NLE, so a later one has its
    last two characters replaced by a counter (01, 02...)."""
    out, taken = {}, set()
    for i in items:
        aid = i.source.asset_id
        if aid in out:
            continue
        base = re.sub(r"[^A-Z0-9]", "", aid.upper())[:8] or "VOLT"
        reel, n = base, 0
        while reel in taken:
            n += 1
            reel = base[:6] + f"{n:02d}"
        taken.add(reel)
        out[aid] = reel
    return out


def build_edl(items: list, name: str = DEFAULT_NAME, start_tc: Optional[str] = None) -> Built:
    p = plan(items, start_tc)
    warnings = []
    reels = reel_names(items)
    collided = {a for a in reels if reels[a] != re.sub(r"[^A-Z0-9]", "", a.upper())[:8]}
    if collided:
        warnings.append("two assets share the same first 8 reel characters; later ones got a numbered reel")
    if any((i.source.channels or 0) > 2 for i in items):
        warnings.append("an EDL audio event carries channels 1 and 2 only; further channels are not in the EDL")
    lines = [f"TITLE: {clean(name, 200)}", f"FCM: {'DROP FRAME' if p.drop else 'NON-DROP FRAME'}"]
    lines.append("")
    event = 0

    def tc(frames, rate, drop):
        return timecode.frames_to_tc(frames, rate, drop)

    for item, a, b in p.placed:
        src = item.source
        rec_in, rec_out = tc(p.start_frames + a, p.rate, p.drop), tc(p.start_frames + b, p.rate, p.drop)
        s_in, s_out = tc(item.frame_in, src.rate, src.drop), tc(item.frame_out, src.rate, src.drop)
        tracks = ["V"] + ([("A" if src.channels == 1 else "AA")] if src.channels else [])
        for track in tracks:
            event += 1
            lines.append(f"{event:03d}  {reels[src.asset_id]:<8} {track:<6}{'C':<9}{s_in} {s_out} {rec_in} {rec_out}")
            lines.append(f"* FROM CLIP NAME: {clean(item.name, 200)}")
            lines.append(f"* COMMENT: {clean(item.comment, 200)}")
    content = "\n".join(lines) + "\n"
    return Built(content, "text/plain", "edl", p.duration, p.rate, p.start_tc, p.drop, warnings)


def build(fmt: str, items: list, name: str = DEFAULT_NAME, start_tc: Optional[str] = None) -> Built:
    if fmt == "xmeml":
        return build_xmeml(items, name, start_tc)
    if fmt == "edl":
        return build_edl(items, name, start_tc)
    raise bad("format must be xmeml or edl")


def filename(name: str, extension: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", clean(name, 80)).strip("._") or "volt-selects"
    return f"{stem}.{extension}"


# ---- the request ------------------------------------------------------------------------------------

def _typed(body: dict, key: str, kind, default):
    value = body.get(key, default)
    if not isinstance(value, kind):
        raise bad(f"{key} must be {'true or false' if kind is bool else 'a string'}")
    return value


def parse_request(body: dict) -> dict:
    # skip_blocked is still ACCEPTED (and must be a bool) but ignored: nothing blocks an export any more, and
    # rejecting it would 422 clients written against the earlier contract.
    _typed(body, "skip_blocked", bool, False)
    extra = sorted(set(body) - {"format", "spans", "media", "name", "start_tc", "dry_run", "skip_blocked"})
    if extra:
        raise bad(f"unknown field(s): {', '.join(extra)}")
    fmt = body.get("format")
    if fmt not in FORMATS:
        raise bad("format must be xmeml or edl")
    spans = body.get("spans")
    if not isinstance(spans, list) or not spans or not all(isinstance(s, str) and 0 < len(s) <= 128 for s in spans):
        raise bad("spans must be a non-empty list of span ids")
    if len(spans) > MAX_SPANS:
        raise bad(f"spans is limited to {MAX_SPANS}")
    media = body.get("media", "hi-res")
    if media not in MEDIA:
        raise bad("media must be hi-res or proxy")
    name = _typed(body, "name", str, DEFAULT_NAME)
    if len(name) > 200 or not clean(name):
        raise bad("name must be 1 to 200 printable characters")
    start_tc = body.get("start_tc")
    if start_tc is not None and (not isinstance(start_tc, str) or len(start_tc) > 20):
        raise bad("start_tc must be a timecode string")
    return {"format": fmt, "spans": spans, "media": media, "name": name, "start_tc": start_tc,
            "dry_run": _typed(body, "dry_run", bool, False)}


HOLD_NOTE = ("on legal hold: an export is allowed because it leaves the original source untouched; "
             "publish and air stay blocked")
NOT_CLEARED_NOTE = ("not cleared: an export is allowed because it leaves the original source untouched; "
                    "publish and air stay blocked until it is cleared")
BOTH_NOTE = ("not cleared and on legal hold: an export is allowed because it leaves the original source "
             "untouched; publish and air stay blocked until it is cleared and the hold is lifted")


def _lookup(store: Store, span_id: str):
    row = store.conn.execute(
        "SELECT s.id, s.asset_id, s.kind, s.label, s.text, s.tc_in, s.tc_out, s.frame_in, s.frame_out, "
        "a.cleared, a.legal_hold, a.camera FROM span s JOIN asset a ON a.id = s.asset_id WHERE s.id = ?",
        (span_id,)).fetchone()
    if row is None:
        raise NleError(404, "not_found", f"no such span: {span_id}")
    return row


def _essence(store: Store, asset_id: str, role: str):
    return store.conn.execute(
        "SELECT location, frame_rate, timecode_start, duration_frames, audio_layout, open "
        "FROM essence WHERE asset_id = ? AND role = ?", (asset_id, role)).fetchone()


def resolve(store: Store, spans: list) -> tuple:
    """(statuses, rows): one status per requested span in the order sent (a span sent twice is listed twice),
    and the span rows. Rights never refuse an export (owner ruling): not-cleared and held spans are 'ready'
    and carry a flag and a note; publish and air gating is elsewhere and unchanged."""
    rows = {}
    statuses, ready = [], []
    for sid in spans:
        row = rows.get(sid) or rows.setdefault(sid, _lookup(store, sid))
        status = {"id": sid, "status": "ready"}
        if not row["cleared"]:
            status["notCleared"] = True
        if row["legal_hold"]:
            status["legalHold"] = True
        if not row["cleared"] and row["legal_hold"]:
            status["note"] = BOTH_NOTE
        elif not row["cleared"]:
            status["note"] = NOT_CLEARED_NOTE
        elif row["legal_hold"]:
            status["note"] = HOLD_NOTE
        statuses.append(status)
        ready.append(row)
    return statuses, ready


def _source(store, asset_id, media, pairs, cache) -> tuple:
    """(Source, Facts) of an asset's chosen essence, refusing what cannot be exported."""
    key = (asset_id, media)
    if key in cache:
        return cache[key]
    try:
        f = shots.facts(store, asset_id)
    except shots.ShotError as exc:
        raise NleError(422, "bad_input", exc.reason) from None
    hi = _essence(store, asset_id, "hi-res")
    chosen = hi
    if media == "proxy":
        chosen = _essence(store, asset_id, "proxy")
        if chosen is None:
            raise NleError(409, "no_proxy", f"asset {asset_id} has no proxy")
        # The proxy is assumed to share the hi-res timebase (spec); where it records otherwise, refuse.
        if chosen["frame_rate"] and timecode.parse_rate(chosen["frame_rate"]) != f.rate:
            raise NleError(409, "proxy_mismatch", f"the proxy of asset {asset_id} records a different frame rate")
        if chosen["timecode_start"] and timecode.tc_to_frames(
                chosen["timecode_start"], f.rate, timecode.is_drop(chosen["timecode_start"])) != f.start:
            raise NleError(409, "proxy_mismatch", f"the proxy of asset {asset_id} starts at a different timecode")
    if chosen["open"]:
        raise NleError(409, "essence_open", f"the {media} of asset {asset_id} is still being written: wait for "
                                            "wrap" + (" or export the proxy" if media == "hi-res" else ""))
    path = map_path(chosen["location"], pairs)
    file_url(path)                                  # not_local is decided here, before any file is built
    channels = parse_layout(chosen["audio_layout"]) or parse_layout(hi["audio_layout"])
    duration = chosen["duration_frames"] if chosen["duration_frames"] is not None else f.duration
    src = Source(f"{asset_id}:{media}", asset_id, path, f.rate, f.drop, f.start,
                 None if duration is None else int(duration), channels, location=chosen["location"])
    cache[key] = (src, f)
    return cache[key]


def item_name(row) -> str:
    label = clean(row["label"], 200)
    if label:
        return label
    return clean(f"{row['camera'] or row['asset_id'][:8]} {row['tc_in']}", 200)


def _paths(cache: dict) -> list:
    """Where each exported essence is: as stored and as written to the file. Lets the page say whether the path
    map did anything (an EDL carries no paths at all, but the list is the same)."""
    seen, out = set(), []
    for src, _f in cache.values():
        if src.key in seen:
            continue
        seen.add(src.key)
        out.append({"asset": src.asset_id, "location": src.location, "path": src.path, "mapped": src.path != src.location})
    return out


def export(store: Store, body: dict, path_map: Optional[str] = None) -> dict:
    """The whole of POST /api/exports/nle. Returns the response body; raises NleError for every refusal."""
    req = parse_request(body)
    pairs = parse_path_map(path_map)
    statuses, ready = resolve(store, req["spans"])
    warnings = []
    uncleared = sum(1 for r in ready if not r["cleared"])
    if uncleared:
        warnings.append(f"{uncleared} span(s) are not cleared: " + NOT_CLEARED_NOTE.split(": ", 1)[1])
    held = sum(1 for r in ready if r["legal_hold"])
    if held:
        warnings.append(f"{held} span(s) are on legal hold: " + HOLD_NOTE.split(": ", 1)[1])

    cache, items = {}, []
    for row in ready:
        src, f = _source(store, row["asset_id"], req["media"], pairs, cache)
        fi = row["frame_in"] if row["frame_in"] is not None else f.frames(row["tc_in"])
        fo = row["frame_out"] if row["frame_out"] is not None else f.frames(row["tc_out"])
        if fi < f.start or (f.duration is not None and fo > f.start + f.duration) or fo <= fi:
            raise NleError(409, "outside_source", f"span {row['id']} lies outside its essence")
        items.append(Item(row["id"], src, fi, fo, item_name(row),
                          f"asset {row['asset_id']} span {row['id']}", clean(row["text"], 500)))
    rates = {i.source.rate for i in items}
    if len(rates) > 1:
        raise NleError(422, "mixed_rates", "the selection mixes frame rates: " + ", ".join(sorted(map(str, rates))),
                       rates=sorted(map(str, rates)))
    built = build(req["format"], items, req["name"], req["start_tc"])
    warnings += built.warnings
    resp = {"ok": True, "format": req["format"], "spans": statuses, "warnings": warnings,
            "durationFrames": built.duration_frames, "rate": str(built.rate), "startTc": built.start_tc,
            "dropFrame": built.drop, "filename": filename(req["name"], built.extension),
            "pathMapSet": bool(pairs), "paths": _paths(cache)}
    if req["dry_run"]:
        resp.update(dryRun=True, preview=built.content.splitlines()[:PREVIEW_LINES])
        return resp
    sha = hashlib.sha256(built.content.encode("utf-8")).hexdigest()
    LOG(f"nle export {req['format']} spans={','.join(req['spans'])} sha256={sha}")
    resp.update(mime=built.mime, content=built.content, sha256=sha)
    return resp
