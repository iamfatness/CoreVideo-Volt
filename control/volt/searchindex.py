"""Faceted search over transcript, metadata and AI labels, from one derived table (`search_doc`).

`search_doc` is never authoritative: it is built from `span` (transcript passages), `asset_meta_value` (one `meta` row per asset)
and, later, the label table, and `rebuild_asset` / `rebuild_all` make it again from those sources at any time. SQLite answers text
with an FTS5 external-content table kept in step by triggers (migration 23); Postgres with a `tsvector` column and a GIN index.
Text is never handed to either engine raw: `parse_text` turns it into folded words, phrases and negations, and the compiler
quotes each one. Without FTS5 on SQLite the same terms are matched with LIKE and the answer says `degraded`.

Design: docs/superpowers/specs/2026-11-01-search-indexing-design.md.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import sqlite3
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from volt import metadata, shots, timecode
from volt.operator_state import _blocked_by
from volt.store import Store, _id, _now

LABEL_KINDS = ("face", "text", "object", "scene")
DOC_KINDS = ("transcript", "meta") + LABEL_KINDS
SCOPES = {"all": DOC_KINDS, "transcript": ("transcript",), "labels": LABEL_KINDS, "metadata": ("meta",)}
SORTS = ("relevance", "recent", "show")
GROUPS = ("asset", "hit")

MAX_Q = 500
MAX_WORDS = 20
MAX_WORD = 64
MAX_CLAUSES = 20
MAX_FACETS = 10
FACET_VALUES = 20
DEFAULT_LIMIT, MAX_LIMIT, DEGRADED_LIMIT = 25, 100, 25
CANDIDATE_CAP = 20000          # distinct assets a text query will rank
HIT_CAP = 5000                 # documents a group=hit query will rank
HITS_PER_ASSET = 3
FTS_DOC_CAP = 200000          # best-ranked documents SQLite hands back for one word
CHUNK = 400                    # ids per IN (...) list

PASSAGE_GAP = 1.5              # seconds of silence that end a passage
PASSAGE_WORDS = 25
PASSAGE_SECONDS = 12.0

WEIGHT = {"meta": 1.5, "transcript": 1.0}
BUILTIN_TEXT = {"show": "a.show_name", "room": "a.room", "camera": "a.camera", "owner": "a.owner", "retention": "a.retention_class"}
BUILTIN_BOOL = {"cleared": "a.cleared", "legalHold": "a.legal_hold"}
_META_KEY = re.compile(r"^meta\.([a-z][a-z0-9_]{0,39})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SearchError(Exception):
    """A refusal. `code` is stable (bad_input), `reason` is for people."""

    def __init__(self, code: str, reason: str, status: int = 422):
        super().__init__(reason)
        self.code, self.reason, self.status = code, reason, status


def _bad(reason: str) -> SearchError:
    return SearchError("bad_input", reason)


# ---- folding and the text parser -----------------------------------------------------------------------

def fold(text: str) -> str:
    """Lower case with accents removed: what both engines index and what every query term is reduced to."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").lower()


def norm_text(text: str) -> str:
    """Folded words separated by single spaces: the text Postgres tokenises, so both engines see the same words."""
    return re.sub(r"[\W_]+", " ", fold(text)).strip()


def norm_value(text: str) -> str:
    """A label value for equality and facets: NFKC, casefolded."""
    return unicodedata.normalize("NFKC", text).casefold().strip()


@dataclass
class Terms:
    terms: list = field(default_factory=list)       # words that must all appear in one document
    phrases: list = field(default_factory=list)     # lists of words that must appear next to each other, in order
    negations: list = field(default_factory=list)   # words that must not appear
    prefix: bool = False                            # the last of `terms` is a prefix (the user is still typing it)

    def empty(self) -> bool:
        return not (self.terms or self.phrases or self.negations)


_TOKEN = re.compile(r'(-?)"([^"]*)"?|(-?)(\S+)')


def parse_text(q) -> Terms:
    """Words, "quoted phrases" and -negations from what a person typed. Empty or blank text is no text query; anything that
    leaves no word to look for is bad_input. Punctuation separates words; nothing typed is ever passed on as syntax."""
    if q is None:
        return Terms()
    if not isinstance(q, str) or len(q) > MAX_Q:
        raise _bad(f"q must be text of at most {MAX_Q} characters")
    if not q.strip():
        return Terms()
    out = Terms()
    last_bare = None
    tokens = list(_TOKEN.finditer(q))
    for m in tokens:
        quoted = m.group(2) is not None
        negated = bool(m.group(1) if quoted else m.group(3))
        words = norm_text(m.group(2) if quoted else m.group(4)).split()
        if not words:
            continue
        if quoted and negated:
            raise _bad("a quoted phrase cannot be excluded; exclude single words with -word")
        if negated:
            out.negations.extend(words)
        elif quoted:
            out.phrases.append(words)
        else:
            out.terms.extend(words)
            last_bare = m
    if any(len(w) > MAX_WORD for w in out.terms + out.negations + [w for p in out.phrases for w in p]):
        raise _bad(f"a word is over {MAX_WORD} characters")
    if len(out.terms) + len(out.negations) + sum(len(p) for p in out.phrases) > MAX_WORDS:
        raise _bad(f"a search takes at most {MAX_WORDS} words")
    if not out.terms and not out.phrases:
        raise _bad("a search needs at least one word to look for")
    out.prefix = bool(out.terms) and last_bare is not None and tokens[-1] is last_bare and not q[-1:].isspace()
    return out


