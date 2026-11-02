"""Typed asset metadata: fields a producer defines, controlled vocabularies, and values validated on the server.

A field is `field_def`; the terms of an enum field are `field_term`; one value is one `asset_meta_value` row with typed
columns (v_text, v_num, v_date), so a range filter or a sort never parses text and a facet is a GROUP BY. A field is never
deleted, only retired. Required means an asset is *incomplete* without it, not that ingest is refused.

Every refusal is a MetaError with a stable `code`; the HTTP layer maps it to ApiError(status, code, reason).
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Optional

from volt.store import Store, _id, _now

FIELD_TYPES = ("text", "longtext", "number", "date", "bool", "enum")
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MAX_ACTIVE_FIELDS = 60
MAX_TERMS = 500
MAX_MULTI_VALUES = 20
MAX_TEXT = 1000
MAX_LONGTEXT = 10000
MAX_NUMBER = 1e15
MAX_LABEL = 100
MAX_TERM = 100
MAX_SHOW = 200

_STATUS = {"field_exists": 409, "type_locked": 409}   # everything else is 422


class MetaError(Exception):
    """A refusal. `code` is stable, `reason` is for people, `status` is the HTTP status the API uses."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason
        self.status = _STATUS.get(code, 422)


def _bad(reason: str) -> MetaError:
    return MetaError("bad_input", reason)


_UNSET = object()


def _clean_text(value, what: str, limit: int, code: str = "bad_input") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise MetaError(code, f"{what} must be 1 to {limit} characters of text")
    return value


def _field_json(row, terms=None) -> dict:
    out = {"key": row["key"], "label": row["label"], "type": row["type"], "showName": row["show_name"],
           "required": bool(row["required"]), "multi": bool(row["multi"]), "position": row["position"],
           "retired": row["retired_at"] is not None}
    if terms is not None:
        out["terms"] = terms
    return out


def _row(store: Store, key, active_only: bool = True):
    row = store.conn.execute("SELECT * FROM field_def WHERE key = ?", (key if isinstance(key, str) else "",)).fetchone()
    if row is None or (active_only and row["retired_at"] is not None):
        raise MetaError("unknown_field", f"no such field: {key if isinstance(key, str) else ''}")
    return row


def _reindex(store: Store, asset_id: str) -> None:
    """Keep the asset's search row in step with its values, in the caller's transaction."""
    from volt import searchindex   # searchindex reads this module's limits, so import it late
    searchindex.rebuild_meta(store, asset_id)


def _has_values(store: Store, field_id: str) -> bool:
    return store.conn.execute("SELECT 1 FROM asset_meta_value WHERE field_id = ? LIMIT 1", (field_id,)).fetchone() is not None


def _check_multi(type_: str, multi) -> None:
    if not isinstance(multi, bool):
        raise _bad("multi must be true or false")
    if multi and type_ not in ("enum", "text"):
        raise _bad("only text and enum fields can hold several values")


# ---- schema -----------------------------------------------------------------------------------------------

