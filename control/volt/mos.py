"""MOS export of the rundown: a show rendered as one MOS roCreate message (docs/specs/mos.md).

Pure rendering (`build_ro_create`) is separate from reading the database (`load_show`, `export`). This module
writes a message; it opens no socket and sends nothing.

CONFORMANCE: the structure follows MOS 2.8.5 roCreate as documented (the readable DTD lines for roCreate, story
and item). It has NOT been validated against the MOS XSD/DTD or any newsroom computer system. Volt's own data
(status, script, cues, timebase, timecode) rides in `mosExternalMetadata` under the `urn:corevideo:volt:mos:1`
namespace, so a receiver that does not know it can ignore it."""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from volt import shots, timecode
from volt.nle import clean as clean_text
from volt.store import Store

NS = "urn:corevideo:volt:mos:1"
DEFAULT_MOS_ID = "volt.mos"
DEFAULT_NCS_ID = "ncs.unset"
MOS_VERSION = "2.8.5"
SCRIPT_LIMIT = 20000
_ID_OK = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BAD_XML = re.compile("[^\u0009\u000a\u000d -퟿-�\U00010000-\U0010ffff]")

ET.register_namespace("volt", NS)


class MosError(Exception):
    """A refusal with an HTTP status; `code` is stable, `reason` is for people."""

    def __init__(self, status: int, code: str, reason: str, **extra):
        super().__init__(reason)
        self.status, self.code, self.reason, self.extra = status, code, reason, extra


@dataclass(frozen=True)
class MediaRef:
    obj_id: str
    slug: str
    rate: Optional[Fraction]          # None when the essence has no usable frame rate
    frames: Optional[int]             # span length in frames at `rate`; None when it cannot be counted
    tc_in: str = ""
    tc_out: str = ""


@dataclass(frozen=True)
class CueRef:
    room: str
    command: str
    payload: str
    status: str


@dataclass(frozen=True)
class Story:
    id: str
    num: int
    slug: str
    status: str
    script: str = ""
    media: Optional[MediaRef] = None
    cues: tuple = ()


@dataclass
class Built:
    content: str
    warnings: list = field(default_factory=list)
    story_count: int = 0
    item_count: int = 0
    total_seconds_exact: Fraction = Fraction(0)

    @property
    def total_seconds(self) -> int:
        return math.floor(self.total_seconds_exact + Fraction(1, 2))     # half up


# ---- small helpers ----------------------------------------------------------------------------------------------

def script_text(text) -> str:
    """Script body: invalid XML characters out, line breaks kept (as \\n)."""
    text = _BAD_XML.sub("", str(text or "")).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()[:SCRIPT_LIMIT]


def hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def timebase_text(rate: Fraction) -> str:
    """objTB as people write it: 25, 29.97, 59.94, 23.976. Three decimals at most, trailing zeros dropped."""
    text = f"{float(rate):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def rate_text(rate: Fraction) -> str:
    return str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"


def _check_id(name: str, value) -> str:
    if not isinstance(value, str) or not _ID_OK.match(value):
        raise MosError(422, "bad_input", f"{name} may use letters, digits and _ . : - only (1 to 128)")
    return value


def _sub(parent, tag, text=None):
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = str(text)
    return el