def _fts_expression(t: Terms) -> str:
    parts = [f'"{w}"' for w in t.terms]
    if t.prefix and parts:
        parts[-1] += "*"
    parts += ['"' + " ".join(p) + '"' for p in t.phrases]
    expr = " AND ".join(parts)
    if t.negations:
        expr = f"({expr})" + "".join(f' NOT "{w}"' for w in t.negations)
    return expr


def _tsquery(t: Terms) -> str:
    parts = [f"'{w}'" for w in t.terms]
    if t.prefix and parts:
        parts[-1] += ":*"
    parts += ["(" + " <-> ".join(f"'{w}'" for w in p) + ")" for p in t.phrases]
    parts += [f"!'{w}'" for w in t.negations]
    return " & ".join(parts)


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def pg_config() -> str:
    given = os.environ.get("VOLT_SEARCH_CONFIG") or "simple"
    return given if re.fullmatch(r"[a-z_]{1,63}", given) else "simple"


# ---- the filter grammar --------------------------------------------------------------------------------

@dataclass
class Clause:
    field: str
    op: str
    value: object = None
    lo: object = None
    hi: object = None
    min_confidence: Optional[float] = None
    status: Optional[str] = None
    ftype: Optional[str] = None       # meta clauses: the field's type once bound to a store
    field_id: Optional[str] = None


@dataclass
class Filter:
    clauses: list = field(default_factory=list)

    def people(self) -> list:
        ids = []
        for c in self.clauses:
            if c.field == "label.person" and c.op in ("eq", "in"):
                for v in (c.value if isinstance(c.value, list) else [c.value]):
                    if v not in ids:
                        ids.append(v)
        return ids


def _str_value(v, what):
    if not isinstance(v, str) or not v.strip() or len(v) > 200 or "\x00" in v:
        raise _bad(f"{what}: a value must be 1 to 200 characters of text")
    return v


def _list_value(v, what, one):
    if not isinstance(v, list) or not v or len(v) > 50:
        raise _bad(f"{what}: 'in' takes a list of 1 to 50 values")
    return [one(x, what) for x in v]


def _date_value(v, what):
    if not isinstance(v, str) or not _DATE.fullmatch(v):
        raise _bad(f"{what}: dates are written YYYY-MM-DD")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise _bad(f"{what}: {v} is not a real date") from None
    return v