def define_field(store: Store, key, label, type, *, show_name: Optional[str] = None, required: bool = False,
                 multi: bool = False, actor: str = "desk") -> dict:
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise _bad("key must be 1 to 40 characters: a lowercase letter, then lowercase letters, digits or _")
    label = _clean_text(label, "label", MAX_LABEL)
    if type not in FIELD_TYPES:
        raise _bad(f"type must be one of {', '.join(FIELD_TYPES)}")
    if show_name is not None:
        show_name = _clean_text(show_name, "show", MAX_SHOW)
    if not isinstance(required, bool):
        raise _bad("required must be true or false")
    _check_multi(type, multi)
    with store.transaction("schema"):
        have = store.conn.execute("SELECT id, type FROM field_def WHERE key = ?", (key,)).fetchone()
        if have is not None:
            if have["type"] != type and _has_values(store, have["id"]):
                raise MetaError("type_locked", f"{key} already holds values as {have['type']}; its type cannot change")
            raise MetaError("field_exists", f"a field called {key} already exists")
        active = store.conn.execute("SELECT COUNT(*) FROM field_def WHERE retired_at IS NULL").fetchone()[0]
        if active >= MAX_ACTIVE_FIELDS:
            raise _bad(f"at most {MAX_ACTIVE_FIELDS} fields can be active; retire one first")
        position = store.conn.execute("SELECT COALESCE(MAX(position), 0) FROM field_def").fetchone()[0] + 1
        store.conn.execute(
            """INSERT INTO field_def (id, key, label, type, show_name, required, multi, position, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (_id(), key, label, type, show_name, required, multi, position, actor, _now()))
    return _field_json(_row(store, key), [])


def update_field(store: Store, key, *, label=_UNSET, type=_UNSET, required=_UNSET, multi=_UNSET) -> dict:
    """Edit a field. The type cannot change once any asset holds a value for it (type_locked)."""
    with store.transaction("schema"):
        row = _row(store, key)
        new_type = row["type"] if type is _UNSET else type
        new_multi = bool(row["multi"]) if multi is _UNSET else multi
        if new_type not in FIELD_TYPES:
            raise _bad(f"type must be one of {', '.join(FIELD_TYPES)}")
        _check_multi(new_type, new_multi)
        sets, args = [], []
        if label is not _UNSET:
            sets.append("label = ?")
            args.append(_clean_text(label, "label", MAX_LABEL))
        if required is not _UNSET:
            if not isinstance(required, bool):
                raise _bad("required must be true or false")
            sets.append("required = ?")
            args.append(required)
        if new_type != row["type"]:
            if _has_values(store, row["id"]):
                raise MetaError("type_locked", f"{key} already holds values as {row['type']}; its type cannot change")
            sets.append("type = ?")
            args.append(new_type)
        if new_multi != bool(row["multi"]):
            if not new_multi and store.conn.execute(
                    "SELECT 1 FROM asset_meta_value WHERE field_id = ? AND ord > 0 LIMIT 1", (row["id"],)).fetchone():
                raise _bad("some assets hold several values for this field")
            sets.append("multi = ?")
            args.append(new_multi)
        if sets:
            store.conn.execute(f"UPDATE field_def SET {', '.join(sets)} WHERE id = ?", (*args, row["id"]))
    return _terms_json(store, _row(store, key))


def retire_field(store: Store, key) -> None:
    with store.transaction("schema"):
        row = _row(store, key)
        store.conn.execute("UPDATE field_def SET retired_at = ? WHERE id = ?", (_now(), row["id"]))
        for held in store.conn.execute("SELECT DISTINCT asset_id FROM asset_meta_value WHERE field_id = ?", (row["id"],)).fetchall():
            _reindex(store, held["asset_id"])    # a retired field leaves search with it


def add_terms(store: Store, key, terms) -> list:
    """Add terms to an enum field's vocabulary. Returns the terms newly made available (a retired one is revived)."""
    if not isinstance(terms, list) or not terms:
        raise _bad("terms must be a non-empty list")
    clean, seen = [], set()
    for t in terms:
        t = _clean_text(t, "a term", MAX_TERM)
        if t not in seen:
            seen.add(t)
            clean.append(t)
    with store.transaction("schema"):
        row = _row(store, key)
        if row["type"] != "enum":
            raise _bad(f"{key} is not an enum field")
        have = {r["term"]: r["retired_at"] for r in store.conn.execute(
            "SELECT term, retired_at FROM field_term WHERE field_id = ?", (row["id"],)).fetchall()}
        fresh = [t for t in clean if t not in have or have[t] is not None]
        active = sum(1 for v in have.values() if v is None)
        if active + len(fresh) > MAX_TERMS:
            raise _bad(f"an enum field holds at most {MAX_TERMS} terms")
        position = store.conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM field_term WHERE field_id = ?", (row["id"],)).fetchone()[0]
        for t in fresh:
            if t in have:
                store.conn.execute("UPDATE field_term SET retired_at = NULL WHERE field_id = ? AND term = ?", (row["id"], t))
            else:
                position += 1
                store.conn.execute("INSERT INTO field_term (field_id, term, position) VALUES (?, ?, ?)", (row["id"], t, position))
    return fresh


def retire_term(store: Store, key, term) -> None:
    with store.transaction("schema"):
        row = _row(store, key)
        cur = store.conn.execute(
            "UPDATE field_term SET retired_at = ? WHERE field_id = ? AND term = ? AND retired_at IS NULL",
            (_now(), row["id"], term if isinstance(term, str) else ""))
        if cur.rowcount != 1:
            raise MetaError("not_in_vocabulary", f"{key} has no active term {term!r}")


def _terms_json(store: Store, row, include_retired: bool = False) -> dict:
    sql = "SELECT term, retired_at FROM field_term WHERE field_id = ?"
    if not include_retired:
        sql += " AND retired_at IS NULL"
    terms = [{"term": r["term"], "retired": r["retired_at"] is not None}
             for r in store.conn.execute(sql + " ORDER BY position, term", (row["id"],)).fetchall()]
    return _field_json(row, terms)


def list_schema(store: Store, show_name: Optional[str] = None, include_retired: bool = False) -> list:
    """Fields (with terms) in display order. With a show: the global fields plus that show's. Retired ones only on request."""
    where, args = [], []
    if not include_retired:
        where.append("retired_at IS NULL")
    if show_name is not None:
        where.append("(show_name IS NULL OR show_name = ?)")
        args.append(show_name)
    sql = "SELECT * FROM field_def" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY position, key"
    return [_terms_json(store, r, include_retired) for r in store.conn.execute(sql, tuple(args)).fetchall()]


# ---- values -----------------------------------------------------------------------------------------------

def _applies(row, asset) -> bool:
    return row["show_name"] is None or row["show_name"] == asset.show_name


def _typed(row, value):
    """One value as (v_text, v_num, v_date), or a MetaError."""
    key, type_ = row["key"], row["type"]

    def mismatch(what):
        return MetaError("type_mismatch", f"{key}: {what}")

    if type_ in ("text", "longtext", "enum"):
        if not isinstance(value, str) or "\x00" in value:
            raise mismatch("a value of this type must be text")
        limit = MAX_LONGTEXT if type_ == "longtext" else MAX_TEXT
        if type_ != "enum" and len(value) > limit:
            raise mismatch(f"text is over {limit} characters")
        return value, None, None
    if type_ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise mismatch("must be a number")
        number = float(value)
        if not math.isfinite(number) or abs(number) > MAX_NUMBER:
            raise mismatch(f"must be finite and within +/-{MAX_NUMBER:g}")
        return None, number, None
    if type_ == "date":
        if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
            raise mismatch("must be a date written YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise mismatch("is not a real calendar date") from None
        return None, None, value
    if type_ == "bool":
        if not isinstance(value, bool):
            raise mismatch("must be true or false")
        return None, 1.0 if value else 0.0, None
    raise mismatch("has an unknown type")  # pragma: no cover - FIELD_TYPES is checked at define time


def _validate_row(store: Store, asset, row, values) -> list:
    key = row["key"]
    if not isinstance(values, list):
        raise _bad("values must be a list")
    if not _applies(row, asset):
        raise MetaError("unknown_field", f"{key} does not apply to this asset's show")
    if not values:
        if row["required"]:
            raise MetaError("required_field", f"{key} is required and cannot be cleared")
        return []
    cap = MAX_MULTI_VALUES if row["multi"] else 1
    if len(values) > cap:
        raise _bad(f"{key} holds at most {cap} value{'s' if cap > 1 else ''}")
    typed = []
    stored_terms = set()
    if row["type"] == "enum":
        active = {r["term"] for r in store.conn.execute(
            "SELECT term FROM field_term WHERE field_id = ? AND retired_at IS NULL", (row["id"],)).fetchall()}
        stored_terms = {r["v_text"] for r in store.conn.execute(
            "SELECT v_text FROM asset_meta_value WHERE asset_id = ? AND field_id = ?", (asset.id, row["id"])).fetchall()}
    seen = set()
    for i, value in enumerate(values):
        v_text, v_num, v_date = _typed(row, value)
        if row["type"] == "enum" and v_text not in active and v_text not in stored_terms:
            raise MetaError("not_in_vocabulary", f"{key}: {v_text!r} is not in its vocabulary")
        marker = (v_text, v_num, v_date)
        if marker in seen:
            raise _bad(f"{key}: the same value is given twice")
        seen.add(marker)
        typed.append((i, v_text, v_num, v_date))
    return typed


def validate(store: Store, asset_id: str, key, values) -> list:
    """The typed rows (ord, v_text, v_num, v_date) a value set would store. Raises KeyError for an unknown asset."""
    asset = store.get_asset(asset_id)
    return _validate_row(store, asset, _row(store, key), values)


def _present(row, v_text, v_num, v_date):
    t = row["type"]
    if t in ("text", "longtext", "enum"):
        return v_text
    if t == "date":
        return v_date
    if t == "bool":
        return bool(v_num)
    return int(v_num) if float(v_num).is_integer() and abs(v_num) < 2 ** 53 else v_num


def _stored(store: Store, asset_id: str, row) -> list:
    return [_present(row, r["v_text"], r["v_num"], r["v_date"]) for r in store.conn.execute(
        "SELECT v_text, v_num, v_date FROM asset_meta_value WHERE asset_id = ? AND field_id = ? ORDER BY ord",
        (asset_id, row["id"])).fetchall()]


def set_values(store: Store, asset_id: str, key, values, actor: str) -> dict:
    """Replace one field's values for one asset, atomically. Returns {"old": [...], "new": [...]} for the audit row."""
    with store.transaction(f"meta:{asset_id}"):
        asset = store.get_asset(asset_id)
        row = _row(store, key)
        typed = _validate_row(store, asset, row, values)
        old = _stored(store, asset_id, row)
        store.conn.execute("DELETE FROM asset_meta_value WHERE asset_id = ? AND field_id = ?", (asset_id, row["id"]))
        now = _now()
        for ord_, v_text, v_num, v_date in typed:
            store.conn.execute(
                """INSERT INTO asset_meta_value (asset_id, field_id, ord, v_text, v_num, v_date, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (asset_id, row["id"], ord_, v_text, v_num, v_date, actor, now))
        new = [_present(row, t, n, d) for _o, t, n, d in typed]
        _reindex(store, asset_id)
    return {"old": old, "new": new}


def get_values(store: Store, asset_id: str, include_retired: bool = False) -> dict:
    """{"values": {key: [..]}, "missing": [required keys with no value]} for the fields that apply to the asset."""
    asset = store.get_asset(asset_id)
    values, missing = {}, []
    for row in store.conn.execute("SELECT * FROM field_def ORDER BY position, key").fetchall():
        retired = row["retired_at"] is not None
        if (retired and not include_retired) or not _applies(row, asset):
            continue
        held = _stored(store, asset_id, row)
        if held:
            values[row["key"]] = held
        elif row["required"] and not retired:
            missing.append(row["key"])
    return {"values": values, "missing": missing}