def _v(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _metadata(parent, scope: str):
    """A mosExternalMetadata block; returns the element Volt's payload goes inside."""
    meta = _sub(parent, "mosExternalMetadata")
    _sub(meta, "mosScope", scope)
    _sub(meta, "mosSchema", NS)
    return _sub(meta, "mosPayload")


# ---- rendering --------------------------------------------------------------------------------------------------

def build_ro_create(show_id: str, title: str, stories: list, mos_id: str = DEFAULT_MOS_ID,
                    ncs_id: str = DEFAULT_NCS_ID, message_id: int = 1, include_cues: bool = False, tag: str = "roCreate") -> Built:
    """One roCreate for a show (`tag="roReplace"` writes the same body as a roReplace). Each rundown item is a story; its attached span is the story's one item."""
    _check_id("mos_id", mos_id)
    _check_id("ncs_id", ncs_id)
    warnings: list = []
    total = Fraction(0)
    items = 0

    root = ET.Element("mos")
    _sub(root, "mosID", mos_id)
    _sub(root, "ncsID", ncs_id)
    _sub(root, "messageID", int(message_id))
    if tag not in ("roCreate", "roReplace"):
        raise ValueError("tag must be roCreate or roReplace")
    ro = _sub(root, tag)
    _sub(ro, "roID", clean_text(show_id, 128))
    _sub(ro, "roSlug", clean_text(title))
    dur_el = _sub(ro, "roEdDur")

    for s in stories:
        st = _sub(ro, "story")
        _sub(st, "storyID", clean_text(s.id, 128))
        _sub(st, "storySlug", clean_text(s.slug))
        _sub(st, "storyNum", s.num)
        payload = _metadata(st, "STORY")
        body = ET.SubElement(payload, _v("story"))
        _sub(body, _v("status"), clean_text(s.status, 32))
        script = script_text(s.script)
        if script:
            _sub(body, _v("script"), script)
        if include_cues and s.cues:
            cues = ET.SubElement(body, _v("cues"))
            for c in s.cues:
                cue = ET.SubElement(cues, _v("cue"))
                _sub(cue, _v("room"), clean_text(c.room, 100))
                _sub(cue, _v("command"), clean_text(c.command, 100))
                _sub(cue, _v("payload"), clean_text(c.payload, 2000))
                _sub(cue, _v("status"), clean_text(c.status, 32))
        m = s.media
        if m is None:
            continue
        items += 1
        known = m.rate is not None and m.frames is not None
        if known:
            total += Fraction(m.frames) / m.rate
        else:
            warnings.append(f"story {s.id}: the media has no frame rate or length; it is listed without a duration "
                            "and is not counted in roEdDur")
        item = _sub(st, "item")
        _sub(item, "itemID", clean_text(f"{s.id}.1", 140))
        _sub(item, "itemSlug", clean_text(m.slug))
        _sub(item, "objID", clean_text(m.obj_id, 128))
        _sub(item, "mosID", mos_id)
        _sub(item, "itemEdStart", 0)
        if known:
            _sub(item, "itemEdDur", m.frames)
        media = ET.SubElement(_metadata(item, "OBJECT"), _v("media"))
        if known:
            _sub(media, _v("objTB"), timebase_text(m.rate))
            _sub(media, _v("objDur"), m.frames)
            _sub(media, _v("rate"), rate_text(m.rate))
        if m.tc_in:
            _sub(media, _v("tcIn"), clean_text(m.tc_in, 32))
        if m.tc_out:
            _sub(media, _v("tcOut"), clean_text(m.tc_out, 32))

    built = Built("", warnings, len(stories), items, total)
    dur_el.text = hms(built.total_seconds)
    ET.indent(root, space="  ")
    built.content = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    return built


# ---- from the database ------------------------------------------------------------------------------------------

def find_show(store: Store, ref: str) -> tuple:
    """(id, title) for a show id, or for a title that matches exactly one show. 404 otherwise."""
    row = store.conn.execute("SELECT id, title FROM show WHERE id = ?", (ref,)).fetchone()
    if row is None:
        rows = store.conn.execute("SELECT id, title FROM show WHERE title = ?", (ref,)).fetchall()
        if len(rows) > 1:
            raise MosError(409, "ambiguous", f"{len(rows)} shows are titled {ref!r}; use the show id")
        row = rows[0] if rows else None
    if row is None:
        raise MosError(404, "not_found", f"no such show: {ref}")
    return row["id"], row["title"]


def _media(store: Store, row, cache: dict) -> MediaRef:
    asset_id = row["asset_id"]
    if asset_id not in cache:
        cache[asset_id] = shots.try_facts(store, asset_id)
    facts = cache[asset_id]
    label = row["label"] or f"{row['camera'] or asset_id[:8]} {row['tc_in']}"
    if facts is None:
        return MediaRef(row["span_id"], label, None, None, row["tc_in"], row["tc_out"])
    frames = None
    try:
        if row["frame_in"] is not None and row["frame_out"] is not None:
            frames = int(row["frame_out"]) - int(row["frame_in"])
        else:
            frames = facts.frames(row["tc_out"]) - facts.frames(row["tc_in"])
    except shots.ShotError:
        frames = None
    if frames is not None and frames <= 0:
        frames = None
    return MediaRef(row["span_id"], label, facts.rate, frames, row["tc_in"], row["tc_out"])


def load_stories(store: Store, show_id: str) -> list:
    from volt.cues import cues as cues_of

    rows = store.conn.execute(
        "SELECT i.id, i.position, i.slug, i.script, i.status, i.span_id, s.asset_id, s.tc_in, s.tc_out, "
        "s.label, s.frame_in, s.frame_out, a.camera "
        "FROM item i LEFT JOIN span s ON s.id = i.span_id LEFT JOIN asset a ON a.id = s.asset_id "
        "WHERE i.show_id = ? ORDER BY i.position, i.id", (show_id,)).fetchall()
    cache: dict = {}
    out = []
    for r in rows:
        media = None
        if r["span_id"] is not None and r["asset_id"] is not None:
            media = _media(store, {**dict(r), "span_id": r["span_id"]}, cache)
        cues = tuple(CueRef(c.room, c.command, c.payload, c.status) for c in cues_of(store, r["id"]))
        out.append(Story(r["id"], r["position"], r["slug"], r["status"], r["script"] or "", media, cues))
    return out


def export(store: Store, show_ref: str, include_cues: bool = False, mos_id: str = DEFAULT_MOS_ID,
           ncs_id: str = DEFAULT_NCS_ID) -> dict:
    """Read-only. The response shape mirrors the NLE export: the file travels in `content`."""
    show_id, title = find_show(store, show_ref)
    built = build_ro_create(show_id, title, load_stories(store, show_id), mos_id, ncs_id, include_cues=include_cues)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", show_id).strip("._") or "show"
    return {"ok": True, "format": "mos-roCreate", "mosVersion": MOS_VERSION, "validated": False,
            "conformance": f"structurally follows MOS {MOS_VERSION} roCreate as documented; unvalidated against "
                           "an NCS or the MOS XSD/DTD",
            "filename": f"{safe}.mos.xml", "mime": "application/xml", "show": {"id": show_id, "title": title},
            "content": built.content, "sha256": hashlib.sha256(built.content.encode("utf-8")).hexdigest(),
            "stories": built.story_count, "items": built.item_count, "durationSeconds": built.total_seconds,
            "includeCues": include_cues, "warnings": built.warnings}


def parse_query(query: dict) -> dict:
    """Keyword arguments for export() from a parsed query string (parse_qs shape). Unknown or repeated -> 422."""
    extra = sorted(set(query) - {"cues", "mos_id", "ncs_id"})
    if extra:
        raise MosError(422, "bad_input", f"unknown parameter(s): {', '.join(extra)}")
    out: dict = {}
    for key, values in query.items():
        if len(values) != 1:
            raise MosError(422, "bad_input", f"{key} may be given once")
        value = values[0]
        if key == "cues":
            if value not in ("0", "1", "true", "false"):
                raise MosError(422, "bad_input", "cues must be 0, 1, true or false")
            out["include_cues"] = value in ("1", "true")
        else:
            out[key] = _check_id(key, value)
    return out