def _num_value(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > metadata.MAX_NUMBER:
        raise _bad(f"{what}: a number is required")
    return float(v)


def _bool_value(v, what):
    if not isinstance(v, bool):
        raise _bad(f"{what}: true or false is required")
    return v


def _bind_meta(store: Store, clause: Clause, what: str) -> None:
    key = clause.field[5:]
    row = store.conn.execute("SELECT id, type FROM field_def WHERE key = ? AND retired_at IS NULL", (key,)).fetchone()
    if row is None:
        raise _bad(f"{what}: no such field {clause.field}")
    clause.ftype, clause.field_id = row["type"], row["id"]
    t, op = row["type"], clause.op
    if op == "range" and t not in ("number", "date"):
        raise _bad(f"{what}: range needs a number or date field")
    coerce = {"number": _num_value, "date": _date_value, "bool": _bool_value}.get(t, _str_value)
    if op in ("eq", "ne"):
        clause.value = coerce(clause.value, what)
    elif op == "in":
        if t == "bool":
            raise _bad(f"{what}: 'in' is not for true/false fields")
        clause.value = _list_value(clause.value, what, coerce)
    elif op == "range":
        clause.lo = None if clause.lo is None else coerce(clause.lo, what)
        clause.hi = None if clause.hi is None else coerce(clause.hi, what)


def parse_filter(obj, store: Optional[Store] = None) -> Filter:
    """The validated clause list of {"all": [clause, ...]}. Unknown fields, ops and keys are refused, never ignored. With a
    store, meta.<key> clauses are bound to their field (and refused when it is unknown or retired)."""
    if obj is None:
        return Filter()
    if not isinstance(obj, dict) or set(obj) != {"all"} or not isinstance(obj["all"], list):
        raise _bad('filter must look like {"all": [clause, ...]}')
    if len(obj["all"]) > MAX_CLAUSES:
        raise _bad(f"a filter takes at most {MAX_CLAUSES} clauses")
    out = Filter()
    for i, raw in enumerate(obj["all"]):
        what = f"clause {i + 1}"
        if not isinstance(raw, dict) or not isinstance(raw.get("field"), str) or not isinstance(raw.get("op"), str):
            raise _bad(f"{what}: needs a field and an op")
        name, op = raw["field"], raw["op"]
        what = f"clause {i + 1} ({name} {op})"
        label = name.startswith("label.") and name[6:] in LABEL_KINDS + ("person",)
        meta = bool(_META_KEY.fullmatch(name))
        if not (name in BUILTIN_TEXT or name in BUILTIN_BOOL or name in ("created", "incomplete") or label or meta):
            raise _bad(f"{what}: unknown field")
        allowed_keys = {"field", "op", "value", "from", "to"} | ({"min_confidence", "status"} if label else set())
        extra = sorted(set(raw) - allowed_keys)
        if extra:
            raise _bad(f"{what}: unknown key(s) {', '.join(extra)}")
        c = Clause(name, op, raw.get("value"), raw.get("from"), raw.get("to"))
        if name in BUILTIN_TEXT:
            ok_ops = ("eq", "ne", "in", "exists", "missing")
        elif name in BUILTIN_BOOL or name == "incomplete":
            ok_ops = ("eq", "ne")
        elif name == "created":
            ok_ops = ("range",)
        elif label:
            ok_ops = ("eq", "in", "exists", "missing")
        else:
            ok_ops = ("eq", "ne", "in", "range", "exists", "missing")
        if op not in ok_ops:
            raise _bad(f"{what}: op must be one of {', '.join(ok_ops)}")
        if op in ("eq", "ne") and "value" not in raw:
            raise _bad(f"{what}: value is required")
        if (op in ("eq", "ne", "in")) is False and "value" in raw:
            raise _bad(f"{what}: this op takes no value")
        if op != "range" and ("from" in raw or "to" in raw):
            raise _bad(f"{what}: from/to are for range")
        if name in BUILTIN_TEXT or (label and op in ("eq", "in")):
            if op in ("eq", "ne"):
                c.value = _str_value(c.value, what)
            elif op == "in":
                c.value = _list_value(c.value, what, _str_value)
        elif name in BUILTIN_BOOL or name == "incomplete":
            c.value = _bool_value(c.value, what)
        if op == "range":
            if raw.get("from") is None and raw.get("to") is None:
                raise _bad(f"{what}: range needs from, to or both")
            if name == "created":
                c.lo = None if c.lo is None else _date_value(c.lo, what)
                c.hi = None if c.hi is None else _date_value(c.hi, what)
        if label:
            if "min_confidence" in raw:
                m = raw["min_confidence"]
                if isinstance(m, bool) or not isinstance(m, (int, float)) or not 0 <= m <= 1:
                    raise _bad(f"{what}: min_confidence is a number from 0 to 1")
                c.min_confidence = float(m)
            if "status" in raw:
                if raw["status"] not in ("confirmed", "proposed"):
                    raise _bad(f"{what}: status is confirmed or proposed")
                c.status = raw["status"]
        if meta and store is not None:
            _bind_meta(store, c, what)
        elif meta and op == "in" and not isinstance(c.value, list):
            raise _bad(f"{what}: 'in' takes a list")
        out.clauses.append(c)
    return out


# ---- cursors ---------------------------------------------------------------------------------------------

def encode_cursor(key, id_) -> str:
    return base64.urlsafe_b64encode(json.dumps(["v1", key, id_]).encode()).decode().rstrip("=")


def decode_cursor(raw):
    try:
        tag, key, id_ = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if tag != "v1" or not isinstance(key, list):
            raise ValueError
        return key, id_
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise _bad("after is not a cursor this server issued") from None


# ---- passages --------------------------------------------------------------------------------------------

def _seconds(tc: str, rate) -> float:
    if rate is not None:
        try:
            return float(timecode.tc_to_seconds(tc, rate))
        except Exception:  # noqa: BLE001 - a label the rate cannot read falls back to the clock part below
            pass
    m = re.fullmatch(r"(\d+):(\d+):(\d+)[:;](\d+)", str(tc).strip())
    if not m:
        return 0.0
    h, mi, s, f = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + f / 30.0


def passages(words, rate=None) -> list:
    """Consecutive word spans (dicts with id, text, tc_in, tc_out, frame_in, frame_out) grouped into passages, cut at a gap over
    1.5 s, at 25 words, or when the passage would run past 12 s."""
    out, cur = [], []

    def close():
        if cur:
            out.append({"first_id": cur[0]["id"], "last_id": cur[-1]["id"], "text": " ".join(w["text"] for w in cur),
                        "tc_in": cur[0]["tc_in"], "tc_out": cur[-1]["tc_out"],
                        "frame_in": cur[0].get("frame_in"), "frame_out": cur[-1].get("frame_out")})
            cur.clear()

    last_end = first_start = 0.0
    for w in words:
        start, end = _seconds(w["tc_in"], rate), _seconds(w["tc_out"], rate)
        if cur and (start - last_end > PASSAGE_GAP or len(cur) >= PASSAGE_WORDS or end - first_start > PASSAGE_SECONDS):
            close()
        if not cur:
            first_start = start
        cur.append(w)
        last_end = max(end, start)
    close()
    return out


# ---- the table -------------------------------------------------------------------------------------------

def sqlite_has_fts5() -> bool:
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False


def fts_available(store: Store) -> bool:
    """True when text queries use the real full-text index: always on Postgres, on SQLite when migration 23 made search_fts."""
    if store.postgres:
        return True
    return store.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_fts'").fetchone() is not None


def default_weight(kind: str, status: Optional[str], confidence: Optional[float]) -> float:
    if kind in WEIGHT:
        return WEIGHT[kind]
    if status == "confirmed":
        return 1.2
    return 0.8 * (1.0 if confidence is None else float(confidence))


def put_doc(store: Store, asset_id: str, kind: str, ref_id, body: str, *, status=None, person_id=None, confidence=None,
            tc_in=None, tc_out=None, frame_in=None, frame_out=None, weight=None) -> str:
    """Insert one search_doc row (the caller owns the transaction). Label rows carry their review status, person and confidence."""
    if kind not in DOC_KINDS:
        raise ValueError(f"unknown search_doc kind {kind!r}")
    body = body.replace("\x00", "")
    if not body.strip():
        raise ValueError("a search_doc body cannot be empty")
    doc_id = _id()
    w = default_weight(kind, status, confidence) if weight is None else weight
    value_norm = norm_value(body) if kind in LABEL_KINDS else None
    cols = ("id, asset_id, kind, ref_id, tc_in, tc_out, frame_in, frame_out, body, weight, status, person_id, confidence, "
            "value_norm, created_at")
    args = [doc_id, asset_id, kind, ref_id, tc_in, tc_out, frame_in, frame_out, body, w, status, person_id, confidence,
            value_norm, _now()]
    if store.postgres:
        store.conn.execute(f"INSERT INTO search_doc ({cols}, tsv) VALUES ({', '.join('?' * 15)}, to_tsvector(?::regconfig, ?))",
                           (*args, pg_config(), norm_text(body)))
    else:
        store.conn.execute(f"INSERT INTO search_doc ({cols}) VALUES ({', '.join('?' * 15)})", args)
    return doc_id


def rebuild_meta(store: Store, asset_id: str) -> int:
    """Remake the asset's one `meta` row from its text, longtext and enum values (active fields that apply to it)."""
    with store.transaction(f"search:{asset_id}"):
        asset = store.get_asset(asset_id)
        store.conn.execute("DELETE FROM search_doc WHERE asset_id = ? AND kind = 'meta'", (asset_id,))
        rows = store.conn.execute(
            """SELECT v.v_text FROM asset_meta_value v JOIN field_def f ON f.id = v.field_id
               WHERE v.asset_id = ? AND f.retired_at IS NULL AND f.type IN ('text', 'longtext', 'enum')
                 AND (f.show_name IS NULL OR f.show_name = ?) AND v.v_text IS NOT NULL
               ORDER BY f.position, f.key, v.ord""", (asset_id, asset.show_name)).fetchall()
        body = "\n".join(r["v_text"] for r in rows)
        if not body.strip():
            return 0
        put_doc(store, asset_id, "meta", None, body)
        return 1


def rebuild_asset(store: Store, asset_id: str) -> dict:
    """Remake the asset's transcript passages and metadata row from their sources. Idempotent. Label rows are written by
    the label code when a label changes, not here."""
    with store.transaction(f"search:{asset_id}"):
        store.get_asset(asset_id)
        store.conn.execute("DELETE FROM search_doc WHERE asset_id = ? AND kind = 'transcript'", (asset_id,))
        words = [dict(id=r["id"], text=r["text"], tc_in=r["tc_in"], tc_out=r["tc_out"], frame_in=r["frame_in"], frame_out=r["frame_out"])
                 for r in store.conn.execute(
                     "SELECT id, text, tc_in, tc_out, frame_in, frame_out FROM span "
                     "WHERE asset_id = ? AND kind = 'transcript' AND text IS NOT NULL ORDER BY tc_in, id", (asset_id,)).fetchall()
                 if r["text"] and r["text"].strip()]
        f = shots.try_facts(store, asset_id)
        found = passages(words, f.rate if f is not None else None)
        for p in found:
            put_doc(store, asset_id, "transcript", p["first_id"], p["text"], tc_in=p["tc_in"], tc_out=p["tc_out"],
                    frame_in=p["frame_in"], frame_out=p["frame_out"])
        return {"passages": len(found), "meta": rebuild_meta(store, asset_id)}


def rebuild_all(store: Store) -> int:
    """Remake every asset's rows from their sources. Returns how many assets were rebuilt."""
    ids = [r["id"] for r in store.conn.execute("SELECT id FROM asset ORDER BY id").fetchall()]
    for asset_id in ids:
        rebuild_asset(store, asset_id)
    return len(ids)


# ---- query building ------------------------------------------------------------------------------------

def _marks(n: int) -> str:
    return ", ".join("?" * n)


def _in(values) -> str:
    return "(" + ", ".join("'" + v + "'" for v in values) + ")"      # constants only: kinds and statuses defined in this module


def label_visible(alias: str, include_proposed: bool) -> str:
    """A label row may be shown: never a rejected one, a face only once confirmed (or when asked for unreviewed faces)."""
    faces = ("confirmed", "proposed") if include_proposed else ("confirmed",)
    return (f"(({alias}.kind = 'face' AND {alias}.status IN {_in(faces)}) OR "
            f"({alias}.kind IN ('text', 'object', 'scene') AND {alias}.status IN ('proposed', 'confirmed')))")


def _doc_visible(alias: str, include_proposed: bool) -> str:
    return f"({alias}.kind IN ('transcript', 'meta') OR {label_visible(alias, include_proposed)})"


def _ts(store: Store) -> str:
    return "?::timestamptz" if store.postgres else "?"


def _label_exists(c: Clause, include: bool):
    kind = "face" if c.field == "label.person" else c.field[6:]
    where = ["l.asset_id = a.id", "l.kind = ?", label_visible("l", include)]
    args = [kind]
    if c.status:
        where.append("l.status = ?")
        args.append(c.status)
    if c.min_confidence is not None:
        where.append("l.confidence >= ?")
        args.append(c.min_confidence)
    if c.op in ("eq", "in"):
        col = "l.person_id" if c.field == "label.person" else "l.value_norm"
        vals = [c.value] if c.op == "eq" else c.value
        if c.field != "label.person":
            vals = [norm_value(v) for v in vals]
        where.append(f"{col} IN ({_marks(len(vals))})")
        args += vals
    return "EXISTS (SELECT 1 FROM search_doc l WHERE " + " AND ".join(where) + ")", args


def _meta_col(ftype: str) -> str:
    return {"number": "v.v_num", "bool": "v.v_num", "date": "v.v_date"}.get(ftype, "v.v_text")


def _meta_value(ftype: str, v):
    return (1.0 if v else 0.0) if ftype == "bool" else v


def _clause_sql(store: Store, c: Clause, include: bool):
    f, op = c.field, c.op
    if f in BUILTIN_TEXT:
        col = BUILTIN_TEXT[f]
        return {"eq": (f"{col} = ?", [c.value]), "ne": (f"({col} IS NULL OR {col} <> ?)", [c.value]),
                "in": (f"{col} IN ({_marks(len(c.value or []))})", list(c.value or [])),
                "exists": (f"{col} IS NOT NULL", []), "missing": (f"{col} IS NULL", [])}[op]
    if f in BUILTIN_BOOL:
        return (f"{BUILTIN_BOOL[f]} {'=' if op == 'eq' else '<>'} ?", [c.value])
    if f == "incomplete":
        sql = ("EXISTS (SELECT 1 FROM field_def r WHERE r.required = TRUE AND r.retired_at IS NULL "
               "AND (r.show_name IS NULL OR r.show_name = a.show_name) "
               "AND NOT EXISTS (SELECT 1 FROM asset_meta_value v WHERE v.asset_id = a.id AND v.field_id = r.id))")
        return (sql if (c.value if op == "eq" else not c.value) else "NOT " + sql), []
    if f == "created":
        parts, args = [], []
        if c.lo:
            parts.append(f"a.created_at >= {_ts(store)}")
            args.append(c.lo + "T00:00:00Z")
        if c.hi:
            try:
                upper = (date.fromisoformat(c.hi) + timedelta(days=1)).isoformat()
                parts.append(f"a.created_at < {_ts(store)}")
                args.append(upper + "T00:00:00Z")
            except OverflowError:
                pass
        return (" AND ".join(parts) or "TRUE"), args
    if f.startswith("label."):
        if op in ("exists", "eq", "in"):
            return _label_exists(c, include)
        sql, args = _label_exists(Clause(c.field, "exists", min_confidence=c.min_confidence, status=c.status), include)
        return "NOT " + sql, args
    col = _meta_col(c.ftype)
    base = "EXISTS (SELECT 1 FROM asset_meta_value v WHERE v.asset_id = a.id AND v.field_id = ?"
    if op == "exists":
        return base + ")", [c.field_id]
    if op == "missing":
        return "NOT " + base + ")", [c.field_id]
    if op in ("eq", "ne"):
        sql = f"{base} AND {col} = ?)"
        return (sql if op == "eq" else "NOT " + sql), [c.field_id, _meta_value(c.ftype, c.value)]
    if op == "in":
        return f"{base} AND {col} IN ({_marks(len(c.value))}))", [c.field_id, *[_meta_value(c.ftype, v) for v in c.value]]
    parts, args = [], [c.field_id]
    if c.lo is not None:
        parts.append(f"{col} >= ?")
        args.append(c.lo)
    if c.hi is not None:
        parts.append(f"{col} <= ?")
        args.append(c.hi)
    return f"{base} AND {' AND '.join(parts)})", args


def _asset_where(store: Store, flt: Filter, include: bool):
    """SQL over alias `a` (asset) for the live assets matching every clause."""
    where, args = ["a.deleted_at IS NULL"], []
    for c in flt.clauses:
        sql, a = _clause_sql(store, c, include)
        where.append("(" + sql + ")")
        args += a
    return " AND ".join(where), args


def text_candidates_sql(store: Store, terms: Terms, kinds, include_proposed: bool, asset_where: str, asset_args, aggregate: bool = True,
                        asset_ids: Optional[list] = None):
    """The SQL that finds the documents matching a text query. With `aggregate`, rows are (asset_id, score) with the best score
    per asset; otherwise one row per document (d.*, score). `asset_where` is SQL over alias `a` that picks the assets.
    Returns (sql, args)."""
    kinds = tuple(k for k in kinds if k in DOC_KINDS)
    conds = [f"d.kind IN {_in(kinds)}", _doc_visible("d", include_proposed)]
    if store.postgres:
        frm = "FROM search_doc d CROSS JOIN to_tsquery(?::regconfig, ?) AS q"
        lead = [pg_config(), _tsquery(terms)]
        conds.insert(0, "d.tsv @@ q")
        score = "(ts_rank_cd(d.tsv, q) + 0.000001) * d.weight"
    elif fts_available(store):
        # bm25() only works where the FTS table drives the query. The ORDER BY ... LIMIT keeps SQLite from flattening this
        # subquery into the outer aggregate (which breaks bm25); it also bounds a very common word to its best matches.
        frm = (f"FROM (SELECT rowid AS seq, bm25(search_fts) AS rank FROM search_fts WHERE search_fts MATCH ? "
               f"ORDER BY rank LIMIT {FTS_DOC_CAP}) m "
               "JOIN search_doc d ON d.seq = m.seq")
        lead = []
        lead_match = [_fts_expression(terms)]
        score = "(-m.rank + 0.000001) * d.weight"
    else:
        frm = "FROM search_doc d"
        lead, lead_match = [], []
        like = [f"lower(d.body) LIKE ? ESCAPE '\\'" for _ in terms.terms] + [f"lower(d.body) LIKE ? ESCAPE '\\'" for _ in terms.phrases]
        lead_match = [_like(w) for w in terms.terms] + [_like(" ".join(p)) for p in terms.phrases]
        conds = like + [f"lower(d.body) NOT LIKE ? ESCAPE '\\'" for _ in terms.negations] + conds
        lead_match += [_like(w) for w in terms.negations]
        score = "d.weight"
    if not store.postgres:
        lead = lead + lead_match
    if asset_ids is not None:
        conds.append(f"d.asset_id IN ({_marks(len(asset_ids))})")
        tail = list(asset_ids)
    else:
        conds.append(f"d.asset_id IN (SELECT a.id FROM asset a WHERE {asset_where})")
        tail = list(asset_args)
    inner = f"SELECT d.*, {score} AS score {frm} WHERE {' AND '.join(conds)}"
    if aggregate:
        return f"SELECT asset_id, MAX(score) AS score FROM ({inner}) hit GROUP BY asset_id", lead + tail
    return inner, lead + tail


# ---- the request -----------------------------------------------------------------------------------------

def _csv(value, what) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _bad(f"{what} must be a comma list")
    return [v.strip() for v in value if v.strip()]


@dataclass
class Request:
    terms: Terms
    q: str
    scope: str
    flt: Filter
    filter_obj: object
    facets: list
    sort: str
    group: str
    include: bool
    limit: int
    after: Optional[str]


_FACETS = ("show", "camera", "owner", "retention", "cleared", "created", "label.kind", "label.person")


def _parse_request(store: Store, request: dict) -> Request:
    if not isinstance(request, dict):
        raise _bad("the search must be an object")
    extra = sorted(set(request) - {"q", "scope", "filter", "facets", "sort", "group", "include", "limit", "after"})
    if extra:
        raise _bad(f"unknown parameter(s): {', '.join(extra)}")
    q = request.get("q")
    terms = parse_text(q)
    has_text = bool(terms.terms or terms.phrases)
    scope = request.get("scope", "all")
    if scope not in SCOPES:
        raise _bad(f"scope must be one of {', '.join(SCOPES)}")
    sort = request.get("sort") or ("relevance" if has_text else "recent")
    if sort not in SORTS:
        raise _bad(f"sort must be one of {', '.join(SORTS)}")
    if sort == "relevance" and not has_text:
        raise _bad("sort=relevance needs q")
    group = request.get("group", "asset")
    if group not in GROUPS:
        raise _bad("group must be asset or hit")
    if group == "hit" and not has_text:
        raise _bad("group=hit needs q")
    include = _csv(request.get("include"), "include")
    if any(i != "proposed" for i in include):
        raise _bad("include may only be: proposed")
    limit = request.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise _bad(f"limit must be a whole number from 1 to {MAX_LIMIT}")
    after = request.get("after")
    if after is not None and not isinstance(after, str):
        raise _bad("after is not a cursor this server issued")
    facets = _csv(request.get("facets"), "facets")
    if len(facets) > MAX_FACETS:
        raise _bad(f"at most {MAX_FACETS} facets")
    for name in facets:
        m = _META_KEY.fullmatch(name)
        if name not in _FACETS and not m:
            raise _bad(f"unknown facet {name}")
        if m and store.conn.execute("SELECT 1 FROM field_def WHERE key = ? AND retired_at IS NULL", (m.group(1),)).fetchone() is None:
            raise _bad(f"unknown facet {name}")
    flt_obj = request.get("filter")
    return Request(terms, q or "", scope, parse_filter(flt_obj, store), flt_obj, facets, sort, group, bool(include), limit, after)


def validate_request(store: Store, request: dict) -> None:
    """Raise SearchError when the request would be refused. Used to vet a saved search before it is stored."""
    _parse_request(store, request)


def _signature(r: Request) -> str:
    blob = json.dumps([r.q, r.scope, r.filter_obj, r.sort, r.group, r.include], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _inv(s) -> list:
    return [-ord(ch) for ch in (s or "")]


def _asset_rank(r: Request, row, score: float) -> list:
    created, id_ = row["created_at"], row["id"]
    if r.sort == "recent":
        return [_inv(created), id_]
    if r.sort == "show":
        show = row["show_name"]
        return [1 if show is None else 0, fold(show or ""), _inv(created), id_]
    return [-round(score, 9), _inv(created), id_]


def _fetch_assets(store: Store, ids: list) -> dict:
    out = {}
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        for row in store.conn.execute(f"SELECT a.* FROM asset a WHERE a.id IN ({_marks(len(chunk))})", tuple(chunk)).fetchall():
            out[row["id"]] = row
    return out


def _hit_json(row, score: float) -> dict:
    out = {"kind": row["kind"], "tcIn": row["tc_in"], "tcOut": row["tc_out"], "frameIn": row["frame_in"], "frameOut": row["frame_out"],
           "snippet": row["body"][:240], "status": row["status"], "confidence": row["confidence"], "score": round(score, 6)}
    if row["kind"] == "transcript":
        out["spanId"] = row["ref_id"]
    elif row["kind"] in LABEL_KINDS:
        out["labelId"] = row["ref_id"]
    return out


def _chunks(ids: list):
    for i in range(0, len(ids), CHUNK):
        yield ids[i:i + CHUNK]


def _facets(store: Store, r: Request, ids: list) -> dict:
    out = {}
    for name in r.facets:
        counts: dict = {}
        for chunk in _chunks(ids):
            marks, args = _marks(len(chunk)), tuple(chunk)
            if name in ("show", "camera", "owner", "retention", "cleared"):
                col = {"show": "show_name", "retention": "retention_class"}.get(name, name)
                rows = store.conn.execute(
                    f"SELECT {col} AS v, COUNT(*) AS n FROM asset WHERE id IN ({marks}) AND {col} IS NOT NULL GROUP BY {col}", args).fetchall()
                rows = [(bool(x["v"]) if name == "cleared" else x["v"], x["n"]) for x in rows]
            elif name == "created":
                month = "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM')" if store.postgres else "substr(created_at, 1, 7)"
                rows = [(x["v"], x["n"]) for x in store.conn.execute(
                    f"SELECT {month} AS v, COUNT(*) AS n FROM asset WHERE id IN ({marks}) GROUP BY {month}", args).fetchall()]
            elif name == "label.kind":
                rows = [(x["v"], x["n"]) for x in store.conn.execute(
                    f"SELECT l.kind AS v, COUNT(DISTINCT l.asset_id) AS n FROM search_doc l WHERE l.asset_id IN ({marks}) "
                    f"AND l.kind IN {_in(LABEL_KINDS)} AND {label_visible('l', r.include)} GROUP BY l.kind", args).fetchall()]
            elif name == "label.person":
                rows = [(x["v"], x["n"]) for x in store.conn.execute(
                    f"SELECT l.person_id AS v, COUNT(DISTINCT l.asset_id) AS n FROM search_doc l WHERE l.asset_id IN ({marks}) "
                    f"AND l.kind = 'face' AND l.person_id IS NOT NULL AND {label_visible('l', r.include)} GROUP BY l.person_id", args).fetchall()]
            else:
                key = name[5:]
                f = store.conn.execute("SELECT id, type FROM field_def WHERE key = ?", (key,)).fetchone()
                col = _meta_col(f["type"])
                rows = []
                for x in store.conn.execute(
                        f"SELECT {col} AS v, COUNT(DISTINCT v.asset_id) AS n FROM asset_meta_value v WHERE v.field_id = ? "
                        f"AND v.asset_id IN ({marks}) GROUP BY {col}", (f["id"], *chunk)).fetchall():
                    val = x["v"]
                    if f["type"] == "bool":
                        val = bool(val)
                    elif f["type"] == "number" and float(val).is_integer():
                        val = int(val)
                    rows.append((val, x["n"]))
            for value, n in rows:
                counts[value] = counts.get(value, 0) + n
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))
        out[name] = {"values": [{"value": v, "count": n} for v, n in ordered[:FACET_VALUES]],
                     "other": sum(n for _v, n in ordered[FACET_VALUES:])}
    return out


def search(store: Store, request: dict, principal=None) -> dict:
    """Run one search. Returns {assets|hits, total, facets, next, degraded}. See the module comment and the design for the shape.

    A filter that names a person writes one `search_person` audit row per person id (the one read that writes), with no name."""
    r = _parse_request(store, request)
    has_text = bool(r.terms.terms or r.terms.phrases)
    degraded = has_text and not fts_available(store)
    if degraded and r.limit > DEGRADED_LIMIT:
        raise _bad(f"text search is running without the full-text index; limit may be at most {DEGRADED_LIMIT}")
    if principal is not None:
        for person_id in r.flt.people():
            store.append_audit(principal.name, "search_person", "person", person_id)
    sig = _signature(r)
    where, where_args = _asset_where(store, r.flt, r.include)
    kinds = SCOPES[r.scope]
    scores: dict = {}
    if has_text:
        sql, args = text_candidates_sql(store, r.terms, kinds, r.include, where, where_args)
        rows = store.conn.execute(sql + f" ORDER BY score DESC LIMIT {CANDIDATE_CAP}", tuple(args)).fetchall()
        scores = {x["asset_id"]: float(x["score"]) for x in rows}
        assets = _fetch_assets(store, list(scores))
    else:
        rows = store.conn.execute(f"SELECT a.* FROM asset a WHERE {where}", tuple(where_args)).fetchall()
        assets = {x["id"]: x for x in rows}
    top = max(scores.values()) if scores else 1.0
    ids = list(assets)
    facets = _facets(store, r, ids) if r.facets else {}
    after_key = after_id = None
    if r.after:
        after_key, after_id = decode_cursor(r.after)
        if len(after_key) != 3 or after_key[0] != sig or after_key[1] != r.sort + "/" + r.group:
            raise _bad("after belongs to a different search")

    if r.group == "hit":
        sql, args = text_candidates_sql(store, r.terms, kinds, r.include, where, where_args, aggregate=False)
        docs = store.conn.execute(sql + f" ORDER BY score DESC LIMIT {HIT_CAP}", tuple(args)).fetchall()
        need = _fetch_assets(store, [d for d in {x["asset_id"] for x in docs} if d not in assets])
        assets = {**assets, **need}
        ranked = sorted(([[-round(float(d["score"]), 9), _inv(assets[d["asset_id"]]["created_at"]), d["seq"]], d] for d in docs),
                        key=lambda p: p[0])
        keys = [p[0] for p in ranked]
        start = bisect_right(keys, after_key[2]) if after_key else 0
        page = ranked[start:start + r.limit]
        more = start + r.limit < len(ranked)
        hits = []
        for _k, d in page:
            h = _hit_json(d, float(d["score"]) / top)
            hits.append({"assetId": d["asset_id"], **h})
        nxt = encode_cursor([sig, r.sort + "/" + r.group, page[-1][0]], page[-1][1]["id"]) if more and page else None
        return {"hits": hits, "total": len(ranked), "facets": facets, "next": nxt, "degraded": degraded}

    ranked = sorted(([_asset_rank(r, assets[i], scores.get(i, 0.0)), i] for i in ids), key=lambda p: p[0])
    keys = [p[0] for p in ranked]
    start = bisect_right(keys, after_key[2]) if after_key else 0
    page = ranked[start:start + r.limit]
    more = start + r.limit < len(ranked)
    page_ids = [i for _k, i in page]
    cold = set()
    if page_ids:
        cold = {x["asset_id"] for x in store.conn.execute(
            f"SELECT asset_id FROM essence WHERE role = 'hi-res' AND tier IN ('archived', 'restoring') AND asset_id IN ({_marks(len(page_ids))})",
            tuple(page_ids)).fetchall()}
    hits_by: dict = {}
    if has_text and page_ids:
        sql, args = text_candidates_sql(store, r.terms, kinds, r.include, where, where_args, aggregate=False, asset_ids=page_ids)
        for d in store.conn.execute(sql + " ORDER BY score DESC", tuple(args)).fetchall():
            lst = hits_by.setdefault(d["asset_id"], [])
            if len(lst) < HITS_PER_ASSET:
                lst.append(_hit_json(d, float(d["score"]) / top))
    out_assets = []
    for i in page_ids:
        a = assets[i]
        state = metadata.get_values(store, i)
        cleared, hold = bool(a["cleared"]), bool(a["legal_hold"])
        out_assets.append({
            "assetId": i, "show": a["show_name"], "camera": a["camera"], "room": a["room"], "owner": a["owner"],
            "retention": a["retention_class"], "cleared": cleared, "legalHold": hold, "blockedBy": _blocked_by(cleared, hold),
            "archived": i in cold, "createdAt": a["created_at"], "metadata": state["values"], "missing": state["missing"],
            "incomplete": bool(state["missing"]), "score": round(scores.get(i, 0.0) / top, 6) if has_text else None,
            "hits": hits_by.get(i, [])})
    nxt = encode_cursor([sig, r.sort + "/" + r.group, page[-1][0]], page[-1][1]) if more and page else None
    return {"assets": out_assets, "total": len(ranked), "facets": facets, "next": nxt, "degraded": degraded}
